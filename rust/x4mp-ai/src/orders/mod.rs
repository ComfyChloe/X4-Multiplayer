//! Ported AI scripts. Each one is a resumable state machine whose program counter is a label or a
//! continuation after a blocking action; comments cite the XML line of every action
//! (`x4-re/gamescripts/resolved/base/aiscripts/<script>.xml`, X4 9.00).

pub mod patrol;
pub mod seekenemies;
pub mod targetselection;

use crate::trace::Record;
use crate::val::Val;
use crate::world::{Stop, R};

/// Read an order/script parameter from a `start` record or from call params, with a default.
pub(crate) fn param(r: &[(String, Val)], name: &str) -> Option<Val> {
    r.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone())
}

pub(crate) fn p_bool(r: &[(String, Val)], name: &str, default: bool) -> R<bool> {
    match param(r, name) {
        None => Ok(default),
        Some(Val::Bool(b)) => Ok(b),
        Some(Val::Int(i)) => Ok(i != 0),
        Some(other) => Err(Stop::Mismatch(format!("param {name}: expected bool, got {other}"))),
    }
}

pub(crate) fn p_f32(r: &[(String, Val)], name: &str, default: f32) -> R<f32> {
    match param(r, name) {
        None => Ok(default),
        Some(Val::F32(v)) => Ok(v),
        Some(Val::Int(i)) => Ok(i as f32),
        Some(other) => Err(Stop::Mismatch(format!("param {name}: expected f32, got {other}"))),
    }
}

pub(crate) fn p_f64(r: &[(String, Val)], name: &str, default: f64) -> R<f64> {
    match param(r, name) {
        None => Ok(default),
        Some(Val::F64(v)) => Ok(v),
        Some(Val::Int(i)) => Ok(i as f64),
        Some(other) => Err(Stop::Mismatch(format!("param {name}: expected f64, got {other}"))),
    }
}

pub(crate) fn p_list(r: &[(String, Val)], name: &str) -> R<Vec<Val>> {
    match param(r, name) {
        None | Some(Val::Null) => Ok(Vec::new()),
        Some(Val::List(l)) => Ok(l),
        Some(other) => Err(Stop::Mismatch(format!("param {name}: expected list, got {other}"))),
    }
}

/// Parameters of a `start` record (`param.<name>=` or `<name>=`).
pub fn start_params(r: &Record) -> Vec<(String, Val)> {
    r.args.iter().map(|(k, v)| (k.strip_prefix("param.").unwrap_or(k).to_string(), v.clone())).collect()
}
