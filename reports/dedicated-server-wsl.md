# Dedicated server — WSL validation status

Branch: `contrib/kimi-dedicated-server` (working-tree evidence, before native acceptance)

## Implemented

- Rust `x4lan-dedicated` supervisor for Linux X4 and the Rust relay. It verifies the
  pinned executable, owned pristine save, installed scripts/config and display, then
  requires native game-time progress plus an admitted authority peer for readiness.
- Lua heartbeat and bounded local pause/resume/shutdown control in isolated profile
  45841. Old commands are not replayed. No engine hooks and no automatic world restart.
- Authority `p1` plus human guests `p2` (ship 2) and `p3` (ship 1), independent sector
  interest/baseline delivery and fan-out for cargo, identity, combat, missions and motion.
- Internal triple packaging with the Rust supervisor and Rust native transport.

## Automated evidence

- WSL Ubuntu 24.04, Python 3.12, Lupa 2.8: **657 tests pass, no skips** via
  `.venv/bin/python dev/check.py`.
- Rust: 18 library tests and 10 integration tests pass; `cargo clippy --all-targets`
  reports no warnings. Five of the library tests target dedicated supervision.
- The existing Lua/native contract passes against `libx4lan_net.so` (12 test cases).
- Differential parity: 137/137 recorded traces, 63/63 handwritten scenarios and
  2000/2000 randomized scenarios identical. Number grammar/formatting 21/21.
- Dedicated triple package builds successfully with three archives; SHA-256
  inventories pass and both Linux binaries are Rust ELF executables.

These are synthetic/unit/integration results, not two-native-player acceptance.

## Native WSL status: BLOCKED, NOT ACCEPTED

The development package was installed into the test copy under `x4lan-test` with an
isolated home and profile 45841. Steam was downloaded and extracted into the same
isolated test area. Multiple launch attempts reached Steam startup/update, but X4 did
not create `x4lan-debug.txt` or complete native world capture. Steam extraction was
slow on DrvFs and one launch exceeded 900 seconds. `STEAM_RUNTIME_LOGGER=0` was used
after the logger reported an unsupported FIFO; this did not establish game startup.

No Sandboxie instance was used. No normal save, Workshop selection or sealed package
was changed. No native readiness, two-client gameplay or performance claim is made.

Still required: complete world capture, verify prepared save, run the supervisor to
`ready`, connect synthetic p2/p3, test independent interests and p3 handoff, then
graceful `stop`. Two real human clients and multi-sector gameplay remain separate gates.
