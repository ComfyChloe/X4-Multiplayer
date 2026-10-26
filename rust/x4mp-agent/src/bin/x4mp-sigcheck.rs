//! Offline check of a Linux X4 executable against the agent's pinned build and sites,
//! and against the code heads of the exports `x4mp-core` binds.
//! Usage: x4mp-sigcheck /path/to/X4

use std::process::ExitCode;

use x4mp_agent::elf::FileImage;
use x4mp_agent::pinned::{self, Image};
use x4mp_agent::sha256::{hex, Sha256};

fn main() -> ExitCode {
    let Some(path) = std::env::args_os().nth(1) else {
        eprintln!("usage: x4mp-sigcheck /path/to/X4");
        return ExitCode::from(2);
    };
    let data = match std::fs::read(&path) {
        Ok(data) => data,
        Err(e) => {
            eprintln!("FAIL cannot read {}: {e}", path.to_string_lossy());
            return ExitCode::FAILURE;
        }
    };
    let mut failures = 0;
    if data.len() as u64 != pinned::FILE_SIZE {
        eprintln!("FAIL file size {} (pinned {})", data.len(), pinned::FILE_SIZE);
        failures += 1;
    }
    let mut hasher = Sha256::new();
    hasher.update(&data);
    let digest = hex(&hasher.finish());
    if digest != pinned::FILE_SHA256 {
        eprintln!("FAIL file sha256 {digest}");
        failures += 1;
    }
    let Some(image) = FileImage::new(data) else {
        eprintln!("FAIL not an x86-64 ELF64 executable");
        return ExitCode::FAILURE;
    };
    if image.elf_type() != Some(2) {
        eprintln!("FAIL not ET_EXEC");
        failures += 1;
    }
    match pinned::verify_sites(&image) {
        Ok(count) => println!("PASS build-id and {count} signature sites"),
        Err(reason) => {
            eprintln!("FAIL {reason}");
            failures += 1;
        }
    }
    let mut heads = 0;
    for pin in x4mp_core::pins::EXPORTS {
        let name = pin.name.to_string_lossy();
        match image.read(pin.linux_va, x4mp_core::pins::HEAD_LEN) {
            Some(bytes) if pinned::digest(&bytes) == pin.linux_head => heads += 1,
            Some(bytes) => {
                eprintln!("FAIL export {name} at 0x{:x}: head sha256 {}", pin.linux_va, pinned::digest(&bytes));
                failures += 1;
            }
            None => {
                eprintln!("FAIL export {name} at 0x{:x}: unreadable", pin.linux_va);
                failures += 1;
            }
        }
    }
    if heads == x4mp_core::pins::EXPORTS.len() {
        println!("PASS {heads} x4mp-core export heads");
    }
    if failures == 0 {
        println!("PASS executable {} build {}; no files modified", pinned::GAME_VERSION, pinned::STEAM_BUILD);
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
