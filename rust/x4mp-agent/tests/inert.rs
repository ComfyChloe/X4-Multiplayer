//! The preloaded library must do nothing unless `X4MP_AGENT=1`, and must refuse (and
//! profile nothing) in a process that is not the pinned X4 build. Needs the cdylib from
//! `cargo build -p x4mp-agent` (cargo test builds only the rlib).
#![cfg(all(target_os = "linux", target_arch = "x86_64"))]

use std::path::PathBuf;
use std::process::Command;

fn library() -> PathBuf {
    // target/<profile>/deps/inert-<hash> -> target/<profile>/libx4mp_agent.so
    let exe = std::env::current_exe().unwrap();
    let lib = exe.parent().unwrap().parent().unwrap().join("libx4mp_agent.so");
    assert!(lib.is_file(), "cdylib not built at {} (run cargo build first)", lib.display());
    // `cargo test` does not rebuild the cdylib: refuse to test a stale one.
    let built = std::fs::metadata(&lib).unwrap().modified().unwrap();
    let src = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    for entry in std::fs::read_dir(src).unwrap().flatten() {
        let changed = entry.metadata().unwrap().modified().unwrap();
        assert!(
            changed <= built,
            "{} is newer than {}: run cargo build -p x4mp-agent first",
            entry.path().display(),
            lib.display()
        );
    }
    lib
}

fn scratch(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("x4mp-agent-inert-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// Thread count of a preloaded shell after the agent would have started.
fn threads_with(enable: Option<&str>, home: &PathBuf) -> String {
    let mut cmd = Command::new("/bin/sh");
    cmd.args(["-c", "sleep 0.4; ls /proc/$$/task | wc -l"])
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("HOME", home)
        .env("LD_PRELOAD", library());
    if let Some(v) = enable {
        cmd.env("X4MP_AGENT", v);
    }
    let out = cmd.output().unwrap();
    assert!(out.status.success(), "{out:?}");
    String::from_utf8(out.stdout).unwrap().trim().to_string()
}

#[test]
fn disabled_unless_exactly_one() {
    for value in [None, Some("0"), Some("true"), Some("")] {
        let home = scratch("off");
        assert_eq!(threads_with(value, &home), "1", "X4MP_AGENT={value:?} must not start a thread");
        assert!(!home.join("x4mp-agent").exists(), "X4MP_AGENT={value:?} must not create a log");
        // Feature flags alone never enable anything.
        let out = Command::new("/bin/sleep")
            .arg("0.3")
            .env("LD_PRELOAD", library())
            .env("HOME", &home)
            .env("X4MP_LOADPROF", "1")
            .env("X4MP_LOADSTAGES", "1")
            .env_remove("X4MP_AGENT")
            .status()
            .unwrap();
        assert!(out.success());
        assert!(!home.join("x4mp-agent").exists());
        std::fs::remove_dir_all(&home).unwrap();
    }
}

#[test]
fn enabled_in_a_foreign_process_refuses_and_profiles_nothing() {
    let dir = scratch("on");
    let log = dir.join("agent.log");
    let status = Command::new("/bin/sleep")
        .arg("0.5")
        .env("LD_PRELOAD", library())
        .env("X4MP_AGENT", "1")
        .env("X4MP_AGENT_LOG", &log)
        .env("X4MP_LOADSTAGES_FILE", dir.join("progressbar.xml"))
        .status()
        .unwrap();
    assert!(status.success(), "the host process must be unaffected");
    let text = std::fs::read_to_string(&log).unwrap();
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(lines.len(), 2, "{text}");
    assert!(lines[0].contains("X4MP_AGENT START pid="), "{text}");
    assert!(lines[1].contains("X4MP_AGENT REFUSED executable size"), "{text}");
    assert!(!text.contains("LOADPROF") && !text.contains("LOADSTAGES"), "{text}");
    std::fs::remove_dir_all(&dir).unwrap();
}
