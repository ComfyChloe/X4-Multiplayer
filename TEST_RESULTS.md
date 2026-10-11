# Historical Linux movement test — 9 October 2026

X4 9.00 (611726), Steam build 23660954, native Linux. One real X4 creative sandbox,
isolated profile 45841 with autosave disabled, a local relay and a synthetic peer.
This report preserves the original movement evidence; see TRADE_TESTS.txt for later work.

Actual player movement reached the external peer. A synthetic incoming path created
and moved one additional ship, confirmed visually by the user. Four deliberate
25 m proxy-movement probes read back with 0.000–0.002 m error. Across 217 samples,
maximum positional drift before the next write was 0.002 m. All 47 orientation
samples reported 0.000 degree error at three-decimal precision. These measure API
readback, not latency or agreement between independent physics simulations.

After a 12-second synthetic-peer interruption, the user confirmed disappearance,
reappearance and exactly one proxy. A relay restart reconnected both endpoints
without restarting X4 or losing the local origin. The recorded range was 250.78
seconds of game time, including outages, not a ten-minute two-human test. Four
spawn events occurred across reconnections; no general object-inventory audit exists.
No adapter STOPPED or initialisation errors occurred in the accepted run.
23 automated tests passed, including real UDP, two LuaJIT runtimes, native Linux
I/O, session changes, parsing, interpolation and ordinary-profile protection.

Direct OS access through Lua FFI was blocked and ffi.load was unavailable. A small
own Lua C module loaded by package.loadlib supplied nonblocking UDP. UI reloads
can lose game-start notifications; the Lua/MD readiness handshake addresses that.
The tutorial restricted flight, so visible tests moved to a creative sandbox.

No existing user save was loaded or modified. There were no test saves at completion;
autosave stayed disabled. The test game/server stopped and the development extension
was archived out of the game folder. Windows and two simultaneous games were not
part of this historical run. Combat, economy, AI and persistence were unproven then.

[Machine-readable results](reports/linux-results.json) and
[filtered log](reports/private-runtime-log-withheld.txt). `[=ERROR=]` on measurement messages
comes from X4's DebugError output and does not itself mean those messages are errors.
