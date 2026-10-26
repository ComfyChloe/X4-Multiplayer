WITHDRAWN - DO NOT RUN THE ORIGINAL TEST-MOD ISOLATION HELPER

A tester confirmed missing Steam Workshop subscriptions after using the test
setup. Writing only an isolated X4 profile is not sufficient evidence of Steam
account isolation. The original helper's subscription effects were not tested.
Exact causality is still under investigation; treat the helper as unsafe.

8-ISOLATE-TEST-MODS now refuses writes, including direct Python apply() calls.
The old Fix-1 ZIP is withdrawn. Keep backups/test-mod-isolation-* as evidence.
Use the separate Workshop-Recovery-1 supplement for guarded local rollback and
Workshop links. It does not automatically reconstruct an account subscription
snapshot, because the old helper never recorded one. Do not resume this test
profile until a reviewed replacement is available.
