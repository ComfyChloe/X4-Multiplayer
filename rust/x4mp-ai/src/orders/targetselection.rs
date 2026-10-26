//! `lib.target.selection` (no `<attention>` variants, no blocking action: one bracket from start to
//! return). Scores candidate targets and returns the best (`<return><retval name="target">`).
//!
//! Ported: the configuration move.seekenemies uses (primarytarget and secondarytargets null, other
//! targets allowed: the commander's `$attacktarget`, then gravidar contacts), the full skip chain, the
//! priority formula and the selection. Refused before any effect (it has none) and before further
//! draws: the big-target reselection L306-374 (modular/attackable-module/capital targets need
//! find_object_component), boarding checks L155 with operations present, the learned-indestructible
//! list L134, MAD score L256. Numbers: priorities and lengths are f32 script floats (literals `10.0`,
//! `500m`); `hull + shield` is summed in f64 and cast with `( )f`. Line numbers:
//! `aiscripts/lib.target.selection.xml` (base 9.00).

use super::{p_bool, p_f32, p_list, param, seekenemies::class_of_val};
use crate::runtime::{RetVals, Wake, Yield};
use crate::val::{Uid, Val};
use crate::world::{Answer, Class, Cx, Prop, Query, Stop, R};

const SCRIPT: &str = "lib.target.selection";

fn unsupported(line: u32, what: &'static str) -> Stop {
    Stop::Unsupported { script: SCRIPT, line, what }
}

/// Call parameters (`<params>`, XML defaults).
#[derive(Clone, Debug, PartialEq)]
pub struct TsParams {
    pub primarytarget: Uid,
    pub secondarytargets: Vec<Uid>,
    pub escort: Uid,
    pub pursuedistance: f32,
    pub pursuetargets: bool,
    pub list_objectstoignore: Vec<Val>,
    pub allowothertargets: bool,
    pub checkrelation: bool,
    pub targetpurposes: Vec<Val>,
    pub targetclasses: Vec<Val>,
    pub has_radius: bool,
    pub engageonlyonline: bool,
    pub readmadscore: bool,
    pub internalorder: bool,
    pub debugchance: f32,
}

impl TsParams {
    /// From `run_script` parameters (the names of the call site).
    pub fn from_params(ps: &[(String, Val)]) -> R<TsParams> {
        let uid = |k: &str| param(ps, k).and_then(|v| v.as_uid()).unwrap_or(Uid::NULL);
        Ok(TsParams {
            primarytarget: uid("primarytarget"),
            secondarytargets: param(ps, "secondarytargets").and_then(|v| v.as_uid_list()).unwrap_or_default(),
            escort: uid("escort"),
            pursuedistance: p_f32(ps, "pursuedistance", 0.0)?,
            pursuetargets: p_bool(ps, "pursuetargets", false)?,
            list_objectstoignore: p_list(ps, "list_objectstoignore")?,
            allowothertargets: p_bool(ps, "allowothertargets", false)?,
            checkrelation: p_bool(ps, "checkrelation", false)?,
            targetpurposes: p_list(ps, "targetpurposes")?,
            targetclasses: p_list(ps, "targetclasses")?,
            has_radius: !matches!(param(ps, "radius"), None | Some(Val::Null)),
            engageonlyonline: p_bool(ps, "engageonlyonline", false)?,
            readmadscore: p_bool(ps, "readmadscore", true)?,
            internalorder: p_bool(ps, "internalorder", false)?,
            debugchance: p_f32(ps, "debugchance", 0.0)?,
        })
    }
}

#[derive(Clone, Debug)]
pub struct TargetSelection {
    pub p: TsParams,
    ship: Uid,
    done: bool,
}

fn is_any_class(f: &dyn crate::world::Facts, c: Uid, classes: &[Val]) -> bool {
    classes.iter().filter_map(class_of_val).any(|k| f.is_class(c, k))
}

fn has_class(classes: &[Val], k: &str) -> bool {
    classes.iter().any(|v| v.as_key() == Some(k))
}

impl TargetSelection {
    pub const NAME: &'static str = SCRIPT;

    pub fn new(p: TsParams, ship: Uid) -> TargetSelection {
        TargetSelection { p, ship, done: false }
    }

    /// The controlled ship.
    pub fn ship(&self) -> Uid {
        self.ship
    }

    pub fn resume(&mut self, cx: &mut Cx<'_>, wake: Wake) -> R<Yield> {
        if wake != Wake::Start || self.done {
            return Err(Stop::Mismatch(format!("{SCRIPT}: unexpected wake {}", wake.name())));
        }
        self.done = true;
        let ship = self.ship;
        let dc = self.p.debugchance;
        let mut targets: Vec<Uid> = Vec::new(); // L36
        if self.p.targetclasses.is_empty() {
            self.p.targetclasses = vec![Val::key("class.ship"), Val::key("class.station")];
            // L38-40
        }
        let ignored = |u: Uid| self.p.list_objectstoignore.contains(&Val::uid(u));
        let f = cx.facts;
        if f.flag(self.p.primarytarget, Prop::CanBeAttacked) {
            // L43-51
            if !ignored(self.p.primarytarget) {
                cx.debug(45, dc)?;
                targets.push(self.p.primarytarget);
            } else {
                cx.debug(49, dc)?;
            }
        } else if !self.p.secondarytargets.is_empty() {
            // L52-64
            cx.debug(53, dc)?;
            for t in self.p.secondarytargets.clone() {
                if !ignored(t) {
                    targets.push(t);
                } else {
                    cx.debug(61, dc)?;
                }
            }
        } else if self.p.allowothertargets {
            // L65-94
            cx.debug(67, dc)?;
            let ct = cx.facts.uid_of(ship, Prop::CommanderAttackTarget);
            if !ct.is_null() && cx.facts.flag(ct, Prop::CanBeAttacked) {
                if !ignored(ct) {
                    targets.push(ct);
                } else {
                    cx.debug(74, dc)?;
                }
            }
            let in_sector = !cx.facts.uid_of(cx.ent, Prop::Sector).is_null();
            if targets.is_empty() && in_sector {
                cx.debug(80, dc)?;
                let q = Query::GravidarContacts {
                    object: ship,
                    classes: self.p.targetclasses.clone(),
                    maybeattackedby: if self.p.checkrelation { ship } else { Uid::NULL },
                    excluded: self.p.list_objectstoignore.clone(),
                };
                targets.extend(self.contacts(cx, 81, q)?);
            }
            if targets.is_empty() && cx.facts.exists(self.p.escort) && in_sector {
                cx.debug(88, dc)?;
                let q = Query::GravidarContacts {
                    object: ship,
                    classes: self.p.targetclasses.clone(),
                    maybeattackedby: if self.p.checkrelation { self.p.escort } else { Uid::NULL },
                    excluded: self.p.list_objectstoignore.clone(),
                };
                targets.extend(self.contacts(cx, 89, q)?);
            }
        }
        // L96 selection, L97, L99
        let mut highest: Option<f32> = None;
        let mut selected = 0usize;
        let count = targets.len();
        for (idx, &t) in targets.iter().enumerate() {
            let i = idx + 1;
            if !self.passes_skip_chain(cx, t)? {
                continue;
            }
            // L155-167
            let f = cx.facts;
            let playerowned = f.flag(cx.ent, Prop::IsPlayerOwned);
            if !playerowned && f.get(t, Prop::BoardingOperations).as_int().unwrap_or(0) > 0 {
                return Err(unsupported(155, "boarding operations on a candidate"));
            }
            // L169-177
            let refobject = if f.exists(self.p.escort) { self.p.escort } else { ship };
            let distance = f.distance_to(t, refobject);
            // L180-183
            if !self.p.pursuetargets && f.uid_of(refobject, Prop::Sector) != f.uid_of(t, Prop::Sector) {
                cx.debug(181, dc)?;
                continue;
            }
            cx.debug(184, 0.0)?;
            // L185-198 (radius is null on this path)
            if !self.p.pursuetargets && self.p.has_radius {
                return Err(unsupported(186, "radius anchor"));
            }
            if !self.p.pursuetargets && self.p.pursuedistance != 0.0 && distance > self.p.pursuedistance {
                cx.debug(194, dc)?;
                continue;
            }
            // L201-219
            let mut priority: f32 = 0.0;
            let f = cx.facts;
            let primary = self.p.primarytarget;
            if !primary.is_null() && (t == primary || f.uid_of(t, Prop::Container) == primary) {
                priority += 10.0;
            } else if !self.p.targetpurposes.is_empty()
                && !self.p.targetpurposes.contains(&f.get(t, Prop::PrimaryPurpose))
            {
                cx.debug(210, dc)?;
                continue;
            } else if !self.p.targetclasses.is_empty() {
                let defensible = f.uid_of(t, Prop::Container); // `@$target.defensible`: the target itself or its container
                let def_ok = if f.is_class(t, Class::Defensible) {
                    false
                } else {
                    !is_any_class(f, if defensible.is_null() { t } else { defensible }, &self.p.targetclasses)
                };
                if !is_any_class(f, t, &self.p.targetclasses) && (f.is_class(t, Class::Defensible) || def_ok) {
                    cx.debug(215, dc)?;
                    continue;
                }
            }
            // L222-235 ($closestmodule is only set by the big-target branch)
            if distance < 5500.0 {
                if distance > 500.0 {
                    priority += 10.0 - (distance - 500.0) / 500.0;
                } else {
                    priority += 10.0;
                }
                cx.debug(233, dc)?;
            }
            // L237-243
            let f = cx.facts;
            priority -= f.relation_to(t, ship) * 5.0;
            if f.exists(self.p.escort) {
                priority -= f.relation_to(t, self.p.escort) * 3.0;
            }
            // L246-254
            let hull = f.get(t, Prop::Hull).as_f64().unwrap_or(0.0);
            let shield = f.get(t, Prop::Shield).as_f64().unwrap_or(0.0);
            if hull + shield > 0.0 {
                let dps = f.get(t, Prop::DpsAll).as_f32().unwrap_or(0.0);
                if dps != 0.0 {
                    priority += 10.0 * dps / ((hull + shield) as f32);
                }
                cx.debug(251, dc)?;
            }
            // L256-267
            if self.p.readmadscore && self.p.internalorder && f.flag(Uid::NULL, Prop::MadScoreEnabled) {
                return Err(unsupported(256, "MAD score"));
            }
            // L271-274
            let f = cx.facts;
            if f.flag(ship, Prop::IsCapitalShip) && f.is_class(t, Class::Ship) && !f.flag(t, Prop::IsCapitalShip) {
                priority -= 20.0;
                cx.debug(273, dc)?;
            }
            // L276-278
            let f = cx.facts;
            if f.now() <= 60.0 && t == f.uid_of(Uid::NULL, Prop::PlayerOccupiedShip) && count > 1 && priority > 1.0 {
                priority = 1.0;
            }
            // L283-287
            if highest.is_none_or(|h| priority > h) {
                highest = Some(priority);
                selected = i;
                cx.debug(286, dc)?;
            }
        }
        // L291-303
        let target = if selected == 0 {
            cx.debug(293, dc)?;
            Uid::NULL
        } else if targets[selected - 1] != self.p.primarytarget {
            cx.debug(298, dc)?;
            targets[selected - 1]
        } else {
            cx.debug(302, dc)?;
            self.p.primarytarget
        };
        // L306: big targets are re-selected among their modules/surface elements
        let f = cx.facts;
        if !target.is_null()
            && (f.flag(target, Prop::IsModular)
                || f.flag(target, Prop::CanHaveAttackableModules)
                || (!f.flag(ship, Prop::IsCapitalShip) && f.flag(target, Prop::IsCapitalShip)))
        {
            return Err(unsupported(306, "big-target reselection (find_object_component)"));
        }
        let ret: RetVals = vec![("target".into(), Val::uid(target))];
        Ok(Yield::Return(ret))
    }

    fn contacts(&self, cx: &mut Cx<'_>, line: u32, q: Query) -> R<Vec<Uid>> {
        match cx.query(line, q)? {
            Answer::Path(v) => Ok(v),
            other => Err(Stop::Mismatch(format!("line {line}: expected contacts, got {other:?}"))),
        }
    }

    /// L102-153: the first matching skip condition prints (one draw) and skips the candidate.
    fn passes_skip_chain(&self, cx: &mut Cx<'_>, t: Uid) -> R<bool> {
        let dc = self.p.debugchance;
        let f = cx.facts;
        let ent = cx.ent;
        let tc = &self.p.targetclasses;
        let primary = self.p.primarytarget;
        let container = f.uid_of(t, Prop::Container);
        let line = if !f.flag(t, Prop::CanBeAttacked)
            || (self.p.checkrelation && !f.may_attack(ent, t))
            || f.get(ent, Prop::TrueOwner) == f.get(t, Prop::TrueOwner)
            || f.flag(t, Prop::Docked)
        {
            Some(103)
        } else if f.is_class(f.uid_of(t, Prop::Zone), Class::Highway) {
            Some(107)
        } else if f.is_class(t, Class::BuildStorage) {
            Some(111)
        } else if self.p.has_radius && !self.p.pursuetargets && f.flag(ent, Prop::IsPlayerOwned) {
            return Err(unsupported(114, "radius anchor"));
        } else if !is_any_class(f, t, tc)
            && !is_any_class(f, container, tc)
            && (!has_class(tc, "class.station")
                || (!f.flag(t, Prop::IsModular) && !f.flag(container, Prop::IsModular))
                || (f.is_class(t, Class::Ship)
                    && !f.flag(t, Prop::CanHaveAttackableModules)
                    && !f.flag(container, Prop::CanHaveAttackableModules)))
        {
            Some(119)
        } else if f.is_class(t, Class::ShipXS)
            && !f.flag(t, Prop::IsLaserTower)
            && !f.is_class(t, Class::Spacesuit)
            && t != primary
        {
            Some(127)
        } else if f.is_class(t, Class::Spacesuit) && !has_class(tc, "class.spacesuit") && t != primary {
            Some(131)
        } else if f.flag(t, Prop::IsIndestructible) && !f.flag(ent, Prop::IsPlayerOwned) {
            return Err(unsupported(134, "learned indestructible objects"));
        } else if self.p.engageonlyonline && !f.flag(t, Prop::IsOnlineObject) {
            Some(151)
        } else {
            None
        };
        match line {
            Some(l) => {
                cx.debug(l, dc)?;
                Ok(false)
            }
            None => Ok(true),
        }
    }
}
