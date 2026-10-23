# Snapshot status — 23 October 2026

Version: {{VERSION}}. This is a source snapshot of an internal candidate, not a
public gameplay release or a claim of complete multiplayer universe support.

## Observed evidence

Separate instrumented native Linux host and guest runs loaded the same starting
save and matched 1,359 observed station identities, with a maximum positional
difference of about 0.031 metres. Those were separate runs with synthetic peers,
not a simultaneous Windows/Linux multiplayer playtest.

The clean packaged Linux host completed native save capture, authenticated
transfer and subsequent loading/admission with a synthetic guest. Archive
integrity checks passed for the matched internal Windows/Linux packages.
Native Windows hosting and simultaneous Windows/Linux gameplay acceptance remain
pending for this version at the time of this snapshot.

Humans previously passed radar, ordinary cargo/magnet interaction and Cooperative
Trial in version 7.2. These results do not establish every cargo case, general NPC
combat, arbitrary shared missions or an ongoing shared economy.

## Known limits and rejected approaches

Restarting reloads the pristine starting snapshot and loses multiplayer progress.
Station production, construction/destruction and general background consequences
are not yet continuously host-authoritative. Persistent campaign save/resume,
progressed-world late join and full docking/combat synchronization remain open.

Giving two independently generated worlds the same numeric seed did not reproduce
the complete station population: the experiment retained 46/47 missing/extra
identities. It was rejected as the initial-world solution. An early guest harness
also read a stale host log; that result was rejected, with corrected fresh evidence
kept separately. A failed result was not converted into a pass by removing rows.

Internal raw logs, local paths, participant details and coordination discussions
are not published in this mirror. These summaries describe retained internal
evidence; this repository does not provide an independently reproducible raw
native evidence bundle. Publication preparation does not add new gameplay results.
