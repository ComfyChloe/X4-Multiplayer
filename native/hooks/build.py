"""Build and test the default-enabled Windows read-only observer using Rust."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

TARGET = "x86_64-pc-windows-msvc"


def build_module(root):
    root = Path(root).resolve()
    crate = root / "native/hooks"
    cargo = shutil.which("cargo")
    if not cargo:
        raise RuntimeError("Cargo with the Windows MSVC target is required")
    installed = subprocess.check_output(["rustup", "target", "list", "--installed"], text=True)
    if TARGET not in installed.splitlines():
        raise RuntimeError(f"Install the Rust target with: rustup target add {TARGET}")
    env = dict(os.environ, CARGO_TARGET_DIR=str(root / ".local/hooks/target"))
    subprocess.run([cargo, "build", "--release", "--target", TARGET], cwd=crate, env=env, check=True)
    source = root / ".local/hooks/target" / TARGET / "release/x4lan_hooks.dll"
    destination = root / ".local/hooks/x4lan_hooks_64.dll"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="Run Rust unit tests before building")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.test:
        if os.name != "nt":
            parser.error("--test executes Windows Rust tests and requires Windows")
        cargo = shutil.which("cargo")
        env = dict(os.environ, CARGO_TARGET_DIR=str(root / ".local/hooks/target"))
        subprocess.run([cargo, "test", "--target", TARGET, "--", "--test-threads=1"],
                       cwd=root / "native/hooks", env=env, check=True)
    print(build_module(root))


if __name__ == "__main__":
    main()
