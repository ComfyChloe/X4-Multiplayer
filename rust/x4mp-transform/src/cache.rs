//! Model of X4's component view-cache build.
//!
//! Every simulation frame, after physics, `U::UniverseFrameUpdate` (0x9fdef0) refreshes the
//! write-side view cache of the component tree: `PropagateCacheFlags(galaxy, idx, galaxy
//! flags | 0x6b)` (call at 0x9fe53c), then `U::Component::BuildCache(galaxy, idx, time)`
//! (0x3d4a80, call at 0x9fe549), then `UniverseClass::FlushOrphanCacheData` and
//! `U::PublishComponentViewCache`. This module reproduces the discrete part exactly:
//! which components are visited, which cache specifications apply where, the flags passed
//! to each `PropagateCacheFlags` (0x3d46b0, "PCF") call, the dirty-bit transitions inside
//! PCF, the specification timers and expiry, and the per-subtree specification counts.
//! The data PCF copies (transforms, velocities, attachment poses) comes from engine
//! virtual getters and is represented by the [`Getters`] callbacks.
//!
//! # Engine data (all offsets in `U::Component`, build 23660954)
//!
//! | offset | field | model |
//! |---|---|---|
//! | +0x70 | live parent | [`Node::parent`] |
//! | +0xa0 | attachment container (children for which vfunc slot 641 is true) | [`Node::attach`] |
//! | +0xa8 | child container | [`Node::live_children`] |
//! | +0xd8 | cache specification list (small vector, 4 inline entries of 0x20 bytes, capacity +0x80, count +0x88) | [`Node::spec`] |
//! | +0xe0 + idx*0x80 | view cache `idx` (0 or 1): +0x00 parent, +0x08 parent connection, +0x10 child vector, +0x28 pose block (0x70 bytes: local pose, scale, velocity), +0x30 attachment pose block, +0x38 attention, +0x3c dirty bits | [`Node::children`], [`Node::dirty`], ... |
//! | +0x1e0 | own cache flags (set by type-7 specifications) | [`Node::flags`] |
//! | +0x1e4 | clear mask: dirty bits that PCF clears after refreshing (bit clear = the value changes continuously and stays dirty) | [`Node::mask`] |
//! | +0x1e8 | number of specification entries in this subtree, own included | [`Node::count`] |
//! | +0x1ec | attention level | [`Node::attention`] |
//!
//! Globals: the specification stack 0x6cf1d50 (depth 0x6cf1d40, at most 99,
//! "MAX_CACHE_SPECIFICATIONS"), the truncation stack 0x6cf1a10 (depth 0x6cf1d30, at most
//! 99, "MAX_CACHE_TRUNCATIONS") and the number of entries on the specification stack
//! 0x6cf1d38. They make the engine traversal strictly single-threaded; [`Stacks`] holds them
//! per traversal here.

/// Cache flag and dirty bits (low 7 bits are the refreshable properties).
pub mod bits {
    /// Parent pointer and parent connection (vc+0x00/+0x08).
    pub const PARENT: u32 = 0x1;
    /// Child vector (vc+0x10), copied from the child container +0xa8.
    pub const CHILDREN: u32 = 0x2;
    /// Attachment container changed (only cleared).
    pub const ATTACH: u32 = 0x4;
    /// Local pose and scale (pose block +0x00 / +0x40, vfunc slots 648 / 649).
    pub const TRANSFORM: u32 = 0x8;
    /// Local velocity (pose block +0x50, vfunc slot 650).
    pub const VELOCITY: u32 = 0x10;
    /// Attachment poses (vc+0x30, vfunc slot 622).
    pub const CONNECTIONS: u32 = 0x20;
    /// Attention level (vc+0x38).
    pub const ATTENTION: u32 = 0x40;
    /// All refreshable properties (constant 0x31bea3c).
    pub const ALL: u32 = 0x7f;
    /// Status: the component is registered (+0xd1), mirrored by the parent refresh.
    pub const REGISTERED: u32 = 0x1_0000;
    /// Clear-mask bit: the component has no parent.
    pub const NO_PARENT: u32 = 0x2_0000;
    /// Pose changed since the last pose refresh (set by SetLocalTransform 0x746980).
    pub const POSE_CHANGED: u32 = 0x4_0000;
    /// Pose block not wanted: PCF frees it when the global 0x2f4b9bc is set.
    pub const DROP_POSE_BLOCK: u32 = 0x20_0000;
    /// Forces the attachment refresh (vfunc slot 622) together with CONNECTIONS.
    pub const CONNECTIONS_FORCED: u32 = 0x40_0000;
}

/// Flags OR-ed onto the galaxy's own flags for its root refresh (constant 0x31bea20).
pub const GALAXY_ROOT_FLAGS: u32 = 0x6b;
/// Initial clear mask and parent-dirty value of a component (constant 0x31bea2c).
pub const COMPONENT_MASK_INIT: u32 = 0x0024_007f;
/// "No class" in a specification's truncation class.
pub const CLASS_NONE: u32 = 0x79;
/// Base flags (global 0x3d72b8c, never written, so zero).
pub const BASE_FLAGS: u32 = 0;
/// Capacity of both global stacks.
pub const MAX_STACK: usize = 99;

/// Specification kinds and the sites that create them (all with `AddCacheSpec` 0x3d5180).
pub mod kinds {
    /// Galaxy constructor (0x5c0bc0 / 0x5c0830): flags 0x6b, truncated at sectors, every frame.
    pub const GALAXY: i32 = 1;
    /// `U::Sector::vfunc701` when the sector becomes active: flags 0x4f, truncated at zones, every frame.
    pub const SECTOR: i32 = 2;
    /// `U::Zone::vfunc701` when the zone becomes active: flags 0x7f, whole subtree, every frame.
    pub const ZONE: i32 = 3;
    /// `U::Highway::vfunc701`: flags 0x7f, whole subtree, every frame.
    pub const HIGHWAY: i32 = 4;
    /// `U::Highway::vfunc701` on an ancestor: flags 0x4f, truncated at zones, every frame.
    pub const HIGHWAY_ZONES: i32 = 5;
    /// One-shot "own" specification (interval -1, lives two frames, also sets +0x1e0):
    /// ConnectTo / AddEntityForPost (0x19), reparenting on the old parent (0x2 or 0x4),
    /// gates (0x20), NavigationController::ClearOwner (0x18).
    pub const OWN: i32 = 7;
    /// Map UI (UniverseMapBehavior): one-shot, flags 0x9 truncated at objects, 0x2 at docking bays.
    pub const MAP: i32 = 8;
    /// Online::VentureTracker::MoveShipToVenturerCluster: flags 0x7f truncated at objects.
    pub const VENTURE: i32 = 9;
}

/// One cache specification entry (0x20 bytes in the engine).
#[derive(Clone, Copy, Debug)]
pub struct SpecEntry {
    /// +0x00 kind (see [`kinds`]); kind 7 also drives the owner's +0x1e0.
    pub kind: i32,
    /// +0x04 flags applied to the owner's descendants while the entry is due.
    pub flags: u32,
    /// +0x08 truncation class: at a component of this class (IsClass, vtable slot 573) the
    /// entry stops applying below it. [`CLASS_NONE`] = never.
    pub class: u32,
    /// +0x0c set while the entry is truncated by a component on the current path.
    pub truncated: bool,
    /// +0x10 interval: 0 = every frame, > 0 = seconds, < 0 = one-shot countdown (+0.5 per
    /// visit while < -0.5; removed when it reaches [-0.5, 0)).
    pub interval: f64,
    /// +0x18 time of the last application.
    pub last: f64,
}

impl SpecEntry {
    /// Bitwise identity of all fields (floats compared by bits).
    pub fn same(&self, o: &SpecEntry) -> bool {
        self.kind == o.kind
            && self.flags == o.flags
            && self.class == o.class
            && self.truncated == o.truncated
            && self.interval.to_bits() == o.interval.to_bits()
            && self.last.to_bits() == o.last.to_bits()
    }
}

/// The parts of a component the view-cache build reads or writes.
#[derive(Clone, Debug, Default)]
pub struct Node {
    /// UniverseID (+0x08), for logs and captures.
    pub id: u64,
    /// Live parent (+0x70).
    pub parent: Option<usize>,
    /// View-cache child vector per cache index (vc+0x10); the traversal follows these.
    pub children: [Vec<usize>; 2],
    /// Child container (+0xa8), source of the child-vector refresh; `None` = no container.
    pub live_children: Option<Vec<usize>>,
    /// Attachment container (+0xa0), non-null elements in iteration order; `None` = none.
    pub attach: Option<Vec<usize>>,
    /// Own cache flags (+0x1e0).
    pub flags: u32,
    /// Clear mask (+0x1e4).
    pub mask: u32,
    /// Specification entries in this subtree (+0x1e8).
    pub count: i32,
    /// Specification list (+0xd8); `None` = no list allocated.
    pub spec: Option<Vec<SpecEntry>>,
    /// `IsClass(this, class)` (vtable slot 573) for class ids below 128.
    pub classes: u128,
    /// Dirty bits per cache index (vc+0x3c).
    pub dirty: [u32; 2],
    /// Registered flag (+0xd1).
    pub registered: bool,
    /// Connection mode (+0x68); decides whether the attachment refresh may copy from the other cache.
    pub mode: i32,
    /// Attention level (+0x1ec).
    pub attention: i32,
    /// Cached attention per cache index (vc+0x38).
    pub vc_attention: [i32; 2],
    /// Cached parent per cache index (vc+0x00).
    pub vc_parent: [Option<usize>; 2],
    /// Pose block allocated per cache index (vc+0x28 non-null).
    pub pose_block: [bool; 2],
    /// Reference count (+0x40), changed by the parent and child refreshes.
    pub refs: i32,
}

impl Node {
    /// `IsClass(this, class)`.
    pub fn is_class(&self, class: u32) -> bool {
        class < 128 && (self.classes >> class) & 1 == 1
    }
}

/// A component tree: the arena holding [`Node`]s.
#[derive(Clone, Debug, Default)]
pub struct CacheTree {
    /// Nodes by index; indices are the handles used everywhere in this crate.
    pub nodes: Vec<Node>,
    /// Global 0x2f4b9bc ("drop unwanted pose blocks"), 1 in the shipped executable.
    pub drop_pose_blocks: bool,
}

/// Node storage the traversal can run against: the whole tree, or a task's subtree.
pub trait Store {
    /// Shared access to a node.
    fn node(&self, i: usize) -> &Node;
    /// Exclusive access to a node.
    fn node_mut(&mut self, i: usize) -> &mut Node;
    /// Adds `delta` to the count of `start` and of every live ancestor (0x3d55c0 / 0x3d55e0).
    fn add_up(&mut self, start: usize, delta: i32);
    /// Adds `delta` to a reference count (+0x40, interlocked in the engine).
    fn add_refs(&mut self, node: usize, delta: i32);
    /// Global 0x2f4b9bc.
    fn drop_pose_blocks(&self) -> bool;
    /// Position in an access log, if the store keeps one (parallel hazard checks).
    fn access_mark(&self) -> usize {
        0
    }
}

impl Store for CacheTree {
    fn node(&self, i: usize) -> &Node {
        &self.nodes[i]
    }
    fn node_mut(&mut self, i: usize) -> &mut Node {
        &mut self.nodes[i]
    }
    fn add_up(&mut self, start: usize, delta: i32) {
        if delta == 0 {
            return;
        }
        let mut c = Some(start);
        while let Some(n) = c {
            let node = &mut self.nodes[n];
            node.count = node.count.wrapping_add(delta);
            c = node.parent;
        }
    }
    fn add_refs(&mut self, node: usize, delta: i32) {
        self.nodes[node].refs += delta;
    }
    fn drop_pose_blocks(&self) -> bool {
        self.drop_pose_blocks
    }
}

/// What the view-cache build observably does, in engine order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Event {
    /// `BuildCache(node)` was entered; `open` = the gate let it in.
    Visit { node: u32, open: bool },
    /// `IsClass(node, class)` was called (only for untruncated entries with a class).
    IsClass { node: u32, class: u32, result: bool },
    /// `PropagateCacheFlags(node, idx, flags)` was called; `attach` = from the attachment loop.
    Propagate { node: u32, flags: u32, attach: bool },
    /// A getter ran inside PCF.
    Get { node: u32, getter: Getter },
    /// A specification entry of `node` expired and was removed.
    Expire { node: u32, kind: i32 },
}

/// Engine getters PCF calls (virtual slots of the component).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Getter {
    /// Slot 648 (+0x1440) `GetLocalTransform(this, block, tls_time, 1, 1)`.
    Pose,
    /// Slot 649 (+0x1448) `GetScale(this, block + 0x40, tls_time, 1, 1)`.
    Scale,
    /// Slot 650 (+0x1450) `GetLocalVelocity(this, block + 0x50, tls_time, 1, 1, pose?)`;
    /// `with_pose` = the pose was refreshed in the same call (block passed, else null).
    Velocity { with_pose: bool },
    /// Slot 622 (+0x1370) attachment poses; `other` = the other cache was offered for copying.
    Connections { other: bool },
}

impl Event {
    /// Whether an offline oracle that only sees virtual calls can observe this event.
    pub fn is_virtual_call(&self) -> bool {
        matches!(self, Event::IsClass { .. } | Event::Get { .. })
    }
}

/// Engine getters called by PCF. Implementations copy the real data (engine shim), record
/// calls (tests, oracle comparison) or do nothing.
pub trait Getters {
    /// Pose (slot 648) and scale (slot 649) refresh of `node`'s cache `idx`.
    fn pose(&mut self, node: usize, idx: usize);
    /// Velocity refresh (slot 650).
    fn velocity(&mut self, node: usize, idx: usize, with_pose: bool);
    /// Attachment refresh (slot 622). Receives the dirty bits and returns them as the
    /// engine function leaves them (it manages bits 0x80000, 0x100000 and 0x400000).
    fn connections(&mut self, node: usize, idx: usize, other: Option<usize>, dirty: u32) -> u32 {
        let _ = (node, idx, other);
        dirty
    }
}

/// Getters that do nothing.
pub struct NoGetters;
impl Getters for NoGetters {
    fn pose(&mut self, _: usize, _: usize) {}
    fn velocity(&mut self, _: usize, _: usize, _: bool) {}
}

/// The global traversal state of one `BuildCache` run.
#[derive(Clone, Debug, Default)]
pub struct Stacks {
    /// Specification stack: owners whose lists are pushed, bottom first.
    pub spec: Vec<SpecSource>,
    /// Truncation stack: entries truncated on the current path.
    pub trunc: Vec<(usize, usize)>,
    /// Number of entries on the specification stack (0x6cf1d38).
    pub total: usize,
}

impl Stacks {
    /// A copy whose lists are all private: what a task needs to continue the walk below the
    /// current component without touching the ancestors' lists.
    pub fn freeze<S: Store>(&self, store: &S) -> Stacks {
        let spec = self
            .spec
            .iter()
            .map(|s| match s {
                SpecSource::Node(o) => SpecSource::Inherited(store.node(*o).spec.clone().unwrap_or_default()),
                SpecSource::Inherited(v) => SpecSource::Inherited(v.clone()),
            })
            .collect();
        Stacks { spec, trunc: self.trunc.clone(), total: self.total }
    }
}

/// A subtree the split policy handed off instead of descending.
#[derive(Clone, Debug)]
pub struct TaskSlot {
    /// Subtree root (BuildCache would have been called on it).
    pub node: usize,
    /// The stacks at that call, with private copies of the inherited lists.
    pub stacks: Stacks,
    /// Position in the event log where the subtree's events belong.
    pub log_pos: usize,
    /// The store's access mark at that call.
    pub access_mark: usize,
}

/// Where a specification list on the stack lives.
#[derive(Clone, Debug)]
pub enum SpecSource {
    /// The list of a node in the current store.
    Node(usize),
    /// A private copy of an ancestor's list (parallel tasks; truncation is per task).
    Inherited(Vec<SpecEntry>),
}

/// Fatal engine errors (the engine logs the path and aborts).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Overrun {
    /// "MAX_CACHE_SPECIFICATIONS has been exceeded!" (assert id 0x3f2).
    Specifications,
    /// "MAX_CACHE_TRUNCATIONS has been exceeded!" (assert id 0x3f3).
    Truncations,
}

/// Where the traversal hands a subtree to someone else instead of descending.
pub trait Split {
    /// Called before `BuildCache(node)` would run; `true` = record a task instead.
    fn split(&mut self, store: &dyn StoreView, node: usize, stacks: &Stacks) -> bool;
}

/// Read access to a store without its type.
pub trait StoreView {
    /// Shared access to a node.
    fn view(&self, i: usize) -> &Node;
}
impl<S: Store> StoreView for S {
    fn view(&self, i: usize) -> &Node {
        self.node(i)
    }
}

/// No splitting: the plain sequential engine traversal.
pub struct NoSplit;
impl Split for NoSplit {
    fn split(&mut self, _: &dyn StoreView, _: usize, _: &Stacks) -> bool {
        false
    }
}

fn spec_len<S: Store>(store: &S, src: &SpecSource) -> usize {
    match src {
        SpecSource::Node(o) => store.node(*o).spec.as_ref().map_or(0, Vec::len),
        SpecSource::Inherited(v) => v.len(),
    }
}

/// One traversal: store, stacks, getters, log and split policy.
pub struct Traversal<'a, S: Store, G: Getters, P: Split> {
    /// Nodes.
    pub store: &'a mut S,
    /// Global stacks.
    pub stacks: Stacks,
    /// Getter callbacks.
    pub getters: &'a mut G,
    /// Event log (in engine order).
    pub log: Vec<Event>,
    /// Split policy.
    pub split: P,
    /// Subtrees handed off by the split policy, in walk order.
    pub tasks: Vec<TaskSlot>,
    /// Frame time (BuildCache's time argument and the getters' TLS time).
    pub time: f64,
    /// Cache index being written.
    pub idx: usize,
}

impl<'a, S: Store, G: Getters, P: Split> Traversal<'a, S, G, P> {
    /// Creates a traversal with empty stacks.
    pub fn new(store: &'a mut S, getters: &'a mut G, split: P, idx: usize, time: f64) -> Self {
        Traversal { store, stacks: Stacks::default(), getters, log: Vec::new(), split, tasks: Vec::new(), time, idx }
    }

    /// `U::Component::BuildCache(node, idx, time)` (0x3d4a80).
    pub fn build_cache(&mut self, c: usize) -> Result<(), Overrun> {
        if self.split.split(&*self.store, c, &self.stacks) {
            let access_mark = self.store.access_mark();
            let stacks = self.stacks.freeze(&*self.store);
            self.tasks.push(TaskSlot { node: c, stacks, log_pos: self.log.len(), access_mark });
            return Ok(());
        }
        let saved_trunc = self.stacks.trunc.len();
        if self.stacks.total == saved_trunc && self.store.node(c).count == 0 {
            self.log.push(Event::Visit { node: c as u32, open: false });
            return Ok(());
        }
        self.log.push(Event::Visit { node: c as u32, open: true });
        if let Some(own) = &self.store.node(c).spec {
            if self.stacks.spec.len() == MAX_STACK {
                return Err(Overrun::Specifications);
            }
            self.stacks.total += own.len();
            self.stacks.spec.push(SpecSource::Node(c));
        }
        let acc = self.accumulate(c)?;
        let kids = self.store.node(c).children[self.idx].clone();
        for ch in kids {
            let f = acc | self.store.node(ch).flags;
            if f != 0 {
                self.propagate(ch, f, false);
            }
            self.build_cache(ch)?;
        }
        for k in saved_trunc..self.stacks.trunc.len() {
            let (s, e) = self.stacks.trunc[k];
            match &mut self.stacks.spec[s] {
                SpecSource::Node(o) => {
                    let o = *o;
                    self.store.node_mut(o).spec.as_mut().expect("truncated list vanished")[e].truncated = false;
                }
                SpecSource::Inherited(v) => v[e].truncated = false,
            }
        }
        self.stacks.trunc.truncate(saved_trunc);
        if let Some(att) = self.store.node(c).attach.clone() {
            for a in att {
                self.propagate(a, bits::ALL, true);
            }
        }
        if self.store.node(c).spec.is_some() {
            let top = self.stacks.spec.pop().expect("specification stack underflow");
            self.stacks.total -= spec_len(&*self.store, &top);
            self.expire(c);
        }
        Ok(())
    }

    /// The loop over every specification entry on the stack (0x3d4bdd..0x3d4cc0).
    fn accumulate(&mut self, c: usize) -> Result<u32, Overrun> {
        let mut acc = BASE_FLAGS;
        let time = self.time;
        let depth = self.stacks.spec.len();
        for s in 0..depth {
            let n = spec_len(&*self.store, &self.stacks.spec[s]);
            for e in 0..n {
                let entry = match &self.stacks.spec[s] {
                    SpecSource::Node(o) => self.store.node(*o).spec.as_ref().unwrap()[e],
                    SpecSource::Inherited(v) => v[e],
                };
                if entry.truncated {
                    continue;
                }
                if entry.class != CLASS_NONE {
                    let result = self.store.node(c).is_class(entry.class);
                    self.log.push(Event::IsClass { node: c as u32, class: entry.class, result });
                    if result {
                        if self.stacks.trunc.len() == MAX_STACK {
                            return Err(Overrun::Truncations);
                        }
                        match &mut self.stacks.spec[s] {
                            SpecSource::Node(o) => {
                                let o = *o;
                                self.store.node_mut(o).spec.as_mut().unwrap()[e].truncated = true;
                            }
                            SpecSource::Inherited(v) => v[e].truncated = true,
                        }
                        self.stacks.trunc.push((s, e));
                    }
                }
                if acc | entry.flags != acc && time >= entry.last + entry.interval {
                    acc |= entry.flags;
                }
            }
        }
        Ok(acc)
    }

    /// Timer update and expiry of `c`'s own entries after its subtree (0x3d4f5e..0x3d50de).
    fn expire(&mut self, c: usize) {
        let time = self.time;
        let mut i = 0;
        loop {
            let len = self.store.node(c).spec.as_ref().map_or(0, Vec::len);
            if i >= len {
                return;
            }
            let (interval, kind) = {
                let e = &mut self.store.node_mut(c).spec.as_mut().unwrap()[i];
                let interval = e.interval;
                if !(interval + e.last > time) {
                    e.last = time;
                }
                (interval, e.kind)
            };
            if interval >= -0.5 {
                if interval >= 0.0 {
                    i += 1;
                    continue;
                }
                let parent = {
                    let n = self.store.node_mut(c);
                    n.count = n.count.wrapping_sub(1);
                    n.parent
                };
                if let Some(p) = parent {
                    self.store.add_up(p, -1);
                }
                let n = self.store.node_mut(c);
                if kind == kinds::OWN {
                    n.flags = BASE_FLAGS;
                }
                let list = n.spec.as_mut().unwrap();
                list.remove(i);
                self.log.push(Event::Expire { node: c as u32, kind });
                if list.is_empty() {
                    n.spec = None;
                    return;
                }
            } else {
                self.store.node_mut(c).spec.as_mut().unwrap()[i].interval = interval + 0.5;
                i += 1;
            }
        }
    }

    /// `maybe_U::Component::PropagateCacheFlags(node, idx, &flags)` (0x3d46b0).
    pub fn propagate(&mut self, c: usize, f: u32, attach: bool) {
        self.log.push(Event::Propagate { node: c as u32, flags: f, attach });
        let idx = self.idx;
        let drop_blocks = self.store.drop_pose_blocks();
        let d0 = self.store.node(c).dirty[idx];
        if d0 == 0 {
            return;
        }
        let mut seen = d0;
        if f & bits::PARENT != 0 && seen & bits::PARENT != 0 {
            let (new_parent, old_parent) = {
                let n = self.store.node(c);
                (n.parent, n.vc_parent[idx])
            };
            if let Some(p) = new_parent {
                self.store.add_refs(p, 1);
            }
            self.store.node_mut(c).vc_parent[idx] = new_parent;
            if let Some(p) = old_parent {
                self.store.add_refs(p, -1);
            }
            let n = self.store.node_mut(c);
            if n.mask & bits::PARENT != 0 {
                n.dirty[idx] &= !bits::PARENT;
            }
            if n.registered {
                n.dirty[idx] |= bits::REGISTERED;
            } else {
                n.dirty[idx] &= !bits::REGISTERED;
            }
            seen = n.dirty[idx];
        }
        if f & bits::CHILDREN != 0 && seen & bits::CHILDREN != 0 {
            let (fresh, old) = {
                let n = self.store.node(c);
                (n.live_children.clone().unwrap_or_default(), n.children[idx].clone())
            };
            for &k in &fresh {
                self.store.add_refs(k, 1);
            }
            for &k in &old {
                self.store.add_refs(k, -1);
            }
            let n = self.store.node_mut(c);
            n.children[idx] = fresh;
            if n.mask & bits::CHILDREN != 0 {
                n.dirty[idx] &= !bits::CHILDREN;
            }
        }
        {
            let n = self.store.node_mut(c);
            if f & bits::ATTACH != 0 && n.dirty[idx] & bits::ATTACH != 0 && n.mask & bits::ATTACH != 0 {
                n.dirty[idx] &= !bits::ATTACH;
            }
        }
        if !drop_blocks || self.store.node(c).dirty[idx] & bits::DROP_POSE_BLOCK == 0 {
            self.store.node_mut(c).pose_block[idx] = true;
            let mut posed = false;
            if f & bits::TRANSFORM != 0 && self.store.node(c).dirty[idx] & bits::TRANSFORM != 0 {
                posed = true;
                self.log.push(Event::Get { node: c as u32, getter: Getter::Pose });
                self.log.push(Event::Get { node: c as u32, getter: Getter::Scale });
                self.getters.pose(c, idx);
                let n = self.store.node_mut(c);
                if n.mask & bits::TRANSFORM != 0 {
                    n.dirty[idx] &= !bits::TRANSFORM;
                }
                n.dirty[idx] &= !bits::POSE_CHANGED;
            }
            if f & bits::VELOCITY != 0 && self.store.node(c).dirty[idx] & bits::VELOCITY != 0 {
                self.log.push(Event::Get { node: c as u32, getter: Getter::Velocity { with_pose: posed } });
                self.getters.velocity(c, idx, posed);
                let n = self.store.node_mut(c);
                if n.mask & bits::VELOCITY != 0 {
                    n.dirty[idx] &= !bits::VELOCITY;
                }
            }
        } else {
            self.store.node_mut(c).pose_block[idx] = false;
        }
        {
            let n = self.store.node_mut(c);
            if f & bits::ATTENTION != 0 && n.dirty[idx] & bits::ATTENTION != 0 {
                n.vc_attention[idx] = n.attention;
                if n.mask & bits::ATTENTION != 0 {
                    n.dirty[idx] &= !bits::ATTENTION;
                }
            }
        }
        let (dirty, mode) = {
            let n = self.store.node(c);
            (n.dirty[idx], n.mode)
        };
        if f & bits::CONNECTIONS != 0 && dirty & (bits::CONNECTIONS | bits::CONNECTIONS_FORCED) != 0 {
            let other =
                if mode != 0 && (dirty & bits::CONNECTIONS_FORCED == 0 || mode != 2) { Some(1 - idx) } else { None };
            self.log.push(Event::Get { node: c as u32, getter: Getter::Connections { other: other.is_some() } });
            let d = self.getters.connections(c, idx, other, dirty);
            let n = self.store.node_mut(c);
            n.dirty[idx] = d;
            if n.mask & bits::CONNECTIONS != 0 {
                n.dirty[idx] &= !bits::CONNECTIONS;
            }
        }
    }
}

/// One view-cache frame as `U::UniverseFrameUpdate` runs it: the galaxy's root refresh
/// with its own flags | 0x6b, then `BuildCache(galaxy)`. Returns the event log.
pub fn frame<G: Getters>(
    tree: &mut CacheTree,
    root: usize,
    idx: usize,
    time: f64,
    getters: &mut G,
) -> Result<Vec<Event>, Overrun> {
    let mut t = Traversal::new(tree, getters, NoSplit, idx, time);
    let f = t.store.node(root).flags | GALAXY_ROOT_FLAGS;
    if f != 0 {
        t.propagate(root, f, false);
    }
    t.build_cache(root)?;
    Ok(t.log)
}

/// `AddCacheSpec` (0x3d5180): adds an entry or merges into the entry of the same kind.
///
/// A positive interval randomises the phase: `last = now - FloatState(tls_rng, interval)`,
/// which `rng` supplies (the engine draws from the calling thread's TLS stream 0x318; the
/// shipped call sites all pass 0 or -1, so no draw happens in practice).
#[allow(clippy::too_many_arguments)]
pub fn add_spec<S: Store>(
    store: &mut S,
    c: usize,
    kind: i32,
    flags: u32,
    class: u32,
    interval: f64,
    now: f64,
    rng: &mut dyn FnMut(f32) -> f32,
) {
    let last = |rng: &mut dyn FnMut(f32) -> f32| if interval > 0.0 { now - rng(interval as f32) as f64 } else { now };
    if let Some(list) = store.node_mut(c).spec.as_mut() {
        if let Some(e) = list.iter_mut().find(|e| e.kind == kind) {
            e.flags |= flags;
            e.class = class;
            e.interval = interval;
            e.last = last(rng);
            if kind == kinds::OWN {
                store.node_mut(c).flags |= flags;
            }
            return;
        }
    }
    let entry = SpecEntry { kind, flags, class, truncated: false, interval, last: last(rng) };
    let n = store.node_mut(c);
    n.spec.get_or_insert_with(Vec::new).push(entry);
    if kind == kinds::OWN {
        n.flags = flags;
    }
    n.count = n.count.wrapping_add(1);
    if let Some(p) = n.parent {
        store.add_up(p, 1);
    }
}

/// `RemoveCacheSpec` (0x3d5440): removes the first entry of `kind`, if any.
pub fn remove_spec<S: Store>(store: &mut S, c: usize, kind: i32) {
    let Some(pos) = store.node(c).spec.as_ref().and_then(|l| l.iter().position(|e| e.kind == kind)) else {
        return;
    };
    let parent = {
        let n = store.node_mut(c);
        n.count = n.count.wrapping_sub(1);
        n.parent
    };
    if let Some(p) = parent {
        store.add_up(p, -1);
    }
    let n = store.node_mut(c);
    if kind == kinds::OWN {
        n.flags = BASE_FLAGS;
    }
    let list = n.spec.as_mut().unwrap();
    list.remove(pos);
    if list.is_empty() {
        n.spec = None;
    }
}

/// Recomputes every node's +0x1e8 from the lists (what the engine maintains incrementally).
pub fn recount(tree: &mut CacheTree) {
    for n in &mut tree.nodes {
        n.count = 0;
    }
    for i in 0..tree.nodes.len() {
        let own = tree.nodes[i].spec.as_ref().map_or(0, Vec::len) as i32;
        if own != 0 {
            tree.add_up(i, own);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn node(parent: Option<usize>, classes: &[u32]) -> Node {
        let mut n = Node { parent, mask: COMPONENT_MASK_INIT, refs: 100, ..Node::default() };
        for &c in classes {
            n.classes |= 1u128 << c;
        }
        n
    }

    fn tree(nodes: Vec<Node>) -> CacheTree {
        let mut t = CacheTree { nodes, drop_pose_blocks: true };
        for i in 0..t.nodes.len() {
            if let Some(p) = t.nodes[i].parent {
                t.nodes[p].children[0].push(i);
                t.nodes[p].children[1].push(i);
                t.nodes[p].live_children.get_or_insert_with(Vec::new).push(i);
            }
        }
        t
    }

    fn entry(kind: i32, flags: u32, class: u32, interval: f64, last: f64) -> SpecEntry {
        SpecEntry { kind, flags, class, truncated: false, interval, last }
    }

    struct Rec(Vec<(usize, usize, &'static str)>);
    impl Getters for Rec {
        fn pose(&mut self, n: usize, i: usize) {
            self.0.push((n, i, "pose"));
        }
        fn velocity(&mut self, n: usize, i: usize, _: bool) {
            self.0.push((n, i, "vel"));
        }
    }

    /// galaxy 0 -> cluster 1 -> sector 2 -> zone 3 -> ship 4 -> part 5
    fn universe() -> CacheTree {
        let mut t = tree(vec![
            node(None, &[0x2f]),
            node(Some(0), &[0x0f]),
            node(Some(1), &[0x58]),
            node(Some(2), &[0x6d]),
            node(Some(3), &[0x49, 0x75]),
            node(Some(4), &[0x73]),
        ]);
        t.nodes[0].spec = Some(vec![entry(kinds::GALAXY, 0x6b, 0x58, 0.0, 0.0)]);
        recount(&mut t);
        t
    }

    fn visits(log: &[Event]) -> Vec<(u32, bool)> {
        log.iter()
            .filter_map(|e| if let Event::Visit { node, open } = e { Some((*node, *open)) } else { None })
            .collect()
    }
    fn props(log: &[Event]) -> Vec<(u32, u32)> {
        log.iter()
            .filter_map(|e| if let Event::Propagate { node, flags, .. } = e { Some((*node, *flags)) } else { None })
            .collect()
    }

    #[test]
    fn galaxy_specification_reaches_the_children_of_sectors_only() {
        let mut t = universe();
        let log = frame(&mut t, 0, 0, 10.0, &mut NoGetters).unwrap();
        // The zone is visited (child of a sector) but its gate is closed: the galaxy entry is
        // truncated at the sector and nothing below has a specification.
        assert_eq!(visits(&log), vec![(0, true), (1, true), (2, true), (3, false)]);
        assert_eq!(props(&log), vec![(0, 0x6b), (1, 0x6b), (2, 0x6b), (3, 0x6b)]);
        assert!(log.contains(&Event::IsClass { node: 2, class: 0x58, result: true }));
        assert!(!t.nodes[0].spec.as_ref().unwrap()[0].truncated, "truncation must be undone");
        assert_eq!(t.nodes[0].spec.as_ref().unwrap()[0].last, 10.0);
    }

    #[test]
    fn active_sector_and_zone_specifications() {
        let mut t = universe();
        add_spec(&mut t, 2, kinds::SECTOR, 0x4f, 0x6d, 0.0, 5.0, &mut |_| 0.0);
        let log = frame(&mut t, 0, 1, 10.0, &mut NoGetters).unwrap();
        // The sector entry is truncated at the zone; the ship (zone child) gets 0x4f but its
        // own gate stays closed, so the part below it is never reached.
        assert_eq!(visits(&log), vec![(0, true), (1, true), (2, true), (3, true), (4, false)]);
        assert_eq!(props(&log).last(), Some(&(4, 0x4f)));
        add_spec(&mut t, 3, kinds::ZONE, 0x7f, CLASS_NONE, 0.0, 5.0, &mut |_| 0.0);
        let log = frame(&mut t, 0, 0, 10.5, &mut NoGetters).unwrap();
        assert_eq!(visits(&log).len(), 6);
        assert_eq!(props(&log)[4..], [(4, 0x7f), (5, 0x7f)]);
        assert_eq!(t.nodes[0].count, 3);
        assert_eq!(t.nodes[3].count, 1);
    }

    #[test]
    fn one_shot_own_specification_lives_two_frames() {
        let mut t = universe();
        add_spec(&mut t, 4, kinds::OWN, 0x19, CLASS_NONE, -1.0, 7.0, &mut |_| 0.0);
        assert_eq!(t.nodes[4].flags, 0x19);
        assert_eq!((t.nodes[0].count, t.nodes[3].count, t.nodes[4].count), (2, 1, 1));
        let log = frame(&mut t, 0, 0, 7.0, &mut NoGetters).unwrap();
        // The ship gets its own flags from the zone's loop, the part gets the entry's flags.
        assert!(props(&log).contains(&(4, 0x19)));
        assert!(props(&log).contains(&(5, 0x19)));
        assert_eq!(t.nodes[4].spec.as_ref().unwrap()[0].interval, -0.5);
        let log = frame(&mut t, 0, 1, 7.1, &mut NoGetters).unwrap();
        assert!(log.contains(&Event::Expire { node: 4, kind: kinds::OWN }));
        assert!(t.nodes[4].spec.is_none());
        assert_eq!(t.nodes[4].flags, 0);
        assert_eq!((t.nodes[0].count, t.nodes[3].count, t.nodes[4].count), (1, 0, 0));
        let log = frame(&mut t, 0, 0, 7.2, &mut NoGetters).unwrap();
        assert_eq!(visits(&log), vec![(0, true), (1, true), (2, true), (3, false)]);
    }

    #[test]
    fn intervals_gate_the_flags_but_not_the_visit() {
        let mut t = universe();
        t.nodes[3].spec = Some(vec![entry(kinds::HIGHWAY, 0x10, CLASS_NONE, 1.0, 9.5)]);
        recount(&mut t);
        let log = frame(&mut t, 0, 0, 10.0, &mut NoGetters).unwrap();
        assert!(!props(&log).iter().any(|p| p.0 == 4), "not due: nodes below get no call");
        // An active entry keeps the whole subtree open even when it is not due.
        assert!(visits(&log).contains(&(4, true)) && visits(&log).contains(&(5, true)));
        assert_eq!(t.nodes[3].spec.as_ref().unwrap()[0].last, 9.5);
        let log = frame(&mut t, 0, 0, 10.5, &mut NoGetters).unwrap();
        assert!(props(&log).contains(&(4, 0x10)));
        assert_eq!(t.nodes[3].spec.as_ref().unwrap()[0].last, 10.5);
    }

    #[test]
    fn attachments_get_all_flags() {
        let mut t = universe();
        t.nodes.push(node(Some(4), &[0x6b]));
        t.nodes[4].attach = Some(vec![6]);
        add_spec(&mut t, 3, kinds::ZONE, 0x08, CLASS_NONE, 0.0, 0.0, &mut |_| 0.0);
        let log = frame(&mut t, 0, 0, 1.0, &mut NoGetters).unwrap();
        assert!(log.contains(&Event::Propagate { node: 6, flags: bits::ALL, attach: true }));
    }

    #[test]
    fn merge_into_existing_kind() {
        let mut t = universe();
        add_spec(&mut t, 3, kinds::OWN, 0x2, CLASS_NONE, -1.0, 1.0, &mut |_| 0.0);
        add_spec(&mut t, 3, kinds::OWN, 0x4, CLASS_NONE, -1.0, 2.0, &mut |_| 0.0);
        let l = t.nodes[3].spec.as_ref().unwrap();
        assert_eq!((l.len(), l[0].flags, l[0].last, t.nodes[3].flags), (1, 0x6, 2.0, 0x6));
        assert_eq!(t.nodes[0].count, 2);
        add_spec(&mut t, 3, kinds::HIGHWAY, 0x1, CLASS_NONE, 2.0, 10.0, &mut |m| m * 0.25);
        assert_eq!(t.nodes[3].spec.as_ref().unwrap()[1].last, 9.5);
        remove_spec(&mut t, 3, kinds::OWN);
        assert_eq!((t.nodes[3].flags, t.nodes[0].count), (0, 2));
    }

    #[test]
    fn dirty_bits_follow_the_clear_mask() {
        let mut t = universe();
        add_spec(&mut t, 3, kinds::ZONE, 0x7f, CLASS_NONE, 0.0, 0.0, &mut |_| 0.0);
        // ship: moving (pose/velocity stay dirty), part: static
        t.nodes[4].mask = COMPONENT_MASK_INIT & !(bits::TRANSFORM | bits::VELOCITY);
        t.nodes[4].dirty = [bits::TRANSFORM | bits::VELOCITY | bits::POSE_CHANGED; 2];
        t.nodes[5].dirty = [bits::TRANSFORM | bits::ATTENTION; 2];
        t.nodes[5].attention = 9;
        let mut rec = Rec(Vec::new());
        frame(&mut t, 0, 0, 1.0, &mut rec).unwrap();
        assert_eq!(t.nodes[4].dirty[0], bits::TRANSFORM | bits::VELOCITY);
        assert_eq!(t.nodes[5].dirty[0], 0);
        assert_eq!(t.nodes[5].vc_attention, [9, 0]);
        assert_eq!(rec.0, vec![(4, 0, "pose"), (4, 0, "vel"), (5, 0, "pose")]);
        rec.0.clear();
        frame(&mut t, 0, 0, 1.1, &mut rec).unwrap();
        assert_eq!(rec.0, vec![(4, 0, "pose"), (4, 0, "vel")], "static part is clean now");
        assert_eq!(t.nodes[5].dirty[1], bits::TRANSFORM | bits::ATTENTION, "other cache untouched");
    }

    #[test]
    fn child_refresh_changes_what_the_traversal_follows() {
        let mut t = universe();
        add_spec(&mut t, 3, kinds::ZONE, 0x7f, CLASS_NONE, 0.0, 0.0, &mut |_| 0.0);
        // a new part 6 below the ship, known only to the live container
        t.nodes.push(node(Some(4), &[0x73]));
        t.nodes[4].live_children.as_mut().unwrap().push(6);
        let log = frame(&mut t, 0, 0, 1.0, &mut NoGetters).unwrap();
        assert!(!visits(&log).iter().any(|v| v.0 == 6), "ship's child vector not dirty");
        t.nodes[4].dirty[0] |= bits::CHILDREN;
        let log = frame(&mut t, 0, 0, 1.1, &mut NoGetters).unwrap();
        assert!(visits(&log).contains(&(6, true)));
        assert_eq!(t.nodes[4].children[0], vec![5, 6]);
        assert_eq!(t.nodes[6].refs, 101);
    }

    #[test]
    fn stack_limits() {
        let mut nodes = vec![node(None, &[])];
        for i in 1..=100 {
            nodes.push(node(Some(i - 1), &[]));
        }
        let mut t = tree(nodes);
        for i in 0..=100 {
            t.nodes[i].spec = Some(vec![entry(kinds::HIGHWAY, 1, CLASS_NONE, 0.0, 0.0)]);
        }
        recount(&mut t);
        assert_eq!(frame(&mut t, 0, 0, 1.0, &mut NoGetters), Err(Overrun::Specifications));
    }
}
