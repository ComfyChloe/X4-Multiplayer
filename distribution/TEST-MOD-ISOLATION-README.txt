X4 Multiplayer 7.2.0 INTERNAL - Windows test-mod isolation helper

The original installer creates config.xml but does not create the test profile's
content.xml. A separate profile alone does not disable other installed mods.
This helper prepares an isolated mod selection for profile 45841.

1. Close X4 completely on the Windows PC.
2. Extract this ZIP's files into the EXISTING configured Windows 7.2.0 package
   folder, beside 1-INSTALL.cmd, installation.json and the runtime folder.
   Do not put the files into the game installation or the Documents folder.
3. Run 8-ISOLATE-TEST-MODS.cmd. No administrator privileges or downloads needed.
4. On success, close BOTH games and stop the host relay too. Start the relay
   again, then use 3-START-X4-TEST on both PCs for fresh test worlds.
   Check Escape/options and pause/resume before continuing the gameplay test.

The helper reads the exact profile/game paths from installation.json and verifies
that the installed multiplayer mod belongs to this package. It creates missing
45841/content.xml, or updates and backs up an existing file. It enables x4_lan_lab,
disables Ventures and other third-party extension IDs, and retains existing
disabled official DLC choices. Previously unspecified installed official DLCs
remain enabled. Both PCs must still have matching official DLC selections.

Extension IDs are read from installed manifests, including compressed manifests
and game/profile/shared-user extension directories. Folder names are not used as
substitutes for IDs. A malformed manifest is reported before settings are written.

Normal profiles, saves, installed mod files and session credentials are not
modified. No old package files are replaced. Original release ZIPs are unchanged.
Run with --check from a terminal for a read-only preview.

Recovery: with X4 closed, the printed backups/test-mod-isolation-* directory
contains the exact previous content.xml, if one existed. Copy it back to the
printed test profile to restore the old selection. If BEFORE-WAS-ABSENT.txt exists,
only this helper's newly created 45841/content.xml needs removing to restore that
prior state. The change.json record includes before/after hashes. Do not restore
over subsequent user edits without reviewing them.

Validation: focused file/ownership/isolation tests passed on Linux. Native Windows
execution and resolution of the reported menu failure still need the human test.
The guest log shows a failed options-menu creation plus a duplicate UI extension;
this helper isolates that suspected interaction. It does not prove which mod
caused the failure and does not implement save transfer or whole-universe sync.
