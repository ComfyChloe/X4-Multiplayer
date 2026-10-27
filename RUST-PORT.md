# Rust port — the other one

This branch carries a second, independent Rust port of the relay, under `rust-chloe/`.

The upstream repository already has one, on `contrib/ferrite-rust-relay` (and a Rust core
that binds X4's FFI, on `contrib/ferrite-rust-core`). Two ports of the same relay, by two
authors, on a protocol where a byte difference is a bug, is a liability rather than an
asset — but this one is verifiable in a way the other is not, and the comparison is worth
making deliberately rather than by accident.

**Read `rust-chloe/docs/PORT-COMPARISON.md` first.** It sets out what each port does
better.

In short:

- **Upstream's port is stronger** on the type-level protocol (`Hex32`/`Atom`/`Slot`/`Ship`
  newtypes, enum lifecycles, the `"C-unwind"` ABI discipline at every Lua entry point) and
  on the native module parity harness (17 scenarios comparing the shipped C DLL against the
  Rust one, including GC, ICMP reset and an unresolvable ABI).
- **This port is stronger** on verification: 22 differential scenarios plus a randomised
  fuzzer that diff the transcripts of the unmodified Python reference against the Rust port
  byte for byte; a float-rendering cross-check against both Python and Lua over 546 values;
  and the extension's own `client.lua` run against the Rust relay in a real LuaJIT 2.1
  runtime over a real socket.

The recommendation in that document is that the project should keep one port — the one
inside the repository — and carry across the fuzzer, the float cross-check and the CI.

The port's own home is <https://github.com/ComfyChloe/X4-Multiplayer-Rust>, which is
where its history, its issues and its CI live.
