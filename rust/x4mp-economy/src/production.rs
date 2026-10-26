//! Production modules: the engine's state machine, reproduced transition by transition.
//!
//! Source (X4 9.00 build 23660954, Windows RVAs; static analysis of the disassembly, see
//! reports/REWRITE-ECONOMY.md section 3):
//!
//! - `U::ProductionModule::Step` 0x761720 performs one transition (switch on `+0x3f0`, jump
//!   table 0x763044) at a given event time and returns the next event time (`+0x390`).
//! - `U::ProductionModule::PrepareUpdate` 0x761320 gives a module's first queue time when a
//!   catch-up starts.
//! - `U::Container::UpdateProduction` 0x41a910 is the catch-up. It is *lazy*: readers
//!   (`GetCargo`, `GetStorageData`, cargo filters, trade code, FFI `UpdateProduction`) call
//!   it with the current game time. It processes every due transition of every operational
//!   module **at the transition's own event time**, in ascending `(time, module address)`
//!   order (with one engine quirk, see [`requeue`]), until no module is due at the call time.
//!
//! What the station provides (storage and resource checks, cargo changes, efficiency) is
//! behind [`StationEnv`], because those parts are separate engine functions (0x424b90 and
//! 0x356010 can-store, 0x356090 has-resources, 0x3fb9f0/0x3ff1c0 cargo removal and addition,
//! 0x7602d0 efficiency). Randomness goes through [`ProductionRng`]: the engine draws from the
//! thread stream (TLS+0x318) for retry delays and the random item choice only.
//!
//! Floating point follows the engine's operation order and precision: f64 times, f32 retry
//! arithmetic widened with `cvtss2sd`, and x86 truncating conversions for amounts.

use crate::ids::WareIx;
use crate::time::{is_set, UNSET};

/// `U::ProductionModule +0x3f0`; names and values from the enum table at 0x24d5cd0.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[repr(i32)]
pub enum State {
    Empty = 1,
    QueueNotStarted = 2,
    QueueStarted = 3,
    ChoosingItem = 4,
    ItemStarted = 5,
    WaitingForResources = 6,
    CycleStarted = 7,
    Producing = 8,
    WaitingForStorage = 9,
    CycleFinished = 10,
    ItemFinished = 11,
    QueueFinished = 12,
    Paused = 13,
}

impl State {
    pub fn from_raw(v: i32) -> Option<State> {
        use State::*;
        Some(match v {
            1 => Empty,
            2 => QueueNotStarted,
            3 => QueueStarted,
            4 => ChoosingItem,
            5 => ItemStarted,
            6 => WaitingForResources,
            7 => CycleStarted,
            8 => Producing,
            9 => WaitingForStorage,
            10 => CycleFinished,
            11 => ItemFinished,
            12 => QueueFinished,
            13 => Paused,
            _ => return None,
        })
    }

    pub fn name(self) -> &'static str {
        use State::*;
        match self {
            Empty => "empty",
            QueueNotStarted => "queuenotstarted",
            QueueStarted => "queuestarted",
            ChoosingItem => "choosingitem",
            ItemStarted => "itemstarted",
            WaitingForResources => "waitingforresources",
            CycleStarted => "cyclestarted",
            Producing => "producing",
            WaitingForStorage => "waitingforstorage",
            CycleFinished => "cyclefinished",
            ItemFinished => "itemfinished",
            QueueFinished => "queuefinished",
            Paused => "paused",
        }
    }
}

/// x86 `cvttsd2si r32`: truncation, `i32::MIN` for NaN and out-of-range values.
#[inline]
pub fn cvttsd2si(x: f64) -> i32 {
    if x.is_nan() || x >= 2147483648.0 || x <= -2147483649.0 {
        i32::MIN
    } else {
        x as i32
    }
}

/// x86 `cvttss2si r32`: truncation, `i32::MIN` for NaN and out-of-range values.
#[inline]
pub fn cvttss2si(x: f32) -> i32 {
    if !(-2_147_483_648.0f32..2_147_483_648.0f32).contains(&x) {
        i32::MIN
    } else {
        x as i32
    }
}

/// Efficiency factors of the current item, `+0x3b0..+0x3d8`, written by
/// `ComputeEfficiency` 0x7602d0 in states 4/5/6 and by `PrepareUpdate`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Factors {
    pub time: f64,
    pub product: f64,
    pub primary: f64,
    pub secondary: f64,
    pub tertiary: f64,
}

impl Default for Factors {
    fn default() -> Self {
        Factors { time: 1.0, product: 1.0, primary: 1.0, secondary: 1.0, tertiary: 1.0 }
    }
}

/// One `{ware, amount}` entry of an engine ware amount list (amounts are 32-bit and converted
/// as signed integers).
pub type WareAmount = (WareIx, i32);

/// A production method (`U::WareProduction`): time `+0xe8`, product ware `+0xb8`, amount per
/// cycle `+0xf8`, resource lists `+0x170` (primary), `+0x1a0`, `+0x1d0`.
#[derive(Clone, Debug, PartialEq)]
pub struct Method {
    pub time: f64,
    pub product: WareIx,
    pub amount: i32,
    pub primary: Vec<WareAmount>,
    pub secondary: Vec<WareAmount>,
    pub tertiary: Vec<WareAmount>,
    /// `method+0xc0` equals the static at 0x2f61000 (research-type item: completes instead
    /// of storing products).
    pub special: bool,
}

/// One queue item (`+0x3d8` vector, 0x60 bytes each).
#[derive(Clone, Debug, PartialEq)]
pub struct Item {
    pub ware: WareIx,
    pub method: Option<Method>,
    pub time_factor: f64,
    pub product_factor: f64,
    pub primary_factor: f64,
    pub secondary_factor: f64,
    pub tertiary_factor: f64,
    /// `+0x38` cycles per item start; resources for all cycles are taken at the start.
    pub cycles: i32,
    /// `+0x3c` the item stays in the queue when finished (factories loop on their items).
    pub repeat: bool,
}

/// Production state of one module: the engine fields the transitions read and write.
#[derive(Clone, Debug, PartialEq)]
pub struct Module {
    /// Engine address of the module: the catch-up orders equal event times by it.
    pub addr: u64,
    /// `U::Destructible`-level "functional" test 0x51e8b0 (input; the economy does not change it).
    pub functional: bool,
    pub state: State,
    /// `+0x388` start of the current phase.
    pub start: f64,
    /// `+0x390` next event time (the value Step returns).
    pub next: f64,
    /// `+0x398` time the module was paused, [`UNSET`] while running.
    pub paused_since: f64,
    /// `+0x3a0` paused time accumulated in the current phase (Resume adds to it).
    pub paused_total: f64,
    /// `+0x3a8` current item index or -1.
    pub item: i32,
    /// `+0x3ac` cycle index inside the item or -1.
    pub cycle: i32,
    pub factors: Factors,
    pub items: Vec<Item>,
    /// `+0x3f8` products that did not fit into storage (count at `+0x420`).
    pub pending: Vec<WareAmount>,
}

/// What a transition needs from the container (station) that owns the module.
pub trait StationEnv {
    /// Owner is the player faction (licence / research checks apply).
    fn player_owned(&self) -> bool {
        false
    }
    /// Player licence/research check for the item's blueprint (0x7224f0); only asked when
    /// [`StationEnv::player_owned`].
    fn production_allowed(&self, _item: &Item) -> bool {
        true
    }
    /// 0x7602d0: efficiency factors of `module.items[item]` at time `t`.
    fn efficiency(&mut self, module: &Module, item: usize, t: f64) -> Factors;
    /// 0x424b90 && 0x356010: the products of one cycle can be stored.
    fn can_store(&mut self, products: &[WareAmount]) -> bool;
    /// 0x356090: the resources are present.
    fn has_resources(&mut self, resources: &[WareAmount]) -> bool;
    /// 0x3fb9f0 + 0x3ff1c0(reason 0x11): remove the resources of an item start.
    fn consume(&mut self, resources: &[WareAmount], t: f64);
    /// 0x3faff0 + 0x3ff1c0(reason 0x12): store finished products; returns what was stored.
    fn store(&mut self, products: &[WareAmount], t: f64) -> Vec<WareAmount>;
    /// Pending-product flush in state 4 (0x4241f0/0x8f15b0/0x3faff0 path): stores what fits,
    /// returns what was stored. Default: same as [`StationEnv::store`].
    fn store_fitting(&mut self, products: &[WareAmount], t: f64) -> Vec<WareAmount> {
        self.store(products, t)
    }
    /// 0x8f0580 on the pending list with a `WareFilterByWare` of an alternative item's
    /// products (alternative-item search when storage is blocked). Semantics not verified;
    /// the default accepts.
    fn pending_accepts(&mut self, _products: &[WareAmount], _pending: &[WareAmount]) -> bool {
        true
    }
    /// 0x4070e0 (the station's trade-offer update), called with the thread's current game
    /// time, not the event time: on entering waiting-for-resources, after storing products,
    /// and at a cycle start of a non-repeating or research-type item. `reset_build_cache` is
    /// set when the caller also clears the container's `+0x931` flag and `+0x938` time
    /// (non-repeating or research-type item).
    fn refresh_offers(&mut self, _reset_build_cache: bool) {}
    /// Engine events posted by the transition (logs and shadow comparison only). They are
    /// posted *scheduled* at the thread's game time (immediate flag 0), so listeners run in
    /// the next `ProcessDueEvents`, outside the catch-up.
    fn event(&mut self, _ev: ProductionEvent, _t: f64) {}
}

/// Events posted by Step (event ctors 0x9a8650 on item start, 0x9a88c0 on item finish) to the
/// module, its container, a related object (0x493390) and, for the player's faction, the
/// player entity.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProductionEvent {
    ItemStarted {
        item: i32,
    },
    ItemFinished {
        item: i32,
    },
    /// Research-type item finished for the player (0x154840).
    ResearchFinished {
        item: i32,
    },
}

/// Engine random draws used by the production code.
pub trait ProductionRng {
    /// `Random::FloatState(max)` 0x14ddc90.
    fn float(&mut self, max: f32) -> f32;
    /// `Random::I32(n)` 0x14ddf70 (index in `0..n`).
    fn i32_below(&mut self, n: i32) -> i32;
    /// Called by [`update_production`] before each Step of the module at `addr` (no engine
    /// counterpart; lets a replay attribute recorded draws to modules).
    fn begin(&mut self, _addr: u64) {}
}

fn add_amount(out: &mut Vec<WareAmount>, w: WareIx, amount: i32) {
    if let Some(e) = out.iter_mut().find(|e| e.0 == w) {
        e.1 = e.1.wrapping_add(amount);
    } else {
        out.push((w, amount));
    }
}

/// `1e-4f` as stored at 0x2cbe0ac (`0x38d1b717`).
const MULT_EPSILON: f32 = 1e-4_f32;

/// Replace-mode scaling (0x151930 with `replace`, i.e. copy + 0x324970 + 0x125bc0; inlined
/// in 0x761320): empty when `|factor| < 1e-7` or `|mult| < 1e-4f`, else per entry
/// `cvttss2si((float)cvttsd2si((double)amount * factor) * mult)`.
pub fn scale_replace(list: &[WareAmount], factor: f64, mult: f32) -> Vec<WareAmount> {
    if !(factor.abs() >= 1e-7) {
        return Vec::new();
    }
    let mut out: Vec<WareAmount> = list.iter().map(|&(w, a)| (w, cvttsd2si(a as f64 * factor))).collect();
    if !(mult.abs() >= MULT_EPSILON) {
        return Vec::new();
    }
    for e in out.iter_mut() {
        e.1 = cvttss2si((e.1 as f32) * mult);
    }
    out
}

/// Append-mode scaling (0x151930 without `replace`): per entry the same arithmetic without
/// the thresholds, merged into `out` by ware (0x8f12e0).
pub fn scale_append(out: &mut Vec<WareAmount>, list: &[WareAmount], factor: f64, mult: f32) {
    for &(w, a) in list {
        let v = cvttss2si((cvttsd2si(a as f64 * factor) as f32) * mult);
        add_amount(out, w, v);
    }
}

/// Products of one cycle (0xe37370): `cvttss2si((float)cvttsd2si((double)amount *
/// (factors.product * item.product_factor)) * mult)` of the method's product ware.
pub fn cycle_products(item: &Item, f: &Factors, mult: f32) -> Vec<WareAmount> {
    let Some(m) = &item.method else { return Vec::new() };
    let a = cvttsd2si((m.amount as f64) * (f.product * item.product_factor));
    let a = cvttss2si(a as f32); // the engine round-trips through float once more
    vec![(m.product, cvttss2si((a as f32) * mult))]
}

/// Primary resources an item start needs (state 6 check and PrepareUpdate).
pub fn required_primary(item: &Item, f: &Factors) -> Vec<WareAmount> {
    match &item.method {
        Some(m) => scale_replace(&m.primary, item.primary_factor * f.primary, item.cycles as f32),
        None => Vec::new(),
    }
}

/// All resources taken at an item start (0xe37400 with `replace` for the primary list).
pub fn item_resources(item: &Item, f: &Factors) -> Vec<WareAmount> {
    let Some(m) = &item.method else { return Vec::new() };
    let c = item.cycles as f32;
    let mut out = scale_replace(&m.primary, f.primary * item.primary_factor, c);
    scale_append(&mut out, &m.secondary, f.secondary * item.secondary_factor, c);
    scale_append(&mut out, &m.tertiary, f.tertiary * item.tertiary_factor, c);
    out
}

/// Engine retry delay `max(spread, 5.0f) + FloatState(spread)` in f32, widened to f64.
fn retry_delay(spread: f32, rng: &mut dyn ProductionRng) -> f64 {
    let draw = rng.float(spread);
    let base = if spread > 5.0f32 { spread } else { 5.0f32 };
    (draw + base) as f64
}

impl Module {
    fn reset_phase(&mut self, t: f64) {
        self.start = t;
        self.next = t;
        self.paused_since = UNSET;
        self.paused_total = 0.0;
    }

    fn item_index(&self) -> Option<usize> {
        usize::try_from(self.item).ok().filter(|&i| i < self.items.len())
    }

    /// `U::ProductionModule::Step` 0x761720: one transition at event time `t`.
    ///
    /// `spread` is `0.5f * (number of modules due when the catch-up started)`.
    pub fn step(&mut self, t: f64, spread: f32, env: &mut dyn StationEnv, rng: &mut dyn ProductionRng) -> f64 {
        if is_set(self.paused_since) {
            return UNSET;
        }
        if self.items.is_empty() && self.state != State::Empty {
            self.next = UNSET;
            self.paused_since = UNSET;
            self.paused_total = 0.0;
            self.become_empty(t);
            return self.next;
        }
        match self.state {
            State::Empty => {
                if self.items.is_empty() {
                    self.next = UNSET;
                } else {
                    self.start = t;
                    self.paused_since = UNSET;
                    self.paused_total = 0.0;
                    self.state = State::QueueNotStarted;
                    self.next = t;
                }
            }
            State::QueueNotStarted => {
                if t >= self.next {
                    self.reset_phase(t);
                    self.state = State::QueueStarted;
                }
            }
            State::QueueStarted => {
                self.reset_phase(t);
                self.item = match self.items.last() {
                    None => -1,
                    Some(last) if !last.repeat => 0,
                    Some(_) => rng.i32_below(self.items.len() as i32),
                };
                self.cycle = -1;
                self.state = State::ChoosingItem;
            }
            State::ChoosingItem => self.choose_item(t, spread, env, rng),
            State::ItemStarted => self.item_started(t, spread, env, rng),
            State::WaitingForResources => self.wait_resources(t, spread, env, rng),
            State::CycleStarted => self.cycle_started(t, spread, env, rng),
            State::Producing => {
                if t >= self.next {
                    self.state = State::WaitingForStorage;
                    self.finish_cycle(t, env);
                }
            }
            State::WaitingForStorage => self.finish_cycle(t, env),
            State::CycleFinished => {
                self.start = t;
                self.next = t;
                self.paused_since = UNSET;
                self.cycle += 1;
                let cycles = self.item_index().map_or(0, |i| self.items[i].cycles);
                if self.cycle < cycles {
                    self.state = State::CycleStarted;
                } else {
                    self.cycle = -1;
                    self.state = State::ItemFinished;
                    env.event(ProductionEvent::ItemFinished { item: self.item }, t);
                }
            }
            State::ItemFinished => self.item_finished(t),
            State::QueueFinished => {
                self.next = UNSET;
                self.paused_since = UNSET;
                self.become_empty(t);
            }
            State::Paused => {}
        }
        self.next
    }

    fn become_empty(&mut self, t: f64) {
        self.state = State::Empty;
        self.item = -1;
        self.cycle = -1;
        self.start = t;
    }

    fn choose_item(&mut self, t: f64, spread: f32, env: &mut dyn StationEnv, rng: &mut dyn ProductionRng) {
        if !self.pending.is_empty() {
            let pending = self.pending.clone();
            let stored = env.store_fitting(&pending, t);
            for (w, a) in stored {
                add_amount(&mut self.pending, w, a.wrapping_neg());
            }
            self.pending.retain(|e| e.1 != 0);
        }
        let Some(idx) = self.item_index() else {
            // The engine indexes the item vector without a bound check here; an invalid
            // index is outside the modelled behaviour.
            self.next = UNSET;
            return;
        };
        self.factors = env.efficiency(self, idx, t);
        let products = cycle_products(&self.items[idx], &self.factors, 1.0);
        if env.can_store(&products) {
            self.item_started(t, spread, env, rng);
            return;
        }
        // Storage blocked: with more than one item and a repeating current item, look for
        // another storable item, starting at the current one and wrapping.
        let n = self.items.len();
        let mut found = None;
        if n > 1 && self.items[idx].repeat {
            let mut cand = idx;
            for _ in 0..n {
                let f = if cand == idx { self.factors } else { env.efficiency(self, cand, t) };
                let p = cycle_products(&self.items[cand], &f, 1.0);
                if env.can_store(&p) && env.pending_accepts(&p, &self.pending) {
                    found = Some(cand);
                    break;
                }
                cand = if cand + 1 < n { cand + 1 } else { 0 };
            }
        }
        match found {
            Some(c) => {
                let draw = rng.float(4.0);
                self.next += (draw as f64) + 1.0;
                self.item = c as i32;
                self.state = State::ItemStarted;
            }
            None => {
                self.next += retry_delay(spread, rng);
                self.paused_since = UNSET;
            }
        }
    }

    fn item_started(&mut self, t: f64, spread: f32, env: &mut dyn StationEnv, rng: &mut dyn ProductionRng) {
        self.reset_phase(t);
        self.cycle = 0;
        if self.item >= 0 {
            env.event(ProductionEvent::ItemStarted { item: self.item }, t);
        }
        self.wait_resources(t, spread, env, rng);
    }

    fn wait_resources(&mut self, t: f64, spread: f32, env: &mut dyn StationEnv, rng: &mut dyn ProductionRng) {
        let Some(idx) = self.item_index() else {
            self.next = UNSET;
            return;
        };
        self.factors = env.efficiency(self, idx, t);
        let item = &self.items[idx];
        let allowed = !env.player_owned() || env.production_allowed(item);
        let req = required_primary(item, &self.factors);
        let reset = !item.repeat || item.method.as_ref().is_some_and(|m| m.special);
        if allowed && env.has_resources(&req) {
            let used = item_resources(item, &self.factors);
            env.consume(&used, t);
            self.reset_phase(t);
            self.state = State::CycleStarted;
        } else if self.state == State::WaitingForResources {
            self.next += retry_delay(spread, rng);
            self.paused_since = UNSET;
            self.paused_total = 0.0;
        } else {
            self.reset_phase(t);
            self.state = State::WaitingForResources;
            env.refresh_offers(reset);
        }
    }

    fn cycle_started(&mut self, t: f64, spread: f32, env: &mut dyn StationEnv, rng: &mut dyn ProductionRng) {
        self.start = t;
        let current = self.item_index().map(|i| &self.items[i]);
        let dur = match current {
            Some(Item { method: Some(m), time_factor, .. }) => time_factor * self.factors.time * m.time,
            _ => 0.0,
        };
        let refresh = current.is_some_and(|i| !i.repeat || i.method.as_ref().is_some_and(|m| m.special));
        self.next = dur + t;
        if !(self.next > t) {
            let draw = rng.float(spread);
            let base = if spread > 5.0f32 { spread } else { 5.0f32 }; // maxss xmm8, 5.0
            self.next += (base + draw) as f64;
        }
        self.paused_since = UNSET;
        self.paused_total = 0.0;
        self.state = State::Producing;
        if refresh {
            env.refresh_offers(true);
        }
    }

    fn finish_cycle(&mut self, t: f64, env: &mut dyn StationEnv) {
        if let Some(idx) = self.item_index() {
            let item = &self.items[idx];
            let products = cycle_products(item, &self.factors, 1.0);
            let special = item.method.as_ref().is_some_and(|m| m.special);
            if special {
                if env.player_owned() {
                    env.event(ProductionEvent::ResearchFinished { item: self.item }, t);
                }
            } else if env.can_store(&products) {
                let stored = env.store(&products, t);
                // 0x8f32f0 on the pending list with the stored list (unverified: modelled as
                // removing stored amounts that are also pending).
                for (w, a) in stored {
                    if let Some(e) = self.pending.iter_mut().find(|e| e.0 == w) {
                        e.1 = e.1.saturating_sub(a).max(0);
                    }
                }
                self.pending.retain(|e| e.1 != 0);
                env.refresh_offers(false);
            } else {
                for &(w, a) in &products {
                    add_amount(&mut self.pending, w, a);
                }
            }
        }
        self.start = t;
        self.next = t;
        self.paused_since = UNSET;
        self.state = State::CycleFinished;
    }

    fn item_finished(&mut self, t: f64) {
        self.start = t;
        self.next = t;
        self.paused_since = UNSET;
        let n = self.items.len() as i32;
        if self.item == -1 {
            self.item = if n > 0 { 0 } else { -1 };
            self.state = State::ChoosingItem;
            return;
        }
        let idx = self.item as usize;
        if self.items.get(idx).is_some_and(|i| i.repeat) {
            let next = self.item + 1;
            self.item = if next < n {
                next
            } else if n > 0 {
                0
            } else {
                -1
            };
            self.state = State::ChoosingItem;
        } else {
            if idx < self.items.len() {
                self.items.remove(idx);
            }
            let n = self.items.len() as i32;
            if self.item > 0 && self.item >= n {
                self.item = -1;
            }
            self.state = if n == 0 { State::QueueFinished } else { State::ChoosingItem };
        }
    }

    /// `U::ProductionModule::PrepareUpdate` 0x761320: the module's time in the catch-up
    /// queue. A module waiting for resources whose primary resources are now present is due
    /// at `now` (the call time, not a retry time).
    pub fn prepare(&mut self, now: f64, env: &mut dyn StationEnv) -> f64 {
        if is_set(self.paused_since) {
            return UNSET;
        }
        if self.items.is_empty() && self.state != State::Empty {
            return now;
        }
        match self.state {
            State::Empty => {
                if self.items.is_empty() {
                    self.next
                } else {
                    now
                }
            }
            State::ChoosingItem | State::WaitingForResources => {
                let Some(idx) = self.item_index() else { return self.next };
                self.factors = env.efficiency(self, idx, now);
                if self.state == State::WaitingForResources {
                    let item = &self.items[idx];
                    if !env.player_owned() || env.production_allowed(item) {
                        let req = required_primary(item, &self.factors);
                        if env.has_resources(&req) {
                            return now;
                        }
                    }
                }
                self.next
            }
            State::WaitingForStorage => now,
            _ => self.next,
        }
    }
}

/// Container fields the catch-up maintains (`+0xa18` last update, `+0xa28`, `+0xa30`).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ContainerTimes {
    /// `+0xa18`: time of the last effective UpdateProduction.
    pub last_update: f64,
    /// `+0xa28`: lowered to a producing module's next time when no module was producing at
    /// the start of the catch-up and `+0xa4c` is clear (meaning not settled; also read by
    /// `GetWorkForceInfo`).
    pub a28: f64,
    /// `+0xa30`: raised to the latest next time of a producing module.
    pub a30: f64,
    /// `+0xa4c` flag (input).
    pub a4c: bool,
}

impl Default for ContainerTimes {
    fn default() -> Self {
        ContainerTimes { last_update: UNSET, a28: UNSET, a30: UNSET, a4c: false }
    }
}

/// Result of one catch-up for logs and shadow comparison.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct CatchUp {
    /// `(module index, event time, state after)` for every Step call, in call order.
    pub steps: Vec<(usize, f64, State)>,
    /// Retry spread used by this catch-up.
    pub spread: f32,
}

/// `U::Container::UpdateProduction` 0x41a910 over `modules` (the container's operational,
/// unpaused production modules in child order). Returns `None` when the call is a no-op
/// because the container is already up to date (`last_update >= now` and not forced).
pub fn update_production(
    c: &mut ContainerTimes,
    now: f64,
    force: bool,
    modules: &mut [Module],
    env: &mut dyn StationEnv,
    rng: &mut dyn ProductionRng,
) -> Option<CatchUp> {
    if !force && is_set(c.last_update) && c.last_update >= now {
        return None;
    }
    c.last_update = now;
    let mut log = CatchUp::default();
    let mut any_producing = false;
    // Queue sorted descending by (time, address); the back is processed first.
    let mut queue: Vec<(f64, u64, usize)> = Vec::new();
    for (i, m) in modules.iter_mut().enumerate() {
        if is_set(m.paused_since) {
            continue;
        }
        if m.functional && m.state == State::Producing {
            any_producing = true;
        }
        let t = m.prepare(now, env);
        if is_set(t) && !(t > now) {
            let pos = queue.partition_point(|q| greater(q, t, m.addr));
            queue.insert(pos, (t, m.addr, i));
        }
    }
    let spread = (queue.len() as f32) * 0.5f32;
    log.spread = spread;
    while let Some(&(t, addr, i)) = queue.last() {
        let mut t2;
        loop {
            rng.begin(addr);
            t2 = modules[i].step(t, spread, env, rng);
            log.steps.push((i, t, modules[i].state));
            if !(is_set(t2) && t2 == t) {
                break;
            }
        }
        // The engine re-reads the module from the queue's back slot after re-queueing: when
        // the stepped module moved forward, the bookkeeping below looks at the module that
        // now occupies the back slot (0x41b0bb `mov rbx, [rbx-0x10]` on the old end pointer).
        let checked = if is_set(t2) && !(t2 > now) {
            if requeue(&mut queue, t2) {
                queue.last().map_or(i, |e| e.2)
            } else {
                i
            }
        } else {
            queue.pop();
            i
        };
        let m = &modules[checked];
        if !is_set(m.paused_since) && m.functional && m.state == State::Producing {
            if !(is_set(c.a30) && c.a30 >= t2) {
                c.a30 = t2;
            }
            if !any_producing && !c.a4c && !(t2 >= c.a28) {
                c.a28 = t2;
            }
        }
    }
    Some(log)
}

/// `(q.time, q.addr) > (t, addr)` with the engine's comparison (`ucomisd` equality first).
fn greater(q: &(f64, u64, usize), t: f64, addr: u64) -> bool {
    if q.0 == t {
        q.1 > addr
    } else {
        q.0 > t
    }
}

/// Re-queues the back element (just stepped, new time `t2`) like 0x41af92..0x41b0b6: it stays
/// at the back when the element before it has a time `>= t2` (the engine checks only the time
/// there, not the address tie-break); otherwise it moves to its binary-search position.
/// Returns `true` when it moved.
pub fn requeue(queue: &mut [(f64, u64, usize)], t2: f64) -> bool {
    let n = queue.len();
    let last = n - 1;
    queue[last].0 = t2;
    if n <= 1 || queue[n - 2].0 >= t2 {
        return false;
    }
    let e = queue[last];
    let pos = queue[..last].partition_point(|q| greater(q, e.0, e.1));
    if pos == last {
        return false;
    }
    queue[pos..].rotate_right(1);
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Engine LCG as described by ecs-core (interfaces.md, x4mp_sim::rng).
    struct Lcg(u64);
    impl Lcg {
        fn step(&mut self) -> u64 {
            self.0 = self.0.wrapping_mul(0x5851_F42D_4C95_7F2D).wrapping_add(0x1405_7B7E_F767_814F).rotate_right(30);
            self.0
        }
    }
    impl ProductionRng for Lcg {
        fn float(&mut self, max: f32) -> f32 {
            if !(max >= 0.0) {
                return 0.0;
            }
            ((self.step() as u32) as f32) * 2.328_306_4e-10_f32 * max
        }
        fn i32_below(&mut self, n: i32) -> i32 {
            if n <= 0 {
                return 0;
            }
            (self.step() % n as u64) as i32
        }
    }

    /// Unlimited storage, plain cargo.
    #[derive(Default)]
    struct Env {
        cargo: Vec<WareAmount>,
        stored: Vec<(f64, WareAmount)>,
    }
    impl Env {
        fn amount(&self, w: WareIx) -> i32 {
            self.cargo.iter().find(|e| e.0 == w).map_or(0, |e| e.1)
        }
    }
    impl StationEnv for Env {
        fn efficiency(&mut self, _m: &Module, _i: usize, _t: f64) -> Factors {
            Factors::default()
        }
        fn can_store(&mut self, _p: &[WareAmount]) -> bool {
            true
        }
        fn has_resources(&mut self, r: &[WareAmount]) -> bool {
            r.iter().all(|&(w, a)| self.amount(w) >= a)
        }
        fn consume(&mut self, r: &[WareAmount], _t: f64) {
            for &(w, a) in r {
                add_amount(&mut self.cargo, w, -a);
            }
        }
        fn store(&mut self, p: &[WareAmount], t: f64) -> Vec<WareAmount> {
            for &(w, a) in p {
                add_amount(&mut self.cargo, w, a);
                self.stored.push((t, (w, a)));
            }
            p.to_vec()
        }
    }

    const ECELLS: WareIx = WareIx(0);
    const ORE: WareIx = WareIx(1);
    const METAL: WareIx = WareIx(2);

    fn module(addr: u64, method: Method) -> Module {
        Module {
            addr,
            functional: true,
            state: State::Empty,
            start: UNSET,
            next: UNSET,
            paused_since: UNSET,
            paused_total: 0.0,
            item: -1,
            cycle: -1,
            factors: Factors::default(),
            items: vec![Item {
                ware: method.product,
                method: Some(method),
                time_factor: 1.0,
                product_factor: 1.0,
                primary_factor: 1.0,
                secondary_factor: 1.0,
                tertiary_factor: 1.0,
                cycles: 1,
                repeat: true,
            }],
            pending: Vec::new(),
        }
    }

    fn refinery() -> Method {
        Method {
            time: 150.0,
            product: METAL,
            amount: 88,
            primary: vec![(ECELLS, 90), (ORE, 240)],
            secondary: vec![],
            tertiary: vec![],
            special: false,
        }
    }

    #[test]
    fn cycles_complete_at_their_own_event_times() {
        let mut env = Env { cargo: vec![(ECELLS, 900), (ORE, 2400)], ..Env::default() };
        let mut rng = Lcg(1);
        let mut mods = vec![module(0x1000, refinery())];
        let mut c = ContainerTimes::default();
        update_production(&mut c, 10.0, false, &mut mods, &mut env, &mut rng).unwrap();
        assert_eq!(mods[0].state, State::Producing);
        assert_eq!((mods[0].start, mods[0].next), (10.0, 160.0));
        // A late catch-up at t=1000 completes the cycles at 160, 310, 460, ... exactly.
        update_production(&mut c, 1000.0, false, &mut mods, &mut env, &mut rng).unwrap();
        let times: Vec<f64> = env.stored.iter().map(|s| s.0).collect();
        assert_eq!(times, vec![160.0, 310.0, 460.0, 610.0, 760.0, 910.0]);
        assert_eq!(mods[0].next, 1060.0);
        assert_eq!(env.amount(METAL), 6 * 88);
        assert_eq!(env.amount(ORE), 2400 - 7 * 240);
        assert!(update_production(&mut c, 1000.0, false, &mut mods, &mut env, &mut rng).is_none());
    }

    #[test]
    fn lazy_and_eager_catch_up_agree_without_external_changes() {
        let run = |calls: &[f64]| {
            let mut env = Env { cargo: vec![(ECELLS, 9000), (ORE, 24000)], ..Env::default() };
            let mut rng = Lcg(7);
            let mut mods = vec![module(0x2000, refinery()), module(0x1000, refinery())];
            let mut c = ContainerTimes::default();
            for &t in calls {
                update_production(&mut c, t, false, &mut mods, &mut env, &mut rng);
            }
            (mods, env.cargo, env.stored)
        };
        let eager: Vec<f64> = (0..=120).map(|k| k as f64 * 25.0).collect();
        assert_eq!(run(&[0.0, 3000.0]), run(&eager));
    }

    #[test]
    fn waiting_for_resources_retries_with_engine_delay() {
        let mut env = Env { cargo: vec![(ECELLS, 10), (ORE, 0)], ..Env::default() };
        let mut rng = Lcg(3);
        let mut mods = vec![module(0x1000, refinery())];
        let mut c = ContainerTimes::default();
        update_production(&mut c, 50.0, false, &mut mods, &mut env, &mut rng).unwrap();
        assert_eq!(mods[0].state, State::WaitingForResources);
        // spread = 0.5 * 1 module; delay = max(0.5, 5) + U[0, 0.5).
        let d = mods[0].next - 50.0;
        assert!((5.0..5.5).contains(&d), "delay {d}");
        // Resources arrive; the next catch-up wakes the module at the call time.
        env.cargo = vec![(ECELLS, 90), (ORE, 240)];
        update_production(&mut c, 52.0, false, &mut mods, &mut env, &mut rng).unwrap();
        assert_eq!(mods[0].state, State::Producing);
        assert_eq!((mods[0].start, mods[0].next), (52.0, 202.0));
    }

    #[test]
    fn amount_rounding_truncates_like_the_engine() {
        let it = &module(1, refinery()).items[0];
        let f = Factors { product: 1.37, ..Factors::default() };
        assert_eq!(cycle_products(it, &f, 1.0), vec![(METAL, 120)]); // 120.56
        let f = Factors { primary: 0.95, ..Factors::default() };
        assert_eq!(required_primary(it, &f), vec![(ECELLS, 85), (ORE, 228)]);
        let f = Factors { primary: 5e-8, ..Factors::default() };
        assert!(required_primary(it, &f).is_empty());
        assert_eq!(cvttsd2si(f64::NAN), i32::MIN);
        assert_eq!(cvttsd2si(3e9), i32::MIN);
        assert_eq!(cvttss2si(-2.5), -2);
        assert_eq!(MULT_EPSILON.to_bits(), 0x38d1_b717);
    }

    #[test]
    fn queue_orders_equal_times_by_address_with_the_engine_quirk() {
        let mut q: Vec<(f64, u64, usize)> = Vec::new();
        for &(t, a, i) in &[(5.0, 0x30, 0), (5.0, 0x10, 1), (4.0, 0x90, 2), (5.0, 0x20, 3)] {
            let pos = q.partition_point(|e| greater(e, t, a));
            q.insert(pos, (t, a, i));
        }
        let order: Vec<usize> = q.iter().rev().map(|e| e.2).collect();
        assert_eq!(order, vec![2, 1, 3, 0]);
        // Back element (index 2) re-queued at time 5.0: the element before it has 5.0 >= 5.0,
        // so it stays at the back although its address 0x90 sorts after 0x10..0x30.
        requeue(&mut q, 5.0);
        assert_eq!(q.last().unwrap().2, 2);
        // Re-queued later than everything: moves to the front.
        requeue(&mut q, 9.0);
        assert_eq!(q[0].2, 2);
    }
}

// Hashing and field-wise comparison for x4mp-sim components and shadow compare (floats are
// compared and hashed by their bits).
impl x4mp_sim::hash::StableHash for State {
    fn stable_hash(&self, h: &mut x4mp_sim::hash::Fnv64) {
        x4mp_sim::hash::StableHash::stable_hash(&(*self as i32), h);
    }
}
impl x4mp_sim::shadow::Compare for State {
    fn compare(&self, actual: &Self, c: &mut x4mp_sim::shadow::Comparator) {
        x4mp_sim::shadow::Compare::compare(&(*self as i32), &(*actual as i32), c);
    }
}
x4mp_sim::impl_fields!(Factors { time, product, primary, secondary, tertiary });
x4mp_sim::impl_fields!(Method { time, product, amount, primary, secondary, tertiary, special });
x4mp_sim::impl_fields!(Item {
    ware,
    method,
    time_factor,
    product_factor,
    primary_factor,
    secondary_factor,
    tertiary_factor,
    cycles,
    repeat
});
x4mp_sim::impl_fields!(Module {
    addr,
    functional,
    state,
    start,
    next,
    paused_since,
    paused_total,
    item,
    cycle,
    factors,
    items,
    pending
});
x4mp_sim::impl_fields!(ContainerTimes { last_update, a28, a30, a4c });
