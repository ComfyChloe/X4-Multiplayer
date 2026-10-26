# Combat load series, 26 October 2026

All four native scenarios completed their expected spawn, measurement and cleanup
markers. This validates the larger test fixtures under the observed load. It does
not validate a combat replacement or establish a performance scaling curve.

The runs used the existing profile overlay and staged version-3 combat scenarios,
one combat lane at a time. Every result has `timing_trustworthy=false`: other X4
processes ran concurrently. Keep the raw figures diagnostic; no scaling exponent,
speedup or hardware capacity claim follows from them.

Evidence directories, relative to this worktree:

- `.local/evidence-092-withheld`: pass; 87 ships spawned, mean 72.0 alive during
  measurement, 120.625 game seconds, ended at the measurement timeout. Diagnostic
  19.01 fps; peak working set 6.66 GB.
- `.local/evidence-093-withheld`: pass; 200 spawned in 7.12392 game seconds,
  mean 164.2 alive, 120.583-second measurement, ended at the measurement timeout.
  Diagnostic 15.03 fps; peak working set 6.86 GB.
- `.local/evidence-094-withheld`: pass, collected 03:28:24 EDT; 400 spawned in
  15.6056 seconds, mean 316.3 alive, 120.847-second measurement, ended at the
  measurement timeout. Diagnostic 8.31 fps; peak working set 7.03 GB.
- `.local/evidence-095-withheld`: pass, collected 03:37:59 EDT; 800 spawned in
  32.6202 seconds, mean 588.1 alive, 112.956-second measurement, ended `decided`.
  Diagnostic 6.50 fps; peak working set 7.35 GB.

The `timeout` window reason above is the intended scenario measurement duration,
not an x4lane watchdog failure. All lane verdicts are pass and cleanup was observed.
Ship counts differ over the windows because the fleets fight and lose ships.

The 800-ship trace contains 70,683 MoveWorker samples across ten complete profiler
windows. Inclusive AIDirector samples are 25.68%, component walk 19.42%, weapon Move
8.23%, bullet Move/flight/apply 2.82%, zone ray cast 4.51% and event processing
9.38%. These categories overlap; they are prioritization evidence for this run,
not additive exclusive CPU costs or a quiet-window ranking. AIDirector's share is
larger than in the 400-ship trace (13.17%); the AI-driver owner was notified.

Reproduce the complete diagnostic report with:

```
python dev/combat_scenario/scaling.py .local/evidence-092-withheld .local/evidence-093-withheld .local/evidence-094-withheld .local/evidence-095-withheld
```

The analyzer now labels timing eligibility and suppresses growth slopes unless
every compared run both passed and had trustworthy timing. A separate quiet-window
series remains necessary before making optimization or scaling claims.
