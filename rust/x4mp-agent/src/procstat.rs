//! Parsers for the `/proc` files the load profiler reads. Pure functions over text, so
//! they are tested with fixtures on any platform; the reading itself is in `agent.rs`.

/// One line of `/proc/<pid>/task/<tid>/stat` (or `/proc/<pid>/stat`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct TaskStat {
    /// `comm` without the parentheses (may contain spaces and parentheses itself).
    pub comm: String,
    pub state: char,
    pub minflt: u64,
    pub majflt: u64,
    /// User and system CPU time in clock ticks.
    pub utime: u64,
    pub stime: u64,
    /// Start time in clock ticks after boot (field 22).
    pub starttime: u64,
}

impl TaskStat {
    pub fn cpu_ticks(&self) -> u64 {
        self.utime + self.stime
    }
}

/// Parse a `stat` line. `comm` is delimited by the first `(` and the **last** `)`,
/// because a thread name may itself contain `)` or spaces.
pub fn parse_stat(text: &str) -> Option<TaskStat> {
    let open = text.find('(')?;
    let close = text.rfind(')')?;
    if close < open {
        return None;
    }
    let comm = text[open + 1..close].to_string();
    // Fields after the comm, numbered from 3 (state).
    let rest: Vec<&str> = text[close + 1..].split_whitespace().collect();
    let field = |n: usize| -> Option<u64> { rest.get(n - 3)?.parse().ok() };
    Some(TaskStat {
        comm,
        state: rest.first()?.chars().next()?,
        minflt: field(10)?,
        majflt: field(12)?,
        utime: field(14)?,
        stime: field(15)?,
        starttime: field(22)?,
    })
}

/// `/proc/<pid>/io` counters (bytes).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct IoStat {
    /// Bytes passed to read()/pread() and friends, including page-cache hits.
    pub rchar: u64,
    pub wchar: u64,
    /// Bytes actually fetched from the storage layer.
    pub read_bytes: u64,
    pub write_bytes: u64,
}

pub fn parse_io(text: &str) -> Option<IoStat> {
    let mut io = IoStat::default();
    let mut seen = 0;
    for line in text.lines() {
        let Some((key, value)) = line.split_once(':') else { continue };
        let Ok(value) = value.trim().parse::<u64>() else { continue };
        let slot = match key.trim() {
            "rchar" => &mut io.rchar,
            "wchar" => &mut io.wchar,
            "read_bytes" => &mut io.read_bytes,
            "write_bytes" => &mut io.write_bytes,
            _ => continue,
        };
        *slot = value;
        seen += 1;
    }
    (seen > 0).then_some(io)
}

/// Resident set size and its high-water mark from `/proc/<pid>/status`, in KiB.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Memory {
    pub rss_kb: u64,
    pub hwm_kb: u64,
}

pub fn parse_status_memory(text: &str) -> Option<Memory> {
    let mut m = Memory::default();
    let mut seen = false;
    for line in text.lines() {
        let Some((key, value)) = line.split_once(':') else { continue };
        let kb = || value.trim().trim_end_matches("kB").trim().parse::<u64>().ok();
        match key {
            "VmRSS" => {
                m.rss_kb = kb()?;
                seen = true;
            }
            "VmHWM" => m.hwm_kb = kb()?,
            _ => {}
        }
    }
    seen.then_some(m)
}

/// Seconds since boot from `/proc/uptime`.
pub fn parse_uptime(text: &str) -> Option<f64> {
    text.split_whitespace().next()?.parse().ok()
}

/// The value after `-personalfolderid` in a NUL-separated `/proc/<pid>/cmdline`.
pub fn personal_folder_id(cmdline: &[u8]) -> Option<String> {
    let mut args = cmdline.split(|&b| b == 0);
    while let Some(arg) = args.next() {
        if arg == b"-personalfolderid" {
            let value = std::str::from_utf8(args.next()?).ok()?;
            if !value.is_empty() && value.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-') {
                return Some(value.to_string());
            }
            return None;
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    // Real-format fixtures (captured from a Linux 6.x process, values edited).
    const MAIN_STAT: &str = "41234 (X4) S 41200 41234 41200 0 -1 4194560 812345 0 12 0 4521 873 0 0 20 0 97 0 8812345 23456456704 1534221 18446744073709551615 4194304 64012345 140733 0 0 0 0 4096 1088 0 0 0 17 5 0 0 0 0 0 0 0 0 0 0 0 0 0\n";
    const ODD_STAT: &str = "41301 (Job (x) 3) R 41200 41234 41200 0 -1 4194368 77 0 0 0 1200 34 0 0 20 0 97 0 8812400 0 0 18446744073709551615 0 0 0 0 0 0 0 4096 1088 0 0 0 -1 7 0 0 0 0 0\n";

    #[test]
    fn stat_fields() {
        let s = parse_stat(MAIN_STAT).unwrap();
        assert_eq!(s.comm, "X4");
        assert_eq!(s.state, 'S');
        assert_eq!((s.minflt, s.majflt, s.utime, s.stime, s.starttime), (812345, 12, 4521, 873, 8812345));
        assert_eq!(s.cpu_ticks(), 5394);
    }

    #[test]
    fn stat_comm_with_spaces_and_parentheses() {
        let s = parse_stat(ODD_STAT).unwrap();
        assert_eq!(s.comm, "Job (x) 3");
        assert_eq!((s.state, s.utime, s.stime, s.starttime), ('R', 1200, 34, 8812400));
    }

    #[test]
    fn stat_rejects_truncated() {
        assert_eq!(parse_stat("12 (x) S 1 2 3"), None);
        assert_eq!(parse_stat(""), None);
        assert_eq!(parse_stat("12 )x( S"), None);
    }

    #[test]
    fn io_counters() {
        let text = "rchar: 2012345678\nwchar: 1234\nsyscr: 99\nsyscw: 5\nread_bytes: 1073741824\nwrite_bytes: 4096\ncancelled_write_bytes: 0\n";
        assert_eq!(
            parse_io(text),
            Some(IoStat { rchar: 2_012_345_678, wchar: 1234, read_bytes: 1_073_741_824, write_bytes: 4096 })
        );
        assert_eq!(parse_io("nothing here\n"), None);
    }

    #[test]
    fn status_memory() {
        let text = "Name:\tX4\nVmPeak:\t23000000 kB\nVmHWM:\t 6543210 kB\nVmRSS:\t 6123456 kB\nThreads:\t97\n";
        assert_eq!(parse_status_memory(text), Some(Memory { rss_kb: 6_123_456, hwm_kb: 6_543_210 }));
        assert_eq!(parse_status_memory("Name:\tX4\n"), None);
    }

    #[test]
    fn uptime_and_cmdline() {
        assert_eq!(parse_uptime("88123.45 700000.01\n"), Some(88123.45));
        let cmd = b"./X4\0-personalfolderid\x0045842\0-module\0custom_creative\0";
        assert_eq!(personal_folder_id(cmd).as_deref(), Some("45842"));
        assert_eq!(personal_folder_id(b"./X4\0-skipintro\0"), None);
        assert_eq!(personal_folder_id(b"./X4\0-personalfolderid\0../x\0"), None);
        assert_eq!(personal_folder_id(b"./X4\0-personalfolderid"), None);
    }
}
