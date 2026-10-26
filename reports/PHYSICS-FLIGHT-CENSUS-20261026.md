# Flight census and replay acceptance

Native run `flight-census-001` completed on lane b4 at 03:04 EDT, 26 October
2026, using physics source `5a41271` and the battle-400 scenario. The runner
closed the game gracefully. Evidence is local at
`.local/evidence-083-withheld,x4lan-debug.txt}`.

The combat window (game time 87.2413 to 207.447) contains 116 census windows,
1,715 sampled frames. Frame-weighted calls per frame from the rounded log:

- Forced: 0; highway: 1.78; linear: 2,039.86.
- Mass traffic: 14.96; spline: 22.22; steering: 406.00.
- Total: 2,484.82; cheap updates: 434.76 (17.50%).

The sum of worker-call durations is 15.84 ms per sampled frame. This is
aggregate worker time, not critical-path/frame latency. The run had other X4
instances and `timing_trustworthy=false`. It cannot establish a speedup.
Approximate call-weighted duration is 1.68 us linear and 30.25 us steering;
steering dominates the recorded aggregate time despite fewer calls.

This supersedes the earlier low-load observation that cheap pathing updates
are practically absent on Windows. They occurred substantially in this
contended battle. The static budget conversion finding is unchanged.

## Acceptance correction

ForcedFlight has extensive **offline** oracle equivalence evidence, but this
native scenario exercised it zero times. The replay entry point previously
returned success on a zero-record capture. It now refuses empty captures with
an explicit untested-coverage error. Parsing also rejects undersized model
images, unsupported agent layouts, arithmetic-overflow layouts and records
whose model/flags disagree with the file contract.

Validation: `cargo test -p x4mp-physics --release`: 10 tests passed, including
the original-machine-code oracle, all 2,130,706,434 in-range asinf inputs,
capture corruption control and three capture-validation regressions. No native
ForcedFlight capture/replay, shadow or switch acceptance is claimed.

Windows oracle tests now fail if the pinned executable is missing, rather than
returning early and being counted as passing. Set `X4MP_X4_EXE` if the usual RE
workspace path is unavailable. A missing-path negative control returned exit101
as expected; the normal release suite then/also passed all10 tests. Other OSes
still compile out the Windows oracle tests and do not provide oracle coverage.

## Next work

Find a disposable scenario that naturally exercises ForcedFlight before its
native capture stage. For the dominant linear/steering classes, pin object sizes
and external inputs before attempting replay. Linear and mass-traffic updates
visibly mutate model state, so slot equality alone would be incomplete evidence.

The native writer now emits format version 2 (same magic), appending the model
image after Update to each record. The reader accepts versions 1 and 2. Forced
replay verifies the model remains unchanged when that output is present, and
reports model differences separately from slot differences. A two-worker C
capture test verifies persistent mutations are captured; the 2,000-record
original-machine-code replay also covers v2 and an injected model-output error.
These are offline/synthetic checks; native v2 capture validation is pending.
