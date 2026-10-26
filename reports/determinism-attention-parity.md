# Deterministic background simulation — Stage 3 attention parity

25 October 2026 | Windows X4 9.00, Steam build 23660954 only
Branch: `contrib/ferrite-attention-parity`, based on `contrib/ferrite-engine-hooks` (4b2458f).
Status: static analysis, offline unit tests and a signature check only. **No native run.**
Nothing in this report is native evidence.

## Goal

For every object, attention (`Component+0x1ec`, levels 1–11) should be the maximum of
what the engine would compute for each player of the session. Both games then hold the
same level for the same object at the same game time, whichever player is local.

## Attention input map

Static analysis of `x4-re/bin/X4.exe` (private Ghidra copy `x4-re/ghidra-parity`, scripts
in `x4-re/scripts/parity`, decompiler output in `x4-re/work/parity/out`, not shared).
Every input below is read inside a class's `ComputeAttention` (vtable slot 703, +0x15f8),
which `SetAttention(c, 1)` (0x3d5660) calls.

| Input | Where it enters | Levels | Confidence |
|---|---|---|---|
| Local player entity `Universe+0x230` (Universe pointer at 0x3d198f8) and its parent chain | Cluster 0x3bb7f0, Sector 0x78d6a0, Zone 0x843b20, Controllable 0x4640e0, Ship 0x7d5110, Positional 0x753350, Room 0x76e520, WalkableModule 0x80e590, CutsceneAnchor 0x4b9cc0 | 3, 5, 8, 9, 10, 11 | high |
| Player position relative to the zone (GetTransformRelativeTo 0x3db8a0) | Zone: adjacent zones 7 inside `defaults(zone+0x60)+0x628` + 50 km (Chebyshev), else 6; Object 0x6d20b0: 9 within the object's attention radius (vfunc 0x1ea8: ships/asteroids 3000 m, stations 250 m, default −1) via distance 0x3e0c40, only in an active zone that is the direct parent and in the player's cluster | 6, 7, 9 | high |
| Zone adjacency (vfunc 0x1db0 → connection type 6, 0x34ecd0) and superhighways (vfunc 0x1d08, 0x5f88c0; Highway::vfunc947 0x5fe9c0 for the sector at each end) | Zone, Sector | 7, 4 | high (adjacency), medium (highway end sectors) |
| Render cameras `Universe+0x2c0[32]`, count `+0x3c0`, active `+0x3d0`; camera `+0x770` anchor, `+0x870` enabled | Same functions as the player (camera inside chain: 0x3e50e0; camera position via Camera::GetOffset) | same as player | high for the code path, medium for which UI cameras are registered (main view, external view and likely monitor/target cameras) |
| Docking state (vfunc 0x1f50 IsDocked; ship `+0x3e0` with connection 0xe to a bay whose defaults set `+0x603`) | Ship/Controllable: docked ships skip the player-chain 9; linked ships return the bay's attention first | inherit | high |
| Walkable modules: player or camera ≥ 1 km (distance-squared 1e6, 0x3e1930) | WalkableModule lowers an inherited level above 8 to 7 | 7 | high |
| Class family | Galaxy is constant 2; Sector `vfunc688` active when level > 3; Component/Positional inherit from the nearest ancestor with own attention | — | high |
| Physics-body eligibility | `WantsPhysicsBody` 0x352d10 → vfunc 0x1b60 with the attention level; Ship::vfunc876 0x7d7f60 needs level ≥ 7 with an active parent, then 250 km from the zone's physics origin **or 250 km from the local player** | — | high (attention part), medium (the 250 km player fallback is a direct player input) |
| AI attention bodies | `AIScriptContext_AttentionBlockChanged` 0x10664a0 compares the entity's `+0x1ec`; NPC pilots inherit from their control room / ship through SetAttention propagation (Component::vfunc701 0x3d5880) | follows the ship | medium-high |
| Low-attention jitter | Sector::Move 0x784230: periodic pass at `now + 5 + FloatState(TLS+0x318, 1)` below level 5, `now + 1` otherwise | — | high; it is a separate draw on the MoveWorker stream, not a separate stream |
| Object refresh timer | Object::Move 0x6ccf90 → 0x6d1ff0: objects with radius > 0 in an active zone recompute every `0.75 + FloatState(TLS+0x318, 0.5)` s | — | high |
| Re-evaluation triggers | Zone::Move 0x834950 recomputes zones above 5 every frame; ConnectTo 0x3d6e50 recomputes the new parent only for the player chain; objects use the timer above | — | high |
| Gate side effects | Gate::vfunc703 0x5cd4f0 drives local gate effects (active camera, 2/20 km) before Object::vfunc703 | — | medium |

## Implementation

`native/hooks/src/attention.rs` (new). `lib.rs` keeps a small set of call sites.

- **51 ComputeAttention slots** (exhaustive `.rdata` scan): Zone/Highway 2, Ship 7,
  Sector 1, Cluster 1, Controllable 5, Object 27, Gate 1, Room 4, WalkableModule 3. Each
  wrapper calls the original and returns `max(original, remote)`. The remote level applies
  the engine rules above to up to 8 remote proxy ships, using the engine's own functions.
  A proxy is its own chain bottom, so "inside the chain" means the object is the proxy or
  one of its ancestors. Remote sources count only on the owner (MoveWorker) thread while
  the module is armed and not loading.
- **Region refresh:** each observer frame tracks the zone/sector/cluster IDs of the local
  player and of every remote source. A change recomputes the old and new cluster, its
  sectors, the old and new sector and its zones, and the zones themselves through
  SetAttention. It runs for the local player too, so both games trigger the same
  recomputations. Stale pointers are never kept; IDs are resolved through the registry.
- **Sector::Move wrapper (slot 674):** when a low-attention pass drew exactly one generator
  step, the wrapper restores the MoveWorker RNG state and writes `now + 5 + j`. `j` is
  seeded from the session seed, the sector position in its cluster, the cluster position
  and the previous due time. Component IDs are not used. Other draw patterns are left alone
  (`jitter_skipped`).
- **Signatures:** 58 new manifest entries (15 code sites, 43 slots); 85 in total. Runtime
  verification compares slot values with `IMAGE + target`.
- **Bug fix in the base:** X4.exe has `DYNAMIC_BASE` and 702,952 relocations. The base
  branch's nine vtable-slot hashes would fail on any relocated load. They are now hashed
  after removing the load delta. Code sites contain no relocations; this was checked
  against the relocation table.
- **Lua:** `state.proxy` plus every ID in `state.proxies` go to
  `set_remote_attention_sources`; at most 8, otherwise fail closed. `HOOKS_STATUS` adds
  `attn_sources/resolved/raises/refreshes`, `jitter_fixed/skipped`, `cameras` and
  `camera_offchain`.
- **STATEHASH attention bucket:** sorted rows `sector|identity|kind|level` for ships,
  stations (`GetAllFactionStations`, read-only `attention_level`) and the sampled sectors.
  `attention=`, `attn_rows=` and `attn_levels=` (histogram of levels 0..11) are emitted;
  the combined `h` includes the bucket. `engine_hash_attention_rows = true` logs the rows
  as `ATTNROW`. `dev/compare_divergence.py --statehash` reports `BUCKET attention` and
  `FIRST ATTENTION MISMATCH` with both histograms and the differing rows. `--align-time S`
  pairs samples by nearest game time, which two simultaneous games need: their observer
  frame numbers are offset.

## Offline validation (not native)

- `python native/hooks/build.py --test`: 15 Rust tests PASS and the release DLL builds.
- `python dev/check_hook_signatures.py`: PASS, exact build and 85 signatures.
- Lua recorder tests (20) and STATEHASH comparison tests (10) PASS under LuaJIT/lupa.
- These tests cover the arithmetic, the hook table, vtable lifecycle, child-list iteration,
  parsing and reporting. They use synthetic memory and mock engine functions; no engine
  function was executed.

## Native test procedure (to run on the primary PC)

Preconditions: X4 closed, two Sandboxie compartment boxes as in `TWO-SANDBOX-X4.md`, both
in profile 45841 with autosave off, the same owned save in both, `-seed:123456789` on both
command lines, Protected UI off.

1. Build: `python native/hooks/build.py --test` and install `.local/evidence-053-withheld`
   as the extension's `native/x4lan_hooks_64.dll` in both boxes. Back up the previous DLL.
2. Configure both games identically: `engine_hooks = true`, `engine_hash_run = 'attn1'`,
   the same `build`, `engine_hash_cadence = 10`, `engine_hash_attention_rows = true`,
   `engine_hash_max_ships = 4000`, and `engine_hash_sectors` listing **both** players'
   sectors, for example `{'cluster_14_sector001_macro', 'cluster_01_sector001_macro'}`.
3. Start host and guest, join the session, and confirm in both logs: `HOOKS_READY`, no
   `HOOKS_DISABLED`, then `HOOKS_STATUS ... attn_sources=1 attn_resolved=1` once the NAV2
   proxy exists, and `jitter_fixed` rising.
4. Phases. Players stay in **different sectors**, ideally different clusters. Keep the
   camera in the cockpit and close the map and monitors, so `camera_offchain=0`. Note the
   game time at each phase boundary.
   - A. Both players stopped for 3 minutes.
   - B. Host flies past NPC ships and a station inside its sector, within 3 km and 250 m.
   - C. Guest jumps through a gate into a sector of the same cluster, and later to another
     cluster.
   - D. Guest uses a superhighway.
   - E. Negative control: guest opens the map or an external camera (`camera_offchain>0`).
   - F. Negative control: guest docks at a station (the proxy does not dock).
5. Collect both X4 logs with the `[X4LAN]` lines under `.local/evidence-113-withheld`, then run
   `python dev/compare_divergence.py host.log guest.log --statehash --align-time 0.2 --report .local/evidence-114-withheld`.
6. Control run: repeat phase A with the base branch DLL (4b2458f) or with the proxy removed.
   Expected result: persistent attention mismatches for objects near the remote player.

Compare and record per phase: `BUCKET attention differences=x/y`, the
`FIRST ATTENTION MISMATCH` rows, both `attn_levels` histograms, the HOOKS_STATUS counters
(`jitter_skipped` should stay 0), `HASHCOST`, and the frame time with and without the
remote player nearby.

Acceptance, attention only (other buckets are expected to differ):
- In phases A–D, after the first 10 s of each phase, at least 95% of time-paired captures
  have equal attention.
- Every remaining mismatch is a transition that disappears within 2 s. It is explained by
  proxy latency, the 0.75–1.25 s object timer or radius boundaries.
- No persistent 8-versus-9 or 5-versus-4 pattern around the remote player.
- In E and F, mismatches are expected and confirm the documented gaps.

## Open risks and unhooked paths

- **Cameras.** Local render cameras are engine inputs the other game cannot see. Monitor
  or external cameras raise objects locally. A full fix needs the camera anchor sent
  across the network (a protocol change), or suppressing camera-only contributions (which
  lowers the engine result and breaks "never lower").
- **Proxy fidelity.** The remote player is represented by a NAV2 proxy ship. It lags, is
  not docked when the real ship docks, and is not inside station rooms. Room/walkable
  levels follow it if it ever is, but on-foot and docked parity is not achieved.
  Boundary crossings happen at slightly different game times.
- **Not hooked.** The Positional room-root level 10 for station modules (63 Positional
  vtables), the Ship physics-body 250 km player fallback (vfunc 0x1b60, mostly relevant in
  zones larger than 250 km), CutsceneAnchor 11, and Gate local effects.
- **Object refresh timer** jitter stays on the shared MoveWorker stream. With equal levels
  and equal streams it matches. Otherwise objects switch up to 0.5 s apart; this belongs
  to the RNG-stream work.
- **Ordering.** Region refreshes run in slot order (local player first). The two games list
  the same players in different slot orders, so equal-time recomputations post
  ChangedAttention events in a different order.
- **Threads.** Remote raises apply only on the MoveWorker thread; ComputeAttention on any
  other thread returns the engine value. Not observed, but not excluded.
- **Engine calls.** Calling SetAttention from the observer callback and calling helpers such
  as the connection query or defaults lookup directly has not been exercised natively.
  Every helper is signature-pinned.
- **Performance.** Each game now simulates both players' surroundings at full detail:
  physics bodies, level-9 ship updates and in-sector AI blocks near both players. Expect
  roughly double the near-player cost, plus small per-call overhead (≤ 8 registry lookups
  per wrapped ComputeAttention). Measure before enabling for more than two players.
- **Sector identity.** The jitter seed assumes sector and cluster positions are identical in
  both games (same build and save). They come from the universe definition.
