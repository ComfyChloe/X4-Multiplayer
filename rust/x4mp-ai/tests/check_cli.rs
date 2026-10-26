//! Exercise the actual checker exit status, not just its report text. Synthetic fixtures only.
use std::path::PathBuf;
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_FILE: AtomicU64 = AtomicU64::new(0);

struct Input(PathBuf);
impl Drop for Input {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

fn check(input: &str) -> Output {
    let path = std::env::temp_dir().join(format!(
        "x4mp-ai-check-{}-{}.x4aitrace",
        std::process::id(),
        NEXT_FILE.fetch_add(1, Ordering::Relaxed)
    ));
    // Create atomically and never overwrite an existing file.
    use std::io::Write;
    let mut handle = std::fs::OpenOptions::new().write(true).create_new(true).open(&path).unwrap();
    let file = Input(path);
    handle.write_all(input.as_bytes()).unwrap();
    drop(handle);
    Command::new(env!("CARGO_BIN_EXE_x4ai-check")).arg(&file.0).output().unwrap()
}

fn capture(until: f64) -> String {
    format!(
        "X4AITRACE\t1\tbuild=23660954\tsource=native\tcapture=bracket\n\
         track\tc:0x1\tmove.generic\tslot=i:0\n\
         run\tc:0x1\tmove.generic\tt=d:{:016x}\tmask=x:1\trng=x:1234\tpc=i:0\n\
         yield\tc:0x1\tmove.generic\trng=x:1234\tpc=i:1\tyielded=b:1\tkw=i:2558\tline=i:382\tuntil=d:{:016x}\n",
        100.0f64.to_bits(),
        until.to_bits()
    )
}

#[test]
fn matching_capture_succeeds_but_one_ulp_mismatch_fails_the_command() {
    let good = 100.0f64 + 0.001;
    let result = check(&capture(good));
    assert!(result.status.success(), "{:?}", result);
    assert!(String::from_utf8_lossy(&result.stdout).contains("1/1 wake times bit-exact"));
    let bad = f64::from_bits(good.to_bits() + 1);
    let result = check(&capture(bad));
    assert_eq!(result.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&result.stdout).contains("first wait mismatch"));
}

#[test]
fn a_bad_session_is_not_hidden_by_a_later_good_one() {
    let raw = capture(100.1) + &capture(100.0 + 0.001);
    let log = raw.lines().map(|line| format!("AITR\t{line}\n")).collect::<String>();
    let result = check(&log);
    assert_eq!(result.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&result.stdout).contains("== session 2"));
}

#[test]
fn an_empty_or_unfinished_capture_cannot_pass() {
    for raw in [
        "X4AITRACE\t1\tbuild=23660954\tsource=native\tcapture=bracket\n".to_owned(),
        "log noise\nAITR\tnot-a-header\n".to_owned(),
        capture(100.001).lines().take(3).collect::<Vec<_>>().join("\n"),
    ] {
        let result = check(&raw);
        assert_eq!(result.status.code(), Some(2), "{:?}", result);
    }
}

#[test]
fn startup_rearm_session_is_explicitly_excluded_when_later_evidence_exists() {
    let raw = "X4AITRACE\t1\tbuild=23660954\tsource=native\tcapture=bracket\n".to_owned() + &capture(100.0 + 0.001);
    let log = raw.lines().map(|line| format!("AITR\t{line}\n")).collect::<String>();
    let result = check(&log);
    assert!(result.status.success(), "{:?}", result);
    assert!(String::from_utf8_lossy(&result.stdout)
        .contains("session 1: no complete brackets; excluded from wait validation"));
}
