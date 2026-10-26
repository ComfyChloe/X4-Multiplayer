//! Round-trip time statistics from Ping/Pong samples. Integer arithmetic only, so the
//! same samples always give the same numbers (relay and client use the same code).

use std::collections::VecDeque;

/// Samples kept for percentiles.
pub const WINDOW: usize = 64;

#[derive(Clone, Debug, Default)]
pub struct RttStats {
    window: VecDeque<u64>,
    jitter_window: VecDeque<u64>,
    /// RFC 6298 smoothed RTT and variation, microseconds.
    srtt_us: Option<u64>,
    rttvar_us: u64,
    last_us: Option<u64>,
    min_us: Option<u64>,
    max_us: u64,
    samples: u64,
    lost: u64,
}

fn push(window: &mut VecDeque<u64>, value: u64) {
    if window.len() == WINDOW {
        window.pop_front();
    }
    window.push_back(value);
}

fn percentile(window: &VecDeque<u64>, pct: usize) -> Option<u64> {
    if window.is_empty() {
        return None;
    }
    let mut sorted: Vec<u64> = window.iter().copied().collect();
    sorted.sort_unstable();
    // Nearest-rank method.
    let rank = (pct * sorted.len()).div_ceil(100).max(1);
    sorted.get(rank - 1).copied()
}

impl RttStats {
    /// Add one RTT sample (microseconds).
    pub fn record(&mut self, rtt_us: u64) {
        if let Some(last) = self.last_us {
            push(&mut self.jitter_window, last.abs_diff(rtt_us));
        }
        push(&mut self.window, rtt_us);
        match self.srtt_us {
            None => {
                self.srtt_us = Some(rtt_us);
                self.rttvar_us = rtt_us / 2;
            }
            Some(srtt) => {
                self.rttvar_us = (3 * self.rttvar_us + srtt.abs_diff(rtt_us)) / 4;
                self.srtt_us = Some((7 * srtt + rtt_us) / 8);
            }
        }
        self.last_us = Some(rtt_us);
        self.min_us = Some(self.min_us.map_or(rtt_us, |m| m.min(rtt_us)));
        self.max_us = self.max_us.max(rtt_us);
        self.samples += 1;
    }

    /// A ping that was never answered.
    pub fn lost(&mut self) {
        self.lost += 1;
    }

    pub fn samples(&self) -> u64 {
        self.samples
    }

    pub fn lost_count(&self) -> u64 {
        self.lost
    }

    pub fn srtt_us(&self) -> Option<u64> {
        self.srtt_us
    }

    /// RFC 6298 RTT variation (mean deviation).
    pub fn rttvar_us(&self) -> Option<u64> {
        self.srtt_us.map(|_| self.rttvar_us)
    }

    pub fn last_us(&self) -> Option<u64> {
        self.last_us
    }

    pub fn min_us(&self) -> Option<u64> {
        self.min_us
    }

    pub fn max_us(&self) -> Option<u64> {
        self.srtt_us.map(|_| self.max_us)
    }

    pub fn p95_us(&self) -> Option<u64> {
        percentile(&self.window, 95)
    }

    /// 95th percentile of the change between consecutive samples.
    pub fn jitter_p95_us(&self) -> Option<u64> {
        percentile(&self.jitter_window, 95)
    }

    /// Proposal section 5: interpolation delay = send interval + 2 x p95 jitter.
    pub fn interpolation_delay_us(&self, send_interval_us: u64) -> u64 {
        send_interval_us.saturating_add(2 * self.jitter_p95_us().unwrap_or(0))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn smoothing_percentiles_and_delay() {
        let mut s = RttStats::default();
        assert_eq!(s.srtt_us(), None);
        assert_eq!(s.interpolation_delay_us(33_000), 33_000);
        for rtt in [40_000, 40_000, 60_000, 40_000] {
            s.record(rtt);
        }
        assert_eq!(s.min_us(), Some(40_000));
        assert_eq!(s.max_us(), Some(60_000));
        assert_eq!(s.p95_us(), Some(60_000));
        assert_eq!(s.jitter_p95_us(), Some(20_000));
        assert_eq!(s.interpolation_delay_us(33_000), 73_000);
        // srtt: 40000 -> 40000 -> 42500 -> 42187
        assert_eq!(s.srtt_us(), Some(42_187));
        s.lost();
        assert_eq!((s.samples(), s.lost_count()), (4, 1));
    }

    #[test]
    fn window_is_bounded() {
        let mut s = RttStats::default();
        for i in 0..1000 {
            s.record(i);
        }
        // Window 936..=999, nearest rank ceil(0.95 * 64) = 61.
        assert_eq!(s.p95_us(), Some(996));
        assert_eq!(s.min_us(), Some(0));
    }
}
