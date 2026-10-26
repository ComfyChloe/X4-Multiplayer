# Dynamic load: retained native evidence and outstanding work

26 October 2026, Codex continuation of the save-trim experiment. This is a
read-only reanalysis of existing Windows native captures, not a new native run.
The inherited Rust save-trimmer edits remain outside this report's commit.

## Result and limits

Runtime creation succeeded for 107 ships and 15 station objects in the isolated
streaming fixture. Immediate station module counts are consistent with the
generated construction plans: 15 counts totalling 343 modules, repeated three
times. Exact per-station identity, module macros, production, storage, crew,
orders and original saved-world equivalence have **not** been established.

The completed manager-suppression observation does **not** establish zero native
spawning. Its Lua harness reloaded and reset its census baseline; subsequent
samples still show new and disappearing ship IDs. The planned minimal-only
comparison has no retained run and remains pending.

No speedup ratio or exclusive timing acceptance is supported. These runs were
contended by other X4 processes, and object creation is not the same operation
as restoring a saved universe.

## Streaming experiment

Local evidence root: `.local/evidence-035-withheld`. Main corrected batching run:
`20261026-014414-stream-4`; input manifest: `.local/evidence-036-withheld`.
The manifest describes sector `cluster_14_sector001_macro`, 107 ships and 15
stations. It omits six docked ships, the player, two player stations, build
storage, and other non-ship/station objects. Ship loadouts are generated at
level 0.5, not restored from saved equipment. Quaternion handling is approximate;
saved state, cargo and active behavior are not reconstructed.

The corrected batch run logged zero create failures for its 12 create operations
(nine ship batches and three station batches). Single-batch 107-ship creation
reported 0.017 and 0.021 s; 15-station batches reported 0.024 and 0.017 s. Those
are Lua Begin-to-next-tick intervals, excluding work before the Begin event,
not CPU-duration measurements or stable performance guarantees. One ship per
batch took 3.015 s across 108 sampled frames. `max_other_x4=6` records contention
and predates the current six-total-process rule.

At approximately 0.1 s after each station creation, module counts were:
44, 39, 39, 13, 9, 57, 15, 47, 19, 8, 11, 14, 9, 8, 11.
They match all 15 manifest plan sizes in observed sequence and as a multiset;
including constructions did not change those counts. Every logged plan is
`nil`: the Lua event handler did not receive the extra plan argument. Therefore
the evidence has no reliable plan/identity correlation and count equality alone
cannot establish complete station restoration.

The five-second checks report zero modules **after the fixture already cleared
the objects** (three-second inter-operation gap). They neither prove a delayed
construction failure nor establish five-second persistence. A new test must
retain each station until both correlated observations finish.

Earlier `stream-2`/`stream-3-stations` logs exposed the encoded float batching
bug: requested batch sizes often arrived as one; their numbers cannot validate
the intended batching plan. `stream-1` exited early. Keep those failed histories.

## Spawning and suppression

`20261026-025124-spawn2-083-managers` reaches a matching DONE marker. It records
`SUPPRESS god=off jobs_off=0` and two manager-suppression messages, with 15 then
zero cancelled cues. Its first census at game time 8.519 has 113 ships and 15
stations; a second `load` census at 66.561 has 133 ships and 15 stations. The
second census resets baseline tracking. At game time 366.574 the final count is
132 ships and 15 stations, with 15 new and 16 gone since the **second** baseline.
Thus the final ship count is 19 above the first count; the displayed new/gone
counters do not cover the entire run. Zero GOD/job event counters do not exclude
other spawning mechanisms, and `jobs_off=0` is not proof jobs were disabled.

The earlier `spawn-083-managers` run had 41 new stations and 61 new ships without
the later successful suppression markers. `spawn-082-managers` timed out, and
`spawn2-083-managers` at 02:50 exited 50. The retained batch3 output ends after
the successful manager run; no minimal-only result directory exists.

## Evidence checker and next steps

`python dev/check_dynload_evidence.py <run> [...] --plan <stream_list.json>`
reports marker, census continuity, contention and count-only evidence separately.
`--require-clean` exits nonzero for missing/wrong DONE, explicit FAIL/SKIP/stream
timeout, repeated/missing baseline, missing final census or failed stream counts.
It does not turn zero other-process samples into trustworthy timing or count
matches into station equivalence. Both main retained captures fail the strict
continuity gate because their harness reloaded. Seven synthetic negative-control
tests pass; actual capture checks correctly exit 1. Existing runner `result=done`
alone is insufficient: the private runner also uses that result for FAIL/SKIP.

Next native work needs a fresh-label, released-lane-compatible harness: correlate
station UID and plan in one supported payload, verify module identities and
states before/after at least five seconds, retain stations until the check ends,
and preserve census continuity across UI reload. Then compare minimal and
manager suppression with explicit acknowledgements and equal observation windows.
Do not rerun private `batch3.ps1`: it deletes incomplete result directories and
its old runner can release another session's instance mutex. Protected profiles
45843/45844 remain with their original session; use the current lane coordinator.

## Fresh station correlation result, 03:46 EDT

The minimal station-completeness gap above is now covered by a fresh isolated
functional run, `station-correlation-002`, run ID
`20261026-034448-b1-dynload-33140`, on released lane b1/profile 45850. The runner
reported PASS and restored the lane. Its private capture remains under
`.local/evidence-034-withheld`; generated construction plans and raw logs
remain local, outside Git. The failed `correlation-001` capture is also retained:
MD table event payloads did not arrive as Lua tables, and it produced no samples.

The corrected scalar event carries plan, index and hexadecimal station UID.
Lua decodes the UID with uint64 arithmetic rather than a floating-point number.
All 15 distinct station UIDs produced early and late samples, covering 343
modules per phase. For each station, the module macro multiset matched its
construction plan exactly, every enumerated module was operational, and the
module UID/macro pairs were unchanged between samples. Independent parsing of
the 30 captured sample rows confirmed an early-to-late interval of 5.066 seconds
for every station. One START and one DONE occurred; no DLCOR FAIL occurred.

Review found that the original gate measured the late deadline from station
creation, potentially allowing back-to-back samples after a UI stall. The
captured intervals above explicitly exclude that failure in run 002. The
committed harness now measures five seconds from the actual successful early
sample; a delayed-update regression test covers this correction. That corrected
deadline itself has unit coverage, not a separate native capture. All 16 dynamic
load tests pass; the generated MD passed the game's schema validation.

This establishes short-term construction-plan module completeness and identity
persistence in this fresh creative world. It does not establish save-equivalent
positions, inventories, crew, production, docking, economy or longer survival.
There is no performance claim: the runner explicitly marked timing untrustworthy.
The remaining minimum experiment is a continuous-baseline minimal-versus-manager
suppression comparison with explicit acknowledgements and equal observation
windows. That experiment has not been run here.
