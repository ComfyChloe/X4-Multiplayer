//! Bounded queue of engine actions that have no C export, executed by one MD cue.
//!
//! ```text
//!  frame thread (Rust)                 same thread, UI onUpdate (Lua)        MD
//!  subsystem --submit--> pending  --take_batch--> wire text --decode--> AddUITriggeredEvent
//!            <--Backpressure--        (in flight, with id, sent time)       ('X4MP_Core','Batch', t)
//!                                                                             |
//!  results <--complete(id,status)-- x4mp_core_md_result <-- RegisterEvent <-- raise_lua_event
//!                                                                  'X4MP_Core.Results' "id status;..."
//! ```
//!
//! - **Single producer:** only the frame thread submits. The Lua bridge runs on the same
//!   engine thread (UI `onUpdate`); the runtime rejects foreign threads.
//! - **Bounded:** at most `capacity` actions are outstanding (pending + in flight); further
//!   submits fail with [`Backpressure::Full`]. Nothing grows on the hot path.
//! - **Ids and results:** every action gets a unique id; each submitter receives exactly
//!   one [`MdResult`] (ok / failed / unknown kind / timeout).
//! - **Timeouts:** an action in flight longer than `timeout_ns` completes with
//!   [`MdStatus::Timeout`]; a late MD answer is then counted as unknown.
//!
//! Wire format (Rust -> Lua), UTF-8 text, one action per line:
//! `X4MPQ1\n` then `<id>\t<kind>\t<arg>\n` per action; `%`, TAB, CR, LF and other control
//! bytes inside `arg` are written as `%XX`. Lua turns a batch into one flat table
//! `{n=N, id1=..., kind1=..., arg1=..., id2=...}` for MD.

use std::collections::VecDeque;
use std::fmt::Write as _;

pub const WIRE_HEADER: &str = "X4MPQ1";
/// UI event screen/control the MD executor listens to, and the Lua event it raises.
pub const MD_SCREEN: &str = "X4MP_Core";
pub const MD_CONTROL: &str = "Batch";
pub const MD_RESULT_EVENT: &str = "X4MP_Core.Results";
/// Longest `debug_text` payload accepted (bytes).
pub const MAX_TEXT: usize = 200;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ActionId(pub u64);

/// An engine action without an export. Each kind must have a branch in the MD executor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MdAction {
    /// `<debug_text>` into the game log: harmless, used to prove the path.
    DebugText(String),
}

impl MdAction {
    pub fn kind(&self) -> &'static str {
        match self {
            MdAction::DebugText(_) => "debug_text",
        }
    }
    fn validate(&self) -> Result<(), &'static str> {
        match self {
            MdAction::DebugText(text) if text.is_empty() => Err("empty_text"),
            MdAction::DebugText(text) if text.len() > MAX_TEXT => Err("text_too_long"),
            MdAction::DebugText(_) => Ok(()),
        }
    }
    fn write_arg(&self, out: &mut String) {
        match self {
            MdAction::DebugText(text) => escape_into(text, out),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MdStatus {
    Ok,
    Failed,
    UnknownKind,
    Timeout,
}

impl MdStatus {
    /// Codes passed through the C bridge (`x4mp_core_md_result`).
    pub fn from_code(code: i32) -> Option<MdStatus> {
        match code {
            0 => Some(MdStatus::Ok),
            1 => Some(MdStatus::Failed),
            2 => Some(MdStatus::UnknownKind),
            _ => None,
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            MdStatus::Ok => "ok",
            MdStatus::Failed => "failed",
            MdStatus::UnknownKind => "unknown_kind",
            MdStatus::Timeout => "timeout",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Backpressure {
    /// `capacity` actions are already outstanding: retry on a later frame.
    Full,
    Invalid(&'static str),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MdResult {
    pub id: ActionId,
    /// Submitter (subsystem index assigned by the frame driver).
    pub owner: u16,
    pub status: MdStatus,
    /// Monotonic time from submit to completion (ns).
    pub latency_ns: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MdQueueConfig {
    /// Maximum outstanding actions (pending + in flight).
    pub capacity: usize,
    /// Maximum actions per batch (per frame).
    pub max_batch: usize,
    /// Maximum wire bytes per batch.
    pub max_batch_bytes: usize,
    pub timeout_ns: u64,
    /// Completed results waiting for their submitter.
    pub results_capacity: usize,
}

impl Default for MdQueueConfig {
    fn default() -> Self {
        MdQueueConfig {
            capacity: 64,
            max_batch: 16,
            max_batch_bytes: 4096,
            timeout_ns: 5_000_000_000,
            results_capacity: 64,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct MdCounters {
    pub submitted: u64,
    pub rejected: u64,
    pub batches: u64,
    pub sent: u64,
    pub completed_ok: u64,
    pub completed_failed: u64,
    pub unknown_kind: u64,
    pub timeouts: u64,
    /// Results for ids that are not in flight (late, duplicate or forged).
    pub unknown_results: u64,
    /// Results discarded because their submitter did not collect them.
    pub results_dropped: u64,
}

#[derive(Debug, Clone)]
struct Pending {
    id: ActionId,
    owner: u16,
    action: MdAction,
    submitted_ns: u64,
}

#[derive(Debug, Clone, Copy)]
struct InFlight {
    id: ActionId,
    owner: u16,
    submitted_ns: u64,
    sent_ns: u64,
}

#[derive(Debug)]
pub struct MdQueue {
    config: MdQueueConfig,
    pending: VecDeque<Pending>,
    in_flight: Vec<InFlight>,
    results: VecDeque<MdResult>,
    next_id: u64,
    counters: MdCounters,
    line: String,
}

/// Escape `%`, control bytes and DEL as `%XX` (uppercase hex).
pub fn escape_into(text: &str, out: &mut String) {
    for ch in text.chars() {
        if ch == '%' || (ch as u32) < 0x20 || ch == '\u{7f}' {
            let _ = write!(out, "%{:02X}", ch as u32);
        } else {
            out.push(ch);
        }
    }
}

/// Inverse of [`escape_into`] (the Lua bridge does the same with `gsub`).
pub fn unescape(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            let hex = std::str::from_utf8(bytes.get(i + 1..i + 3)?).ok()?;
            out.push(u8::from_str_radix(hex, 16).ok()?);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8(out).ok()
}

/// Decoded wire action: `(id, kind, arg)`. Mirrors the Lua bridge; used by tests and tools.
pub fn decode_batch(text: &str) -> Option<Vec<(u64, String, String)>> {
    let mut lines = text.split_terminator('\n');
    if lines.next()? != WIRE_HEADER {
        return None;
    }
    lines
        .map(|line| {
            let mut fields = line.split('\t');
            let id = fields.next()?.parse().ok()?;
            let kind = fields.next()?.to_string();
            let arg = unescape(fields.next()?)?;
            fields.next().is_none().then_some((id, kind, arg))
        })
        .collect()
}

/// Parse the MD result string `"<id> <status>;..."` into `(id, code)` pairs.
pub fn parse_results(text: &str) -> Vec<(u64, i32)> {
    text.split(';')
        .filter_map(|item| {
            let (id, status) = item.trim().split_once(' ')?;
            let code = match status {
                "ok" => 0,
                "failed" => 1,
                "unknown_kind" => 2,
                _ => return None,
            };
            Some((id.parse().ok()?, code))
        })
        .collect()
}

impl MdQueue {
    pub fn new(config: MdQueueConfig) -> Self {
        let config = MdQueueConfig {
            capacity: config.capacity.max(1),
            max_batch: config.max_batch.max(1),
            results_capacity: config.results_capacity.max(1),
            ..config
        };
        MdQueue {
            pending: VecDeque::with_capacity(config.capacity),
            in_flight: Vec::with_capacity(config.capacity),
            results: VecDeque::with_capacity(config.results_capacity),
            next_id: 1,
            counters: MdCounters::default(),
            line: String::with_capacity(MAX_TEXT * 3 + 64),
            config,
        }
    }

    pub fn config(&self) -> &MdQueueConfig {
        &self.config
    }
    pub fn counters(&self) -> MdCounters {
        self.counters
    }
    pub fn pending(&self) -> usize {
        self.pending.len()
    }
    pub fn in_flight(&self) -> usize {
        self.in_flight.len()
    }
    pub fn outstanding(&self) -> usize {
        self.pending.len() + self.in_flight.len()
    }

    pub fn submit(&mut self, owner: u16, action: MdAction, now_ns: u64) -> Result<ActionId, Backpressure> {
        if let Err(reason) = action.validate() {
            self.counters.rejected += 1;
            return Err(Backpressure::Invalid(reason));
        }
        if self.outstanding() >= self.config.capacity {
            self.counters.rejected += 1;
            return Err(Backpressure::Full);
        }
        let id = ActionId(self.next_id);
        self.next_id += 1;
        self.pending.push_back(Pending { id, owner, action, submitted_ns: now_ns });
        self.counters.submitted += 1;
        Ok(id)
    }

    /// Move up to `max_batch` pending actions into flight and write their wire text to
    /// `out` (cleared first), staying within `min(limit_bytes, max_batch_bytes)`. Returns
    /// the number of actions; 0 leaves `out` empty and moves nothing.
    pub fn take_batch(&mut self, now_ns: u64, limit_bytes: usize, out: &mut String) -> usize {
        out.clear();
        let limit = limit_bytes.min(self.config.max_batch_bytes);
        if self.pending.is_empty() || limit <= WIRE_HEADER.len() + 1 {
            return 0;
        }
        out.push_str(WIRE_HEADER);
        out.push('\n');
        let mut count = 0;
        while count < self.config.max_batch {
            let Some(next) = self.pending.front() else { break };
            self.line.clear();
            let _ = write!(self.line, "{}\t{}\t", next.id.0, next.action.kind());
            next.action.write_arg(&mut self.line);
            self.line.push('\n');
            if out.len() + self.line.len() > limit {
                break;
            }
            out.push_str(&self.line);
            let sent = self.pending.pop_front().expect("front exists");
            self.in_flight.push(InFlight {
                id: sent.id,
                owner: sent.owner,
                submitted_ns: sent.submitted_ns,
                sent_ns: now_ns,
            });
            count += 1;
        }
        if count == 0 {
            out.clear();
        } else {
            self.counters.batches += 1;
            self.counters.sent += count as u64;
        }
        count
    }

    fn finish(&mut self, flight: InFlight, status: MdStatus, now_ns: u64) {
        match status {
            MdStatus::Ok => self.counters.completed_ok += 1,
            MdStatus::Failed => self.counters.completed_failed += 1,
            MdStatus::UnknownKind => self.counters.unknown_kind += 1,
            MdStatus::Timeout => self.counters.timeouts += 1,
        }
        if self.results.len() >= self.config.results_capacity {
            self.results.pop_front();
            self.counters.results_dropped += 1;
        }
        self.results.push_back(MdResult {
            id: flight.id,
            owner: flight.owner,
            status,
            latency_ns: now_ns.saturating_sub(flight.submitted_ns),
        });
    }

    /// A result from MD. Unknown ids (late after timeout, duplicates) are counted only.
    pub fn complete(&mut self, id: ActionId, status: MdStatus, now_ns: u64) -> bool {
        match self.in_flight.iter().position(|f| f.id == id) {
            Some(index) => {
                let flight = self.in_flight.swap_remove(index);
                self.finish(flight, status, now_ns);
                true
            }
            None => {
                self.counters.unknown_results += 1;
                false
            }
        }
    }

    /// Time out actions in flight for longer than `timeout_ns`.
    pub fn expire(&mut self, now_ns: u64) {
        let timeout = self.config.timeout_ns;
        let mut index = 0;
        while index < self.in_flight.len() {
            if now_ns.saturating_sub(self.in_flight[index].sent_ns) > timeout {
                let flight = self.in_flight.swap_remove(index);
                self.finish(flight, MdStatus::Timeout, now_ns);
            } else {
                index += 1;
            }
        }
    }

    /// Next completed result for `owner`, oldest first.
    pub fn take_result(&mut self, owner: u16) -> Option<MdResult> {
        let index = self.results.iter().position(|r| r.owner == owner)?;
        self.results.remove(index)
    }

    /// Drop everything (world reload): pending and in-flight actions time out now.
    pub fn reset(&mut self, now_ns: u64) {
        while let Some(p) = self.pending.pop_front() {
            let flight = InFlight { id: p.id, owner: p.owner, submitted_ns: p.submitted_ns, sent_ns: now_ns };
            self.finish(flight, MdStatus::Timeout, now_ns);
        }
        self.expire(u64::MAX);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(s: &str) -> MdAction {
        MdAction::DebugText(s.into())
    }

    #[test]
    fn submit_batch_complete_round_trip() {
        let mut q = MdQueue::new(MdQueueConfig::default());
        let a = q.submit(1, text("hello"), 100).unwrap();
        let b = q.submit(2, text("tab\there 100%\nnext"), 100).unwrap();
        assert_ne!(a, b);
        let mut wire = String::new();
        assert_eq!(q.take_batch(200, 4096, &mut wire), 2);
        assert_eq!((q.pending(), q.in_flight()), (0, 2));
        let decoded = decode_batch(&wire).unwrap();
        assert_eq!(decoded[0], (a.0, "debug_text".into(), "hello".into()));
        assert_eq!(decoded[1], (b.0, "debug_text".into(), "tab\there 100%\nnext".into()));
        assert!(wire.contains("tab%09here 100%25%0Anext"), "{wire}");

        // MD answers b, then a with a failure.
        for (id, code) in parse_results(&format!("{} ok;{} failed;", b.0, a.0)) {
            assert!(q.complete(ActionId(id), MdStatus::from_code(code).unwrap(), 300));
        }
        assert_eq!(q.take_result(1).map(|r| (r.id, r.status, r.latency_ns)), Some((a, MdStatus::Failed, 200)));
        assert_eq!(q.take_result(2).map(|r| r.status), Some(MdStatus::Ok));
        assert_eq!(q.take_result(2), None);
        assert!(!q.complete(a, MdStatus::Ok, 400), "duplicate result");
        let c = q.counters();
        assert_eq!(
            (c.submitted, c.sent, c.batches, c.completed_ok, c.completed_failed, c.unknown_results),
            (2, 2, 1, 1, 1, 1)
        );
    }

    #[test]
    fn back_pressure_counts_pending_and_in_flight() {
        let mut q = MdQueue::new(MdQueueConfig { capacity: 2, ..Default::default() });
        q.submit(0, text("a"), 0).unwrap();
        let mut wire = String::new();
        q.take_batch(0, 4096, &mut wire);
        q.submit(0, text("b"), 0).unwrap();
        assert_eq!(q.submit(0, text("c"), 0), Err(Backpressure::Full));
        assert_eq!(q.submit(0, text(""), 0), Err(Backpressure::Invalid("empty_text")));
        assert_eq!(q.submit(0, text(&"x".repeat(MAX_TEXT + 1)), 0), Err(Backpressure::Invalid("text_too_long")));
        assert_eq!(q.counters().rejected, 3);
        q.complete(ActionId(1), MdStatus::Ok, 1);
        assert!(q.submit(0, text("c"), 0).is_ok());
    }

    #[test]
    fn batches_respect_count_and_byte_limits() {
        let mut q = MdQueue::new(MdQueueConfig { max_batch: 3, ..Default::default() });
        for i in 0..5 {
            q.submit(0, text(&format!("m{i}")), 0).unwrap();
        }
        let mut wire = String::new();
        assert_eq!(q.take_batch(0, 4096, &mut wire), 3);
        // Too small for even one action: nothing moves, nothing written.
        assert_eq!(q.take_batch(0, 12, &mut wire), 0);
        assert!(wire.is_empty());
        assert_eq!(q.pending(), 2);
        let one_line = WIRE_HEADER.len() + 1 + "4\tdebug_text\tm3\n".len();
        assert_eq!(q.take_batch(0, one_line, &mut wire), 1);
        assert!(wire.len() <= one_line);
        assert_eq!(q.take_batch(0, 4096, &mut wire), 1);
        assert_eq!(q.take_batch(0, 4096, &mut wire), 0);
    }

    #[test]
    fn timeouts_and_late_answers() {
        let mut q = MdQueue::new(MdQueueConfig { timeout_ns: 1000, ..Default::default() });
        let id = q.submit(3, text("x"), 0).unwrap();
        let mut wire = String::new();
        q.take_batch(10, 4096, &mut wire);
        q.expire(1010);
        assert_eq!(q.in_flight(), 1);
        q.expire(1011);
        assert_eq!(q.take_result(3).map(|r| r.status), Some(MdStatus::Timeout));
        assert!(!q.complete(id, MdStatus::Ok, 2000));
        assert_eq!((q.counters().timeouts, q.counters().unknown_results), (1, 1));
    }

    #[test]
    fn uncollected_results_are_bounded_and_reset_drains() {
        let mut q = MdQueue::new(MdQueueConfig { results_capacity: 2, ..Default::default() });
        for _ in 0..3 {
            q.submit(9, text("x"), 0).unwrap();
        }
        q.reset(5);
        assert_eq!(q.outstanding(), 0);
        assert_eq!(q.counters().results_dropped, 1);
        assert_eq!(q.counters().timeouts, 3);
    }

    #[test]
    fn executor_stub_and_lua_bridge_agree_with_rust() {
        let md = include_str!("../executor/x4mp_core_executor.xml");
        let lua = include_str!("../executor/x4mp_core_bridge.lua");
        assert!(md.contains(&format!("screen=\"'{MD_SCREEN}'\" control=\"'{MD_CONTROL}'\"")));
        assert!(md.contains(&format!("name=\"'{MD_RESULT_EVENT}'\"")));
        assert!(md.contains("$kind == 'debug_text'") && md.contains("' unknown_kind;'"));
        assert!(lua.contains(&format!("AddUITriggeredEvent(\"{MD_SCREEN}\", \"{MD_CONTROL}\", batch)")));
        assert!(lua.contains(&format!("RegisterEvent(\"{MD_RESULT_EVENT}\"")));
        assert!(lua.contains(&format!("line ~= \"{WIRE_HEADER}\"")));
        for (status, code) in [("ok", 0), ("failed", 1), ("unknown_kind", 2)] {
            assert!(lua.contains(&format!("{status} = {code}")), "{status}");
            assert_eq!(MdStatus::from_code(code).unwrap().name(), status);
        }
        assert!(lua.contains("intptr_t x4mp_core_md_take(uint8_t* buf, size_t cap);"));
        assert!(lua.contains("intptr_t x4mp_core_md_result(uint64_t id, int status);"));
        // MD caps a batch at 16 actions.
        assert!(md.contains("$batch.$n le 16") && MdQueueConfig::default().max_batch <= 16);
    }

    #[test]
    fn wire_escaping_and_parsing_edge_cases() {
        let mut s = String::new();
        escape_into("a%b\tc\u{1}é", &mut s);
        assert_eq!(s, "a%25b%09c%01é");
        assert_eq!(unescape(&s).as_deref(), Some("a%b\tc\u{1}é"));
        assert_eq!(unescape("%4"), None);
        assert_eq!(decode_batch("nope\n"), None);
        assert_eq!(decode_batch("X4MPQ1\n1\tdebug_text\n"), None);
        assert_eq!(parse_results("1 ok; 2 unknown_kind;3 weird;x ok;"), vec![(1, 0), (2, 2)]);
        assert_eq!(MdStatus::from_code(7), None);
    }
}
