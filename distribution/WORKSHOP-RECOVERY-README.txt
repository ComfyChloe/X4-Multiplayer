X4 Multiplayer 7.2.0 INTERNAL - Workshop Recovery 1

This is assisted recovery, NOT an automatic account subscription restore.
The previous helper did not save a Steam subscription snapshot. Installed mods
can still be present after unsubscribing, and installed IDs are not proof that
every item was previously subscribed. We must not guess account changes.

1. Close X4 completely. Keep Steam available in the affected account.
2. Extract all files into the SAME configured Windows package folder that ran
   Fix-1, beside installation.json, package.json, runtime and backups.
   Allow replacement of isolate_test_mods.py, 8-ISOLATE-TEST-MODS.cmd and its
   README: the old helper is deliberately disabled. No game files are replaced.
3. Run 10-RECOVER-WORKSHOP.cmd (no administrator privileges).
4. The helper opens a local page listing affected Workshop IDs and names from
   remaining installed manifests. Review the list. On each official Steam item
   page, click Subscribe if you want that mod back. Already subscribed items
   need no action. Confirm the result in Steam's X4 subscribed-items list.

The helper also restores the ORIGINAL test-profile content.xml (or its original
absence) ONLY if all backup hashes form a complete chain and the current file
exactly matches the last recorded helper output. Subsequent game/user changes
are never overwritten. If it prints manual_review_required, Workshop links
still work but the local file stays untouched. Keep the printed recovery folder
for investigation. Do not manually replace changed files on guesswork.

No backups found: use the actual original configured package, not a fresh ZIP.
The backups/test-mod-isolation-* folders must remain in place. No records means
this tool cannot reconstruct the helper's affected-ID list safely.

The patch does not launch X4, call Steam APIs, change subscription/account files,
read normal saves, change normal profiles, or edit/delete installed mods. It
writes only package-local reports/backups and the guarded 45841 selection rollback.
--report-only creates the report without rolling back the selection.
The HTML checkboxes are temporary notes, not live subscription status.

DO NOT resume this multiplayer test profile or use the old Fix-1 ZIP. A reviewed
replacement is required before further mod-isolation testing. Restoring local
settings alone cannot guarantee that Steam subscriptions were restored or that
a subsequent X4 launch will preserve them.

Validation: synthetic filesystem/rollback/failure tests on Linux; no real Steam
subscriptions changed and no native Windows execution claimed. Please report
whether the links recovered the missing subscriptions. If local rollback was
refused, share recovery.json and the two content XMLs in the printed recovery
folder privately (they contain local paths/mod lists, not game session keys).
