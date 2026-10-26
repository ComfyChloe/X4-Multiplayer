//! Deterministic cross-sector message bus and structural commands.
//!
//! During a parallel phase each sector job writes into its own [`Outbox`]: messages and
//! structural commands (spawn, despawn, migrate) in emission order, numbered by a per-sector
//! sequence. At the barrier the outboxes are drained in [`SectorIx`] order (= engine tree
//! order), each in emission order. That total order equals the order a sequential walk over
//! the sectors would have produced, independent of thread count and scheduling:
//!
//! 1. structural commands are applied in that order, then every world is committed;
//! 2. messages are routed in that order. Every message is addressed to a **system** (by
//!    default the sending system; [`Outbox::send_to`] names another one) and is delivered to
//!    that system's next run after the barrier: `Sector` and `Entity` targets land in the
//!    target world's inbox for that system (an entity target follows a migration applied in
//!    the same barrier), `Barrier` targets are handed to that system's serial
//!    [`crate::sched::System::barrier`] hook. A system therefore receives its own messages in
//!    the next frame, and messages from an earlier system of the same frame in this frame.
//!
//! **Order keys.** Tree order is the original order of the component walk. Where the
//! original sequential order is something else (for example the AI director's wake order),
//! a job calls [`Outbox::set_order`] before emitting the effects of one unit of original
//! work; the barrier then uses the total order (order key, sector, emission sequence).
//! Keys default to 0, which gives plain (sector, emission) order.

use std::any::Any;
use std::fmt;

use crate::column::Bundle;
use crate::id::{EntityId, OrderKey, SectorIx, UniverseId};

/// Any message payload.
pub trait Message: Any + Send + Sync + fmt::Debug {}
impl<T: Any + Send + Sync + fmt::Debug> Message for T {}

trait Payload: Send + Sync {
    fn as_any(&self) -> &dyn Any;
    fn into_any(self: Box<Self>) -> Box<dyn Any + Send + Sync>;
    fn type_name(&self) -> &'static str;
    fn debug(&self) -> String;
}

impl<T: Message> Payload for T {
    fn as_any(&self) -> &dyn Any {
        self
    }
    fn into_any(self: Box<Self>) -> Box<dyn Any + Send + Sync> {
        self
    }
    fn type_name(&self) -> &'static str {
        std::any::type_name::<T>()
    }
    fn debug(&self) -> String {
        format!("{self:?}")
    }
}

/// Message destination (within the addressed system).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Target {
    /// The sector's world.
    Sector(SectorIx),
    /// The sector the entity is in after this barrier's structural commands (dropped and
    /// counted if the entity no longer exists).
    Entity(EntityId),
    /// The addressed system's serial barrier hook, in total order.
    Barrier,
}

/// Kind of a buffered engine event operation.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[repr(u8)]
pub enum EngineOpKind {
    Post = 0,
    Cancel = 1,
}

/// One buffered engine event operation, field for field the op record of the walk agent's
/// stage-1 buffer (`walkpar.h`: src UniverseID, type, time, imm, task, seq; the event object
/// itself stays on the engine side). Used when a Rust system has to emit or replay engine
/// events through the barrier (rewrite-coord Q8).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct EngineOp {
    pub kind: EngineOpKind,
    pub src: UniverseId,
    pub event_type: u32,
    /// Event time (f64 bits, TLS game time domain).
    pub time_bits: u64,
    pub imm: bool,
    /// Task key of the poster (sector or bucket), 0 = no scope.
    pub task: u64,
    /// Post order inside the task.
    pub seq: u32,
}

/// A routed message.
pub struct Envelope {
    pub from: SectorIx,
    pub seq: u32,
    /// Order key (see the module docs); 0 unless the sender set one.
    pub order: u64,
    pub to: Target,
    /// Name of the system the message is addressed to.
    pub system: &'static str,
    payload: Box<dyn Payload>,
}

impl Envelope {
    pub fn get<M: Message>(&self) -> Option<&M> {
        self.payload.as_any().downcast_ref::<M>()
    }

    pub fn is<M: Message>(&self) -> bool {
        self.payload.as_any().is::<M>()
    }

    /// Takes the payload out (or gives the envelope back when it is not an `M`).
    pub fn take<M: Message>(self) -> Result<M, Envelope> {
        if !self.is::<M>() {
            return Err(self);
        }
        let any = self.payload.into_any();
        Ok(*any.downcast::<M>().unwrap_or_else(|_| unreachable!("type checked above")))
    }

    pub fn type_name(&self) -> &'static str {
        self.payload.type_name()
    }

    /// `from#seq -> system:target type payload` (message logs, determinism tests).
    pub fn describe(&self) -> String {
        format!(
            "{}#{} -> {}:{:?} {} {}",
            self.from.0,
            self.seq,
            self.system,
            self.to,
            self.type_name(),
            self.payload.debug()
        )
    }
}

impl fmt::Debug for Envelope {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.describe())
    }
}

pub(crate) enum Command {
    Spawn { sector: SectorIx, uid: UniverseId, key: Option<OrderKey>, bundle: Bundle },
    Despawn { entity: EntityId },
    Migrate { entity: EntityId, to: SectorIx, key: Option<OrderKey> },
}

impl fmt::Debug for Command {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Command::Spawn { sector, uid, key, bundle } => {
                write!(f, "spawn {uid} in {sector:?} key={key:?} {bundle:?}")
            }
            Command::Despawn { entity } => write!(f, "despawn {entity:?}"),
            Command::Migrate { entity, to, key } => write!(f, "migrate {entity:?} to {to:?} key={key:?}"),
        }
    }
}

pub(crate) enum Item {
    Msg(Envelope),
    Cmd { seq: u32, order: u64, cmd: Command },
}

impl Item {
    pub(crate) fn sort_key(&self, sector: u32) -> (u64, u32, u32) {
        match self {
            Item::Msg(e) => (e.order, sector, e.seq),
            Item::Cmd { seq, order, .. } => (*order, sector, *seq),
        }
    }
}

/// A sector job's output buffer (see the module docs for the ordering guarantees).
#[derive(Default)]
pub struct Outbox {
    from: SectorIx,
    seq: u32,
    order: u64,
    pub(crate) keyed: bool,
    system: &'static str,
    pub(crate) items: Vec<Item>,
}

impl Outbox {
    pub(crate) fn new(from: SectorIx, system: &'static str) -> Outbox {
        Outbox { from, seq: 0, order: 0, keyed: false, system, items: Vec::new() }
    }

    pub(crate) fn reset(&mut self, from: SectorIx, system: &'static str) {
        self.from = from;
        self.seq = 0;
        self.order = 0;
        self.keyed = false;
        self.system = system;
        self.items.clear();
    }

    /// Sets the order key of everything emitted from now on (see the module docs).
    pub fn set_order(&mut self, key: u64) {
        self.order = key;
        self.keyed |= key != 0;
    }

    fn next_seq(&mut self) -> u32 {
        let s = self.seq;
        self.seq = self.seq.checked_add(1).expect("more than 2^32 outbox items in one phase");
        s
    }

    /// Sends a message to the sending system's next run (or its barrier hook).
    pub fn send<M: Message>(&mut self, to: Target, msg: M) {
        let system = self.system;
        self.send_to(system, to, msg);
    }

    /// Sends a message to another system's next run after this barrier.
    pub fn send_to<M: Message>(&mut self, system: &'static str, to: Target, msg: M) {
        let seq = self.next_seq();
        let order = self.order;
        self.items.push(Item::Msg(Envelope { from: self.from, seq, order, to, system, payload: Box::new(msg) }));
    }

    /// Spawns an entity at the end of `sector`'s tree order.
    pub fn spawn(&mut self, sector: SectorIx, uid: UniverseId, bundle: Bundle) {
        let seq = self.next_seq();
        self.items.push(Item::Cmd { seq, order: self.order, cmd: Command::Spawn { sector, uid, key: None, bundle } });
    }

    /// Spawns an entity at tree-order position `key`.
    pub fn spawn_at(&mut self, sector: SectorIx, uid: UniverseId, key: OrderKey, bundle: Bundle) {
        let seq = self.next_seq();
        self.items.push(Item::Cmd {
            seq,
            order: self.order,
            cmd: Command::Spawn { sector, uid, key: Some(key), bundle },
        });
    }

    pub fn despawn(&mut self, entity: EntityId) {
        let seq = self.next_seq();
        self.items.push(Item::Cmd { seq, order: self.order, cmd: Command::Despawn { entity } });
    }

    /// Moves an entity (all components, same EntityId) to the end of `to`'s tree order.
    pub fn migrate(&mut self, entity: EntityId, to: SectorIx) {
        let seq = self.next_seq();
        self.items.push(Item::Cmd { seq, order: self.order, cmd: Command::Migrate { entity, to, key: None } });
    }

    pub fn migrate_at(&mut self, entity: EntityId, to: SectorIx, key: OrderKey) {
        let seq = self.next_seq();
        self.items.push(Item::Cmd { seq, order: self.order, cmd: Command::Migrate { entity, to, key: Some(key) } });
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Debug, PartialEq)]
    struct Hello(u32);

    #[test]
    fn envelope_typing_and_sequence() {
        let mut o = Outbox::new(SectorIx(3), "test");
        o.send(Target::Barrier, Hello(7));
        o.despawn(EntityId::from_bits(5));
        o.send(Target::Sector(SectorIx(1)), 1.5f32);
        assert_eq!(o.len(), 3);
        let mut seqs = Vec::new();
        for item in o.items.drain(..) {
            match item {
                Item::Msg(env) => {
                    seqs.push(env.seq);
                    if env.is::<Hello>() {
                        assert_eq!(env.get::<Hello>(), Some(&Hello(7)));
                        assert!(env.describe().starts_with("3#0 -> test:Barrier "), "{}", env.describe());
                        assert_eq!(env.take::<Hello>().unwrap(), Hello(7));
                    } else {
                        assert!(env.get::<Hello>().is_none());
                        let env = env.take::<Hello>().unwrap_err();
                        assert_eq!(env.take::<f32>().unwrap(), 1.5);
                    }
                }
                Item::Cmd { seq, .. } => seqs.push(seq),
            }
        }
        assert_eq!(seqs, vec![0, 1, 2]);
    }
}
