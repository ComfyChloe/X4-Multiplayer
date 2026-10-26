//! Bounded log line buffer. The frame thread never does I/O: it appends lines here and
//! the host drains them from its own thread (Linux agent worker) or from Lua (Windows).

use std::collections::VecDeque;

pub const DEFAULT_LINES: usize = 256;
/// Longest line kept; longer lines are truncated at a char boundary.
pub const MAX_LINE: usize = 1024;

#[derive(Debug)]
pub struct LogBuf {
    lines: VecDeque<String>,
    capacity: usize,
    dropped: u64,
}

impl LogBuf {
    pub fn new(capacity: usize) -> Self {
        let capacity = capacity.max(1);
        LogBuf { lines: VecDeque::with_capacity(capacity), capacity, dropped: 0 }
    }

    /// Append a line; when full the **new** line is dropped and counted.
    pub fn push(&mut self, mut line: String) {
        if self.lines.len() >= self.capacity {
            self.dropped += 1;
            return;
        }
        if line.len() > MAX_LINE {
            let mut cut = MAX_LINE;
            while !line.is_char_boundary(cut) {
                cut -= 1;
            }
            line.truncate(cut);
        }
        self.lines.push_back(line);
    }

    pub fn pop(&mut self) -> Option<String> {
        self.lines.pop_front()
    }

    pub fn drain(&mut self) -> impl Iterator<Item = String> + '_ {
        self.lines.drain(..)
    }

    pub fn len(&self) -> usize {
        self.lines.len()
    }
    pub fn is_empty(&self) -> bool {
        self.lines.is_empty()
    }
    pub fn dropped(&self) -> u64 {
        self.dropped
    }
}

impl Default for LogBuf {
    fn default() -> Self {
        LogBuf::new(DEFAULT_LINES)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounded_and_counts_drops() {
        let mut log = LogBuf::new(2);
        log.push("a".into());
        log.push("b".into());
        log.push("c".into());
        assert_eq!(log.dropped(), 1);
        assert_eq!(log.drain().collect::<Vec<_>>(), vec!["a", "b"]);
        log.push("é".repeat(MAX_LINE));
        assert!(log.pop().unwrap().len() <= MAX_LINE);
    }
}
