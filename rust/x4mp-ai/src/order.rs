//! Commanding native orders: the interface the MD layer, the player UI and the faction AI use to give
//! a ship an order, and how they learn where it stands. Published in rewrite-coord/interfaces.md.
//!
//! An order is a typed [`OrderSpec`] (the script's documented `<params>`, XML defaults). It round-trips
//! with the engine's order parameters (`create_order` / `edit_order_param` name-value pairs), so the same
//! spec can be handed to the original engine (fallback) or to the native behaviour. Progress is visible
//! only through [`crate::world::Effect`]s (the same actions the script would take) and [`OrderStatus`].

use crate::orders::patrol::{Patrol, PatrolParams, Range};
use crate::runtime::{Agent, Frame, Waiting};
use crate::val::{Uid, Val};
use crate::world::{Facts, Io, Prop, Stop, R};

/// A native order and its parameters.
#[derive(Clone, Debug, PartialEq)]
pub enum OrderSpec {
    /// `Patrol` (order.fight.patrol): patrol a zone, sector or (jobs) cluster and engage hostiles.
    Patrol(PatrolParams),
}

/// Where an order stands.
#[derive(Clone, Debug, PartialEq)]
pub enum OrderStatus {
    /// No order script on the agent.
    Idle,
    /// Running: the script on top of the stack and what it waits for.
    Running { order: &'static str, top: String, waiting: Waiting },
    /// A branch is not ported: the original engine must run this order for this ship.
    Unsupported(String),
}

impl OrderSpec {
    /// Engine order id (`<order id=>`).
    pub fn id(&self) -> &'static str {
        match self {
            OrderSpec::Patrol(_) => "Patrol",
        }
    }

    /// The order's script.
    pub fn script(&self) -> &'static str {
        match self {
            OrderSpec::Patrol(_) => Patrol::NAME,
        }
    }

    /// From engine order parameters. `ship` supplies parameter defaults that read the ship
    /// (Patrol: `pursuedistance = [this.ship.maxradarrange, 1km].max`).
    pub fn from_engine(id: &str, params: &[(String, Val)], facts: &dyn Facts, ship: Uid) -> R<OrderSpec> {
        match id {
            "Patrol" => {
                let radar = facts.length(ship, Prop::MaxRadarRange);
                Ok(OrderSpec::Patrol(PatrolParams::from_params(params, radar)?))
            }
            other => Err(Stop::Unsupported { script: "x4mp_ai::order", line: 0, what: leak_free(other) }),
        }
    }

    /// Engine order parameters (every parameter, in `<params>` order).
    pub fn to_engine(&self) -> Vec<(String, Val)> {
        match self {
            OrderSpec::Patrol(p) => {
                let range = match p.range {
                    None => Val::Null,
                    Some(Range::Zone) => Val::key("class.zone"),
                    Some(Range::Sector) => Val::key("class.sector"),
                    Some(Range::Cluster) => Val::key("class.cluster"),
                };
                vec![
                    ("space".into(), Val::uid(p.space)),
                    ("range".into(), range),
                    ("pursuetargets".into(), Val::Bool(p.pursuetargets)),
                    ("pursuedistance".into(), Val::F32(p.pursuedistance)),
                    ("includeroute".into(), Val::Bool(p.includeroute)),
                    ("allowothertargets".into(), Val::Bool(p.allowothertargets)),
                    ("targetpurposes".into(), Val::List(p.targetpurposes.clone())),
                    ("stayinspace".into(), Val::Bool(p.stayinspace)),
                    ("usebasket".into(), Val::Bool(p.usebasket)),
                    ("timeout".into(), Val::F64(p.timeout)),
                    ("engageonlyonline".into(), Val::Bool(p.engageonlyonline)),
                    ("targetclasses".into(), Val::List(p.targetclasses.clone())),
                    ("stayhidden".into(), Val::Bool(p.stayhidden)),
                    ("organizesubordinates".into(), Val::Bool(p.organizesubordinates)),
                    ("checkrelation".into(), Val::Bool(p.checkrelation)),
                    ("debugchance".into(), Val::F32(p.debugchance)),
                ]
            }
        }
    }

    /// Whether the native behaviour can run this order with these parameters at all (branches that
    /// are refused at run time are reported by [`OrderStatus::Unsupported`] instead).
    pub fn native_supported(&self) -> Result<(), &'static str> {
        match self {
            OrderSpec::Patrol(p) if p.stayhidden => Err("Patrol stayhidden (ambush) is not ported"),
            OrderSpec::Patrol(p) if p.organizesubordinates => Err("Patrol organizesubordinates is not ported"),
            OrderSpec::Patrol(_) => Ok(()),
        }
    }
}

fn leak_free(id: &str) -> &'static str {
    match id {
        "Escort" => "Escort is not ported",
        "TradeRoutine" => "TradeRoutine is not ported",
        "MiningRoutine" => "MiningRoutine is not ported",
        _ => "order is not ported",
    }
}

impl Agent {
    /// Give the agent's ship a native order (the `run_order_script` of orders.base); runs `<init>`.
    pub fn start(&mut self, spec: &OrderSpec, ship: Uid, facts: &dyn Facts, io: &mut dyn Io) -> R<()> {
        let frame = match spec {
            OrderSpec::Patrol(p) => Frame::Patrol(Box::new(Patrol::new(p.clone(), ship))),
        };
        self.start_order(frame, facts, io)
    }

    /// Where the agent's order stands.
    pub fn status(&self) -> OrderStatus {
        if let Some(why) = &self.stopped {
            return OrderStatus::Unsupported(why.clone());
        }
        match (self.stack.first(), self.stack.last(), self.waiting.last()) {
            (Some(bottom), Some(top), Some(w)) => {
                let order = match bottom {
                    Frame::Patrol(_) => "Patrol",
                    Frame::Seek(_) => "move.seekenemies",
                    Frame::TargetSel(_) => "lib.target.selection",
                    Frame::Opaque { .. } => "opaque",
                };
                OrderStatus::Running { order, top: top.name().to_string(), waiting: *w }
            }
            _ => OrderStatus::Idle,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::replay::FactStore;
    use crate::rng::Stream;
    use crate::world::{Answer, Effect, Query};

    struct Sink(Vec<Effect>);
    impl Io for Sink {
        fn roll(&mut self, _: Uid, _: u32, _: f32, _: bool) -> R<()> {
            Ok(())
        }
        fn rnd(&mut self, _: Uid, _: u32, _: f64) -> R<()> {
            Ok(())
        }
        fn query(&mut self, _: Uid, _: u32, q: &Query, _: &mut Stream) -> R<Answer> {
            Err(Stop::Mismatch(format!("no query expected: {q:?}")))
        }
        fn effect(&mut self, _: Uid, _: u32, e: &Effect, _: &mut Stream) -> R<()> {
            self.0.push(e.clone());
            Ok(())
        }
    }

    #[test]
    fn engine_params_round_trip() {
        let mut f = FactStore::default();
        f.set(Uid(0x200), Prop::MaxRadarRange, Val::F32(12000.0));
        let ps = vec![("range".to_string(), Val::key("class.sector")), ("timeout".to_string(), Val::F64(900.0))];
        let spec = OrderSpec::from_engine("Patrol", &ps, &f, Uid(0x200)).unwrap();
        let OrderSpec::Patrol(p) = &spec;
        assert_eq!(p.pursuedistance, 12000.0); // the ship-dependent default
        assert_eq!(p.range, Some(Range::Sector));
        let back = OrderSpec::from_engine("Patrol", &spec.to_engine(), &f, Uid(0x200)).unwrap();
        assert_eq!(back, spec);
        assert_eq!(spec.id(), "Patrol");
        assert_eq!(spec.script(), "order.fight.patrol");
        assert!(spec.native_supported().is_ok());
        assert!(OrderSpec::from_engine("Escort", &[], &f, Uid(0x200)).is_err());
    }

    #[test]
    fn start_runs_init_and_reports_status() {
        let f = FactStore::default();
        let mut a = Agent::new(Uid(0x100), 1);
        assert_eq!(a.status(), OrderStatus::Idle);
        let spec = OrderSpec::Patrol(PatrolParams::from_params(&[], 1000.0).unwrap());
        let mut sink = Sink(Vec::new());
        a.start(&spec, Uid(0x200), &f, &mut sink).unwrap();
        assert_eq!(sink.0.len(), 2); // set_command, set_command_action
        assert_eq!(
            a.status(),
            OrderStatus::Running { order: "Patrol", top: "order.fight.patrol".into(), waiting: Waiting::Start }
        );
    }
}
