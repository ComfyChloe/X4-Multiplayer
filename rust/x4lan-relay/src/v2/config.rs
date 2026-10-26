//! v2 settings from session.json. v2 is chosen only by `"protocol": "v2"`; every key
//! below is optional and falls back to the [`V2Config::new`] default.
//!
//! ```json
//! { "token": "...", "build": "...", "bubble": "...", "port": 45841, "protocol": "v2",
//!   "v2": { "max_players": 6, "lease_timeout_s": 30, "resume_window_s": 300,
//!           "tick_hz": 60, "ping_interval_s": 1, "authority_token": "<32 hex>",
//!           "event_repeats": 3, "max_objects": 8192,
//!           "tier1_hz": [20, 30], "tier2_hz": [20, 30], "tier3_hz": [1, 2],
//!           "speed_for_max_rate": 300, "weapons_range_m": 6000, "refresh_hz": 1,
//!           "map_refresh_s": 30, "max_records_per_tick": 256,
//!           "disable": { "event_repeat": "reason why it is off" } } }
//! ```
//!
//! Features are on by default; `disable` switches one off and requires a reason,
//! which the relay logs as `FEATURE <name> DISABLED reason=...`.

use x4mp_proto::sched::TierRate;
use x4mp_proto::Token;

use super::V2Config;
use crate::json::Json;

/// Whether session.json selects protocol v2.
pub fn selects_v2(session: &Json) -> bool {
    session.get("protocol").and_then(Json::as_str) == Some("v2")
}

fn number(v2: &Json, key: &str) -> Result<Option<f64>, String> {
    match v2.get(key) {
        None => Ok(None),
        Some(value) => value
            .as_f64()
            .filter(|v| v.is_finite() && *v >= 0.0)
            .map(Some)
            .ok_or_else(|| format!("v2.{key} must be a non-negative number")),
    }
}

fn integer(v2: &Json, key: &str, max: f64) -> Result<Option<u64>, String> {
    match number(v2, key)? {
        Some(v) if v.fract() == 0.0 && v <= max => Ok(Some(v as u64)),
        Some(_) => Err(format!("v2.{key} must be an integer up to {max}")),
        None => Ok(None),
    }
}

fn micros(v2: &Json, key: &str) -> Result<Option<u64>, String> {
    Ok(number(v2, key)?.map(|s| (s * 1.0e6).round() as u64))
}

fn band(v2: &Json, key: &str) -> Result<Option<TierRate>, String> {
    match v2.get(key) {
        None => Ok(None),
        Some(Json::Array(pair)) if pair.len() == 2 => {
            let (Some(min_hz), Some(max_hz)) = (pair[0].as_f64(), pair[1].as_f64()) else {
                return Err(format!("v2.{key} must be [min_hz, max_hz]"));
            };
            Ok(Some(TierRate { min_hz, max_hz }))
        }
        Some(_) => Err(format!("v2.{key} must be [min_hz, max_hz]")),
    }
}

/// Build the v2 configuration from session.json and the already-resolved token,
/// build and world (bubble) identity.
pub fn from_session(session: &Json, token: &str, build: &str, world: &str) -> Result<V2Config, String> {
    let token = Token::parse_hex(token).ok_or("token must be 32 lowercase hexadecimal characters")?;
    let mut config = V2Config::new(token, build, world);
    let empty = Json::Object(vec![]);
    let v2 = session.get("v2").unwrap_or(&empty);
    if !matches!(v2, Json::Object(_)) {
        return Err("v2 must be an object".into());
    }
    if let Some(v) = integer(v2, "max_players", 64.0)? {
        config.max_players = v as u8;
    }
    if let Some(v) = micros(v2, "lease_timeout_s")? {
        config.lease_timeout_us = v;
    }
    if let Some(v) = micros(v2, "resume_window_s")? {
        config.resume_window_us = v;
    }
    if let Some(v) = integer(v2, "tick_hz", 1000.0)? {
        config.tick_hz = v as u32;
    }
    if let Some(v) = micros(v2, "ping_interval_s")? {
        config.ping_interval_us = v;
    }
    if let Some(v) = micros(v2, "ping_timeout_s")? {
        config.ping_timeout_us = v;
    }
    if let Some(v) = micros(v2, "roster_interval_s")? {
        config.roster_interval_us = v;
    }
    if let Some(v) = integer(v2, "event_repeats", 16.0)? {
        config.event_repeats = v as u8;
    }
    if let Some(v) = integer(v2, "max_objects", 1_000_000.0)? {
        config.max_objects = v as usize;
    }
    if let Some(text) = v2.get("authority_token") {
        let text = text.as_str().ok_or("v2.authority_token must be a string")?;
        config.authority_token =
            Some(Token::parse_hex(text).ok_or("v2.authority_token must be 32 lowercase hexadecimal characters")?);
    }
    let s = &mut config.scheduler;
    for (key, slot) in [("tier1_hz", &mut s.tier1), ("tier2_hz", &mut s.tier2), ("tier3_hz", &mut s.tier3)] {
        if let Some(rate) = band(v2, key)? {
            *slot = rate;
        }
    }
    if let Some(v) = number(v2, "speed_for_max_rate")? {
        s.speed_for_max_rate = v;
    }
    if let Some(v) = number(v2, "weapons_range_m")? {
        s.weapons_range_m = v;
    }
    if let Some(v) = number(v2, "refresh_hz")? {
        s.refresh_hz = v;
    }
    if let Some(v) = number(v2, "map_refresh_s")? {
        s.map_refresh_s = Some(v);
    }
    if let Some(v) = integer(v2, "max_records_per_tick", 65536.0)? {
        s.max_records_per_tick = v as usize;
    }
    match v2.get("disable") {
        None => {}
        Some(Json::Object(entries)) => {
            for (name, reason) in entries {
                let reason = reason.as_str().ok_or_else(|| format!("v2.disable.{name} must be a reason string"))?;
                if name == "speed_scaling" {
                    config.scheduler.speed_scaling = false;
                    config.speed_scaling_reason = Some(reason.to_string());
                } else {
                    config.features.disable(name, reason)?;
                }
            }
        }
        Some(_) => return Err("v2.disable must be an object of feature -> reason".into()),
    }
    config.validate()?;
    Ok(config)
}

#[cfg(test)]
mod tests {
    use super::*;

    const TOKEN: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    #[test]
    fn selection_defaults_overrides_and_loud_disables() {
        let v1 = Json::parse(r#"{"token": "x", "coop": true}"#).unwrap();
        assert!(!selects_v2(&v1));
        let plain = Json::parse(r#"{"protocol": "v2"}"#).unwrap();
        assert!(selects_v2(&plain));
        let config = from_session(&plain, TOKEN, "world1-test", "seed").unwrap();
        assert_eq!(config.max_players, 6);
        let session = super::super::V2Session::new(config, super::super::LeaseSource::Counter(0)).unwrap();
        assert_eq!(
            session.feature_report(),
            vec![
                "FEATURES x4lan-relay protocol=v2 rtt_stats=on roster=on event_repeat=on intent_cache=on speed_scaling=on"
            ]
        );
        let custom = Json::parse(
            r#"{"protocol": "v2", "v2": {"max_players": 4, "lease_timeout_s": 2.5, "tier3_hz": [0.5, 1],
                "disable": {"event_repeat": "measuring raw loss", "speed_scaling": "fixed-rate test"}}}"#,
        )
        .unwrap();
        let config = from_session(&custom, TOKEN, "b", "w").unwrap();
        assert_eq!((config.max_players, config.lease_timeout_us), (4, 2_500_000));
        assert_eq!(config.scheduler.tier3, TierRate { min_hz: 0.5, max_hz: 1.0 });
        let session = super::super::V2Session::new(config, super::super::LeaseSource::Counter(0)).unwrap();
        let report = session.feature_report();
        assert!(report[0].contains("event_repeat=off") && report[0].contains("speed_scaling=off"));
        assert_eq!(report[1], "FEATURE event_repeat DISABLED reason=measuring raw loss");
        assert_eq!(report[2], "FEATURE speed_scaling DISABLED reason=fixed-rate test");
        for bad in [
            r#"{"v2": {"max_players": 0}}"#,
            r#"{"v2": {"max_players": 65}}"#,
            r#"{"v2": {"tier1_hz": [30, 20]}}"#,
            r#"{"v2": {"disable": {"warp_drive": "x"}}}"#,
            r#"{"v2": {"disable": {"roster": true}}}"#,
            r#"{"v2": {"authority_token": "short"}}"#,
            r#"{"v2": 5}"#,
        ] {
            assert!(from_session(&Json::parse(bad).unwrap(), TOKEN, "b", "w").is_err(), "{bad}");
        }
    }
}
