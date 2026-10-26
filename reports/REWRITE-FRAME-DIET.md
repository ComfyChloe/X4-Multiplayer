# Server frame diet (dedicated server mode)

Owner: frame-diet agent, branch `contrib/ferrite-rw-frame-diet` (base `contrib/ferrite-rewrite-base` e63d9cf).
Build: X4 9.00, Steam build 23660954 (Windows RVAs; Linux VAs in bin-linux/X4 where given).
Status 26 Oct 2026: module, audit mode and tests done; native functional audit done on a lane; the timing
measurement waits for a quiet window (rewrite-coord Q9). Terminology: the dedicated X4 is the **server**,
players connect as **clients**.

## Summary

- A dedicated server spends a large share of every MoveWorker frame on presentation. In the 8-sector attention
  run (25 Oct, simprof folds) slot 6 (UI) costs 5.3 ms of a 17.3 ms frame at baseline and 6.2 of 29.7 ms under
  load, but the Lua part of it (all UI Lua including the mod's networking) is only 0.6 / 0.8 ms. Slot 10 (render
  preparation) costs 2.3 / 2.6 ms, of which the camera render is 2.1 / 2.3 ms.
- `native/engine/framediet.c` (`x4lan_framediet_64.dll`, opt-in) skips presentation work through data-only,
  build-pinned, fail-closed hooks while the simulation, MD, AI, input and every UI Lua script run unchanged.
- **Behaviour-identical set (`server`)**: the Anark scene layer walk (`ui_scenes`, ~1.9 / 2.0 ms) plus the
  engine's own `disable_rendering` flag for the main-thread draw (`draw`). The native audit found no simulation
  side effect of the layer walk: 0 thread-RNG steps, 0 UniverseIDs, 0 changed component words in 1.36 M
  snapshots over 40,000 calls.
- **Not behaviour-identical**: the camera render (`camera`, ~2.1 / 2.3 ms). It draws about 150 MoveWorker
  thread-RNG steps per frame, allocates UniverseIDs for mass-traffic welding effects and sets a view-cache flag on
  every rendered component. Skipping it changes the RNG stream and later IDs relative to a client that renders.
  The camera view is therefore an input of the simulation RNG stream on every X4 (a parity leak). The new
  `rng_split` site moves slot 10's draws to a private stream (proposal of the determinism owner).
- Expected saving of the behaviour-identical set: ~1.9-2.0 ms per frame on the MoveWorker (11 % of the
  baseline frame, 7 % under load) plus the whole main-thread draw; with `camera` and `ui_render` another
  ~2.3-2.6 ms. Measured numbers: section 6 (pending the quiet window).

## 1. Attribution (where the server's frame goes)

Source: `X4-Multiplayer-fixed-tick/.local/evidence-238-withheld`,
per-window `SIMPROF fold` lines; baseline = windows ending 40-80 s, load = 110-260 s; ms per MoveWorker frame =
samples (1 kHz) / frames (FRAMEPROBE simFrames). Reproduce: `python native/engine/framediet_analyze.py
attribution <log> --window 35:81 --window 105:261`.

| work | baseline ms (share) | load ms (share) | diet |
|---|---|---|---|
| frame (wall per MoveWorker frame) | 17.26 | 29.69 | |
| slot 6 total, UI::CUIController::Move 0xb3bad0 | 5.27 (30.5 %) | 6.21 (20.9 %) | |
| - Lua: every stack with a lua51_64 frame (game UI + mod) | 0.59 (3.4 %) | 0.82 (2.7 %) | keep |
| - Anark gameface element update 0x1399c40 | 1.85 (10.7 %) | 1.95 (6.6 %) | next candidate (section 8) |
| - Anark scene layer walk (SceneManager slot 3 0xaf8e50 -> AnarkScene slot 12 0xaf8760) | 1.87 (10.8 %) | 2.01 (6.8 %) | `ui_scenes` |
| - ICom message pump 0xae9880 (VisibilityData::Update 0xadcfa0) | 0.75 (4.3 %) | 1.20 (4.1 %) | keep |
| slot 10 total, X4::GameRender::Move 0x121c1f0 | 2.30 (13.3 %) | 2.58 (8.7 %) | |
| - camera render, U::Camera slot 3 0x983160 via GameClient render bank 0x122bfc0 | 2.10 (12.2 %) | 2.34 (7.9 %) | `camera` (opt-in) |
| - UI render bank, CUIController::Render 0xb3bba0 | 0.19 (1.1 %) | 0.23 (0.8 %) | `ui_render` (opt-in) |
| Component::BuildCache 0x3d4a80 | 3.85 (22.3 %) | 3.68 (12.4 %) | slot 3, not presentation (transforms) |

BuildCache sits entirely under `U::UniverseFrameUpdate` in slot 3 (not on the camera path); it fills the view
caches that pathing jobs and MassTraffic read, so it is simulation work (owner: transforms). Slot 8 (character
animation) and slot 9 (sound) are 0.4 ms each under load.

## 2. What the simulation and the mod need from slots 6 and 10

Slot 6 is `CUIController::Move` (0xb3bad0, CS +0x70 for the whole call): ticker toggles, the ICom message
pump 0xae9880, `CXAnarkController::Move` 0xb36300, then input-context updates on `g_InputSystem`.
`CXAnarkController::Move` (gated on +0x3f0, up to 3 passes while +0x3ee): gameface slot 12, the presentation
loop with gameface slot 13 (0x13a5f90 -> 0x138f2c0), anchored-element/camera sync, gameface slot 14 and the Lua
event `updateScenesDone`. Inside 0x138f2c0 (vtable 0x2ca2988): time update 0x1399550, clearing of dirty lists,
0x138fa90 (contains script event dispatch, 0x13b2aa0 -> Lua), the **element update** 0x1399c40 (only when the root's
subtree-dirty flag 0x2000 at +0x2c is set), and the **Lua per-frame dispatch** `(+0x38)->vfunc5` =
`AnarkLuaEngine::Update` 0xaf85e0 -> `DispatchFrameScripts` 0x13a1270 -> `onUpdate` (the mod's drivers run from
`widget_fullscreen.lua`'s onUpdate via `CallUpdateScripts`). The scene manager path (slot 3 0xaf8e50 -> per
scene slot 5 0x13aa510 -> slot 12 0xaf8760) contains no Lua at all in the profile.

Therefore slot 6 can never be skipped wholesale, nor CXAnarkController::Move, gameface slot 13 or 0x138f2c0.
What the mod's Lua uses (inventory of `extension/x4_lan_lab/ui/*.lua`): component-state FFI (positions,
contexts, operational, IDs), `SetObjectSectorPos`, `SpawnObjectAtPos2`, `RemoveComponent`, `RegisterEvent` /
`AddUITriggeredEvent` (MD <-> UI events), `GetComponentData`, time, save list, `SetObjectForcedRadarVisible`.
None of it reads Anark scene, element, render-list, holomap or camera state.

Slot 10 is `GameRender::Move` 0x121c1f0: `Render` (vfunc2) of the four render-bank entries (VbiTable
+0x110..+0x128: UI, character animation, GameClient), then a release of `*(0x6adfc30)` (graphics config object).
The GameClient bank 0x122bfc0 renders every camera of the universe camera list (Universe+0x2c0; the engine
itself skips that loop while its render pause GameClient+0x2a8 is armed) and then `(+0x2a0)->vfunc3` when that
object's +0x70 == 1. Camera::Render 0x983160 -> `U::Scene::Render` (vfunc915, 0x779a00; "Scene::Render(): No
scenecontext found") -> per component `Positional::vfunc693` 0x751770 (frustum culling, LOD, draw items into the
producer half of the double-buffered render list, see x4-re render-decouple.md) and `HoloMap::vfunc915` 0xe197f0.

Simulation-visible side effects of slot 10 found statically and confirmed natively (section 5):
- **Thread RNG**: `Random::FloatState` 0x14ddc90 on TLS+0x318 (the MoveWorker stream that AI chance rolls, MD,
  movement controllers etc. also draw from) in the scene render helper 0x77b890 (per rendered zone: a random
  time offset scaled by game speed), `maybe_U::SpawnEffect` 0xd73e00 and the effect creator 0xd717a0.
- **Components and UniverseIDs**: `Zone::vfunc693` 0x8429a0 -> 0x82f1e0 -> 0x8aba40 ->
  `U::MT::WeldingMassTrafficBehavior::vfunc16` 0x89a7d0 -> SpawnEffect -> `CreateEffectObjectCreator` 0xd73060
  (AssignNewID 0xcc220, PostEvent) -> 0xd717a0 (`Component::ConnectTo` 0x3d6e50, SetLocalTransform): visual
  welding effects of construction traffic become components in the tree.
- **View-cache flag**: the render helper 0x74ff70 sets bit 0x100000 in the flags word of the component's view
  cache (`or [comp+0x11c+idx*0x80], 0x100000` at 0x75049a) after its LOD classification. Readers unknown
  (rewrite-coord Q11 to transforms).

Slot 6 Anark functions (all 573 sampled ones) call no `Random::*` helper; the only UniverseID allocation in slot
6 is `Lua::CreateSource` 0xb0e8a0 -> `CMessageManager::CreateSource` 0xae9050 (UI ICom message sources take IDs
from the universe ID map), inside the Lua dispatch that the diet keeps.

## 3. Sites

| site | hook (all data-only) | what is skipped | equivalence |
|---|---|---|---|
| `ui_scenes` | AnarkScene vtable 0x2bc0e10 slot 12 (entry 0x2bc0e70, fn 0xaf8760), skipped only inside AnarkSceneManager slot 3 (entry 0x2bc0dd8, fn 0xaf8e50) on the MoveWorker | the per-frame layer walk; slot 5 still clears the scene's per-frame lists, so they stay empty (= the engine's state for a scene whose root is not visible); consumer is the UI render | identical (static + native audit) |
| `draw` | RenderFlags bit `disable_rendering` 0x8000000 of qword 0x6adfa30 and staged copy 0x6adfa38 (interlocked OR/AND of that bit only, re-applied each frame, released only if we set it) | main-thread `RenderPreviousFrame` 0xf9ff40 skips drawing the render targets (test at 0xfa007a); per-target bookkeeping, present and frame limiter still run | identical (main thread only reads the double-buffered half) |
| `ui_render` | CUIController XVbiHandler vtable 0x2bc07d0 slot 2 (entry 0x2bc07e0, fn 0xb3bba0) | UI draw-list build | no RNG/ID/event effect natively; per-frame UI queues consumed there are not yet checked for growth: opt-in |
| `camera` | U::Camera vtable 0x2bae1a0 slot 3 (entry 0x2bae1b8, fn 0x983160), skipped only inside the GameClient render bank (entry 0x2c8abe0, fn 0x122bfc0) | camera render-list preparation | **changes the RNG stream, IDs and view-cache flags**: opt-in |
| `rng_split` | X4::GameRender vtable 0x2c8ac08 slot 1 (entry 0x2c8ac10, fn 0x121c1f0) | nothing: slot 10 runs with TLS+0x318 swapped to a private render stream (seed "RENDERV1") | a determinism fix; effect objects created by the render still take IDs |

Per-frame guard: empty VBI slot 2 (before GameClient::Move). It re-verifies the table pointer, its own slot and
every installed vtable entry; any change disarms everything (entries restored by compare-exchange, a foreign
value is never overwritten, the draw bit released). Wrappers pass straight through on other threads (loading
mode runs the UI on the main thread), while loading, and when not armed. `period=N` presents every N-th frame
fully (an operator heartbeat). Linux counterparts of every site and of the RNG/ID side effects are in
rewrite-coord/questions.md Q10 (Linux vtable slot = Windows slot + 1; thread RNG at `fs:[-0x2720]`; AutoIDMap
fields +8).

## 4. Module

`native/engine/framediet.c`, built by `python native/engine/build.py --zig <zig> --framediet [--test]`,
never packaged. Lua: `start(mode, sites[, period[, audit_every[, digest_max]]])` returns a handle or
`nil, reason`; `handle:set(mode[, sites])`, `handle:stats()`, `handle:active()`, `handle:stop()` (also `__gc`).
Modes: `diet` (skip the active sites), `pass` (all wrappers installed, inert: A/B in one session), `audit`
(run the originals and measure). Sites: `ui_scenes`, `ui_render`, `camera`, `draw`, `rng_split`, `server`
(= ui_scenes,draw), `all` (= the four skip sites).

Safety: X4.exe verified by size, SHA-256 and PE header; 14 code signatures (signatures.py `FRAMEDIET_SITES`)
for every wrapped function, the render-flag test and copy, and the audit probes; every vtable entry must hold the
verified original before anything is written (all-or-nothing install); main Lua thread only; DLL pinned; state
kept in static storage (the Lua handle only holds a generation number, so a wrapper running on another thread
never touches memory the Lua GC freed); digest reads through ReadProcessMemory (a component freed meanwhile
yields a failed copy, never a fault). Registered in rewrite-coord/hooks.tsv.

Loader `native/engine/framediet_probe.lua` (inert unless `X4LAN_FRAMEDIET_MODE` is set and the config gate
`test_profile == "45841"` holds): retries while loading, loud `FEATURE framediet DISABLED reason=...` on any
refusal or disarm, `FRAMEDIET` stats every `X4LAN_FRAMEDIET_REPORT_S`, `FRAMEDIET_AUDIT` rows and
`FRAMEDIET_FRAMES` totals in audit mode, phase cycles (`X4LAN_FRAMEDIET_AB_S`, `X4LAN_FRAMEDIET_AB_CYCLE`, e.g.
`pass;server;all` or `audit:camera;audit:camera,rng_split`) for in-session A/B runs.

## 5. Equivalence evidence

Method. Skipping region R is behaviour-identical for the simulation if, for the same inputs, R writes nothing the
simulation reads. Audit mode tests that per call on the running game (diet off, originals run):
- thread RNG (TLS+0x318) before/after; the exact number of LCG steps is recovered by stepping (the generator is a
  bijection), bound 65,536;
- UniverseID insertions: AutoIDMap insert counter (+0x38 of `*(0x6cf1500)`);
- deferred event list size (`U::g_EventCommandQueue` 0x39943a8 +8; other threads also post, see control);
- TLS game time unchanged;
- component digest: the first 0x200 bytes (U::Component: identity, parent, children, attention, flags, timed
  event queue, both view caches) of up to 16,384 registered components (AutoIDMap slot order, rotating cursor),
  snapshotted before and compared after the region; refcount/lifecycle (+0x40) excluded;
- a control window of equal length in slot 2 measures what other threads change meanwhile;
- whole frames (slot 2 to slot 2): all simulation-stream RNG steps and ID insertions, for scale.

Synthetic (Windows, `build.py --framediet --test`): install/refusal/fail-closed lifecycle, skip only in context,
other threads and loading pass through, draw bit ownership, period, pass mode, audit counters (RNG steps, IDs,
events, word histogram, control), rng_split on/off. Real Lua ABI: `framediet_lua_abi.py`. Loader: 14 LuaJIT tests.

Native (functional, lane b1, other X4 running; `results/2026-10-26-frame-diet-audit.md`): 9,952 frames:

| region | RNG steps | IDs | events | changed components |
|---|---|---|---|---|
| ui_scenes (40,000 calls) | 0 | 0 | +8 in 2 calls (background) | 0 / 1,359,872 |
| ui_render (9,952) | 0 | 0 | 0 | 1 freed block |
| camera (19,904) | 1,497,601 (~150 per frame) | 418 | 0 | 446 (+0x198 flags word 271x) |
| control (82) | 0 | 0 | 0 | 54 (view-cache words of other threads) |

Interpretation: with fixed per-frame inputs (game time from the fixed-tick stage, same session RNG), the `server`
set leaves every probed simulation channel bit-identical, so runs with and without it differ only through
wall-clock-dependent engine behaviour (thread timing of Gravidar/MassTraffic/pathing), the same class of
difference as running on a faster PC. Removing slot 10's camera work on the server instead changes the RNG
stream relative to every client that renders (and IDs where welding traffic is in view); with `rng_split` on
the server and on all clients the RNG streams stop depending on the view. In-session A/B of rng_split:
audit-002 (pending, section 6).

## 6. Measurements

Pending: the quiet-window timing run (Q9): attnload steps 0/8, phases `pass` / `diet:server` / `diet:all`
every 30 s, slot-loop ms and fps from the fixed-tick probe per phase (`framediet_analyze.py phases`).

## 7. What a headless Linux server gets for free

Nothing in slots 6 and 10: the Linux X4 under Xvfb + lavapipe runs the same MoveWorker loop (x4mods-61's VPS
profile: slot 6 21 %, slot 10 9.8 %) and the same RNG coupling (`fs:[-0x2720]`). `-disableui` removes the Lua
state and with it the mod. What it does get, without hooks:
- the main-thread draw can be dropped by its own no-render decision (x4mods-61's FrameState+0x200 read #2); with
  lavapipe this is CPU time on the same machine, so it matters more than on Windows;
- supported launch switches (parsed in 0x1221840 / 0x121df00): `-disablesound` (and `-disablemusic`,
  `-disablevoicesounds`, `-disableambientsounds`, `-disableuisounds`, `-disableeffectsounds`),
  `-disableventures`, `-disableventureupdate`, `-noantialiasing`, `-disablecockpit`,
  `-disableplayershipgeometry`, `-disableallplayershiprendering`, small `-width`/`-height`; their MoveWorker
  effect is unmeasured;
- the Linux split agreed with x4mods-61: they implement the Linux switches (rust/x4mp-agent), this branch owns
  the Windows module, the audit and the safe/unsafe list (Q10).

## 8. Next client-only costs

1. Anark element update 0x1399c40 (~1.9 / 2.0 ms): direct call at 0x138f477 (E8 rel32) inside 0x138f2c0, whose
   caller also runs the Lua dispatch. Data-only hooks cannot reach it; a call-site rel32 patch through a near
   stub (as ai-driver does) can skip or throttle it (every k-th frame; the update is time-based). UI-only effect:
   animations and attribute propagation stall or step coarsely; UI Lua reading element state sees older values.
2. ICom message pump 0xae9880 / VisibilityData::Update 0xadcfa0 (0.75 / 1.2 ms): UI data and UI events; needs
   RE of which messages reach Lua/MD before anything is skipped.
3. Slot 8 character animation, slot 9 sound (0.4 ms each under load): `-disablesound` first, then measure.
4. BuildCache (3.7-3.9 ms): if Q11 shows the render-set flag drives render-only cache work, a server without
   camera rendering would also save BuildCache time (owner transforms).

## 9. Running it

```
python native/engine/build.py --zig <zig> --framediet --test
python native/engine/framediet_lua_abi.py --lua-runtime <lua51_64.dll copy> --module .local/evidence-037-withheld
x4lane run --agent frame-diet --overlay <wt>\.local\lane\overlays\probe --overlay <wt>\.local\lane\overlays\framediet ^
    --lua "X4LAN_FRAMEDIET_MODE='audit'" --lua "X4LAN_FRAMEDIET_SITES='ui_scenes,ui_render,camera'" ^
    --until "\[X4LAN\] FRAMEDIET_AUDIT row=control" --until-count 30 --timeout 1200 ^
    --expect "\[X4LAN\] FRAMEDIET armed" --fail-on "FEATURE framediet DISABLED" --collect <wt>\.local\lane\runs\audit-001
python native/engine/framediet_analyze.py audit [--phases] <collect>\x4lan-debug.txt
```
The framediet overlay holds `native/x4lan_framediet_64.dll`, `ui/framediet_probe.lua` and `ui.xml.files`.

## 10. Open questions and native steps

- Q11 (transforms): readers of view-cache flag bit 0x100000 set by the camera render.
- `ui_render`: per-frame UI queue growth when skipped (commit size over a long diet run).
- Whether `CMessageManager::CreateSource` IDs (UI message sources) differ between server and clients in practice.
- Native steps: audit-002 (rng_split on/off), a diet stability run (`server`, then `all`, 10+ min, Lua and frames
  keep running, no `FEATURE ... DISABLED`), the quiet-window timing run (Q9).
