# Attention load test: simulation cost per high-attention sector

Status: built and tested offline (LuaJIT doubles, synthetic logs, temporary install
trees). Not yet run natively. This is also the first native run of the shared-attention
hooks (attention-parity branch, with the ASLR fix for vtable signature hashing).

## Goal

One Windows X4 instance forces N sectors to full simulation detail at once by
registering N NPC ships as remote-player attention proxies (zone level 8, proxy and
ships in range level 9, as the local player does). The fixed-tick probe measures the
per-frame cost of MoveWorker slots 1..14. The default schedule is

| phase | proxies | duration |
|---|---|---|
| baseline | 0 | 60 s |
| load | 8 (8 distinct busy sectors) | 180 s |
| done | 0 (sources cleared) | rest of the run |

The cost per added sector is `(mean sim ms at N=8 - mean at N=0) / 8`. It bounds the
dedicated server's practical player limit. Other schedules (for example
`--steps 0,1,2,4,8,0 --durations 60,60,60,60,60,60`) are installer options.

## Components

| file | installed as | role |
|---|---|---|
| `.local/evidence-053-withheld` (built by `native/hooks/build.py`) | `native/x4lan_hooks_64.dll` | observer slot 12, shared attention vtable wrappers |
| `extension/x4_lan_lab/ui/engine_hooks.lua` | `ui/engine_hooks.lua` | hooks adapter; publishes proxy IDs to the DLL |
| `dev/attention_load_test.lua` | `ui/attention_load_test.lua` | driver: picks sectors and proxies, logs `ATTNLOAD` |
| `dev/attention_load_test.xml` | `md/x4lan_attnload.xml` | read-only MD helper: sector ranking, ship/station lists |
| config overlay | appended to `ui/config.lua` | test-only settings (below) |
| `dev/attention_load_report.py` | (not installed) | per-step table and CSV |

The game view already has `ui/fixed_tick_probe.lua` and `x4lan_fixedtick_64.dll`
(VBI slots 0, 5 and 15). The hooks use slot 12 only (`table[12]` and `table[29]` must be
empty). RNG-marker slots 2/5 from other branches are not part of this install. In probe
mode the fixed-tick DLL patches no code. Both DLLs verify the same X4.exe: the
signature check passes on the game view's X4.exe (exact build, 85 hook signatures).

## How proxies are chosen and replaced

1. Wait until `HOOKS_READY` (adapter armed), then `attention_load_warmup` (20 s).
2. **Rank**: the MD helper lists every sector in the galaxy with station and ship
   counts. Score = ships + 10 x stations. The player's sector and
   `attention_load_exclude` are skipped. `attention_load_sectors = {...}` replaces the
   ranking with a fixed list.
3. **Prefetch**: the top `maxN + 4` sectors are streamed (the sector, its stations and
   up to 600 ships). Candidates are operational, not player-owned, not the player's
   ship and not XS-class (drones). Docked ships come first by default because they stay
   in the sector longest (`attention_load_prefer = "undocked"` or `"any"` changes that).
4. **Step N**: the first N ranked sectors that have a candidate each get one proxy (one
   proxy per distinct sector). The IDs go to `X4LAN_ATTENTION_LOAD.sources`, and
   `engine_hooks.lua` passes them to `set_remote_attention_sources` within one frame.
5. **Every second**, each proxy is checked: it must still be operational and still in
   its slot's sector. A dead (`reason=dead`) or departed (`reason=left`) proxy is replaced
   by the next valid candidate of the same sector. If the list is exhausted, the sector
   is re-streamed once (`pending=refresh`). If that finds nothing, the slot moves to the
   next unused ranked sector (`resector`). A registration that the DLL refuses
   (`HOOKS_ATTN_REFUSED`) blacklists the newly added IDs and replaces them.
6. **Census** every 20 s (5 s after each step change): the proxy sectors (in the
   baseline, the sectors that step 8 will use) and the player's sector are streamed. The
   attention level of every listed ship and station, and of the sector, is read with the
   read-only `attention_level`.

MD requests run one at a time. Each request is bracketed by `scan_begin` / `scan_end`, and
the report drops the FRAMEPROBE windows that overlap a scan.

## Adjustments for profile 45842 (test install only)

| adjustment | why |
|---|---|
| `attention_load_profile = "45842"` (honoured only with `attention_load_test = true`) | `engine_hooks.lua` refuses to arm unless `GetSaveFolderPath()` ends in `/45841/save`. The game view runs `-personalfolderid 45842`. Values other than 45841/45842 are rejected. |
| `test_profile` stays `"45841"` | the config guard in `engine_hooks.lua` and `fixed_tick_probe.lua` both require it. The installer refuses a config that changes it. |
| `enabled = false` required | no networking or bubble adapter. With no `X4LAN_BUBBLE_API`, the driver is the only proxy source. |
| `engine_hash_enabled = false`, `engine_hash_cadence = 3600` | STATEHASH capture runs in the observer (slot 12), inside the measured slot window. Earlier smoke runs spent 8 ms (budget) to 750 ms (full) per capture. Off, the observer only counts frames and syncs attention. |
| `HOOKS_ATTN_REFUSED` instead of `HOOKS_DISABLED` on a refused proxy set (load test only) | a proxy dying between the driver's check and the DLL's validation would otherwise disarm the whole module mid-run. The previous sources stay active and the driver replaces the refused proxy. |
| Protected UI must be off | `package.loadlib` needs it. It is already off in profile 45842, since the fixed-tick probe DLL loads there. The hooks adapter checks it again (`disable Protected UI mode`). |

Without `attention_load_test = true`, none of these paths changes behaviour. The
existing hooks tests and the new ones in `tests/test_engine_hooks.py` cover this.

## Native run procedure

X4 must be closed. Run these commands from the attnload worktree
(`C:\Users\developer\Documents\X4Mods\X4-Multiplayer-attnload`) in a Windows shell.

1. Build the DLL and check the executable:
   ```
   python native/hooks/build.py --test
   python dev/check_hook_signatures.py "C:\Users\developer\Documents\X4Mods\X4-Multiplayer-fixed-tick\.local\sandbox-baseline\host\game-view\steamapps\common\X4 Foundations\X4.exe"
   ```
   Expect `PASS ... exact build and 85 hook signatures`.
2. Install. The default `--extension` is the game view's `x4_lan_lab`:
   ```
   python dev/install_attention_load_test.py install
   python dev/install_attention_load_test.py status
   ```
   The backup goes to `...\X4 Foundations\x4lan-attnload-backup` (outside `extensions`).
3. Move the previous debug log aside so the report reads only this run:
   `Documents\Egosoft\X4\45842\x4lan-debug.txt`. This is the user's step; the tools never
   touch `Documents\Egosoft`.
4. Launch `C:\Users\developer\Documents\X4Mods\X4-Multiplayer-fixed-tick\.local\sandbox-baseline\host\launch45842.cmd`
   (custom_creative new world, `-debug scripts -logfile x4lan-debug.txt`).
5. Start the new world. Leave the player ship idle in its start sector and do not pause
   or open the map. Do not use SETA.
6. Let it run about 7 minutes after the world appears: 20 s warmup, a few seconds of
   prefetch, a 60 s baseline, 180 s with 8 sectors, then `ATTNLOAD done`. Quit X4 after
   `done` (about 4.5 minutes after `HOOKS_READY`). The extra minutes cover loading.
7. Run the report:
   ```
   python dev/attention_load_report.py "%USERPROFILE%\Documents\Egosoft\X4\45842\x4lan-debug.txt" --csv .local\attnload\report.csv
   ```
8. Restore the game view:
   ```
   python dev/install_attention_load_test.py restore
   ```
   This prints `restored ...`. `status` then says `not installed`.

## Expected log lines (in order)

```
[X4LAN] ATTNLOAD loaded
[X4LAN] FRAMEPROBE armed in MoveWorker slot 5 mode=probe
[X4LAN] HOOKS_READY slot=12 cadence=3600 run=attnload session=... budget_ms=8.000 hashing=0 profile=45842
[X4LAN] ATTNLOAD armed warmup=20 steps=0,8 durations=60,180 prefer=docked
[X4LAN] ATTNLOAD scan_begin seq=1 kind=Rank sector=-
[X4LAN] ATTNLOAD scan_end seq=1 kind=Rank sector=- ms=...
[X4LAN] ATTNLOAD ranked sectors=<n> player=<macro> top=<macro:score,...>
[X4LAN] ATTNLOAD scan_begin/scan_end ... kind=Sector ...            (12 prefetches)
[X4LAN] ATTNLOAD step=0 index=1 sectors=- proxies=- duration=60 available=0 t=... gt=...
[X4LAN] ATTNLOAD tick step=0 t=... gt=... proxies=- attn_sources=0 attn_resolved=0 attn_raises=...
[X4LAN] FRAMEPROBE frames=... simHooks=1 simFrames=... simMeanMs=... simP95Ms=... simMaxMs=...
[X4LAN] ATTNLOAD census round=1 step=0 role=baseline sector=<macro> ships=.. stations=.. read=.. hi9=.. sector_level=.. levels=a.b.c...
[X4LAN] ATTNLOAD census_total round=1 step=0 proxy_sectors=0 hi9=0 player_hi9=..
[X4LAN] ATTNLOAD step=8 index=2 sectors=<8 macros> proxies=<8 ids> duration=180 available=8 ...
[X4LAN] ATTNLOAD tick step=8 ... proxies=1:<id>@<macro>,...,8:<id>@<macro> attn_sources=8 attn_resolved=8 ...
[X4LAN] ATTNLOAD census ... role=proxy ... hi9=<large> sector_level=5 ...
[X4LAN] ATTNLOAD replace slot=3 sector=<macro> old=<id> new=<id> reason=dead|left|refused   (occasionally)
[X4LAN] HOOKS_STATUS frames=... attn_sources=8 attn_resolved=8 attn_raises=<rising> ...   (every 5 s)
[X4LAN] ATTNLOAD done t=... gt=...
```

`HOOKS_STATUS` with `attn_resolved=8` and a rising `attn_raises`, together with `hi9` in
the proxy-sector census well above the baseline census of the same sectors, shows that the
proxies really raise attention. Without that, a flat cost curve proves nothing.

## Report output

One row per step (`phase=post` after `done`): clean one-second windows, excluded
windows, fps (mean `simFrames` per window), frame-weighted `simMeanMs`, median and max of
the per-second `simP95Ms`, max `simMaxMs`, mean census `hi9` in the proxy sectors and in
the player sector, mean `attn_resolved`, replacements, `delta_ms` against the baseline,
`per_sector` = delta / occupied sectors (`occ`, from `available`), and the marginal cost against the previous step. The first 10
windows after each step change are dropped (`--settle`), as are scan windows and paused
windows.

## Failure modes

| symptom | meaning / action |
|---|---|
| `HOOKS_DISABLED reason=inactive outside isolated profile 45841` | the overlay is missing (`status`) or `attention_load_test` is not true. Reinstall. |
| `HOOKS_DISABLED reason=inactive outside isolated profile 45842` | X4 did not run with `-personalfolderid 45842`. Use `launch45842.cmd`. |
| `HOOKS_DISABLED reason=disable Protected UI mode` | turn Protected UI off in profile 45842 (Settings, Extensions), restart. |
| `HOOKS_DISABLED reason=unsupported executable or modified engine signatures` (or a signature reason) | the X4 build changed. Rerun `check_hook_signatures.py`; do not continue. |
| `HOOKS_DISABLED reason=VBI slot occupied or unavailable` | another module owns slot 12 (or 29). Check that only fixed-tick (0/5/15) and hooks (12) are installed. |
| `HOOKS_DISABLED reason=observer disarmed` | the hooks lost their slot (world reload or loading screen). Restart the run in a new world. |
| `HOOKS_DISABLED reason=<vtable/attention reason>` at start | shared attention could not take a vtable slot (foreign replacement). Record the reason; this is first-run evidence for the ASLR fix. |
| `ATTNLOAD error reason=hooks_disabled` | follows any `HOOKS_DISABLED`; the driver stops and clears its sources. |
| `ATTNLOAD error reason=hooks_adapter_missing` | `ui/engine_hooks.lua` is not loaded (ui.xml) or `engine_hooks` is not true. |
| `ATTNLOAD scan_timeout` x3, then `error reason=md_helper_unavailable` | `md/x4lan_attnload.xml` is not loaded or has an MD error. Search the log for `X4LAN_AttnLoad` script errors. |
| `ATTNLOAD short step=8 wanted=8 available=<n>` | fewer than 8 sectors had candidates. The report divides by `available` (column `occ`). Record it. |
| `HOOKS_ATTN_REFUSED reason=...` repeatedly | proxies are rejected by the DLL validation. Note the reason. The driver replaces them, but persistent refusals mean no load. |
| `FRAMEPROBE ... simHooks=0` | the sim brackets (slots 0/15) were occupied. The report excludes these windows. Without clean windows there is no result. |
| No `ATTNLOAD` lines at all | the driver is not in ui.xml or `attention_load_test` is not true. Check `status`. |

Restore always runs, also after a failed run. If a managed file changed during the run,
restore notes it and still puts the previous version back.

## Offline evidence (this branch)

- Rust hooks tests 15/15 PASS (`python native/hooks/build.py --test`).
- `dev/check_hook_signatures.py` PASS on `x4-re/bin/X4.exe` and on the game view's X4.exe.
- LuaJIT driver tests (fake hooks and MD helper): `tests/test_attention_load_test.py`.
- Hooks adapter tests including the load-test paths: `tests/test_engine_hooks.py`.
- Report parser on a synthetic log: `tests/test_attention_load_report.py`.
- Installer backup, merge and byte-exact restore in temporary trees:
  `tests/test_install_attention_load_test.py`. A dry run on a temporary copy of the game
  view's `x4_lan_lab` was also byte-identical after restore.

None of this is native evidence. The MD helper syntax and the MD-to-Lua parameter
passing follow the existing divergence recorder, but they have not run in X4.
