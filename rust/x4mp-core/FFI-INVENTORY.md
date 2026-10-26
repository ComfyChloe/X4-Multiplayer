# FFI inventory for the motion core (build 23660954)

Exported C functions of X4 9.00 (611726), Steam build 23660954, that the Rust core needs
for motion, with their C signatures, presence on both platforms and confidence.
Static evidence only: no game was launched to produce this file.

## Sources and method

- Exports: Linux `x4-re/bin-linux/X4` (`nm -D --defined-only`, type `T`), Windows
  `x4-re/bin/X4.exe` (PE export directory). Both files match the pinned SHA-256 values in
  `src/pins.rs`.
- Signatures: `ffi.cdef` blocks in the game's UI Lua (`x4-re/gamelua/08`, `.lua` plus
  the readable string constants of `.xpl` bytecode), third-party UI mods in
  `x4-re/gamelua`, and the mod's own `extension/x4_lan_lab/ui/*.lua`. "Decls" counts
  declarations found; every listed signature was identical across all of them apart
  from parameter names and `UniverseID` vs `uint64_t`.
- ABI check: the Linux bodies of `GetObjectPositionInSector` (24-byte `UIPosRot` via
  hidden return pointer in `rdi`, id in `rsi`), `GetCurrentGameTime` (returns the TLS
  `CurTime` at `fs:-0x2728`, the same variable `x4mp-agent` pins), `IsGamePaused` (reads
  TimerBase) and `GetPlayerOccupiedShipID` were disassembled; they match the
  declarations. Thread safety from the RE notes `x4-re/coord/reports/simtick.md` §4 and
  `entity.md` §5 (Windows analysis): these exports use the component registry and the
  calling thread's TLS game time, no UI state, so they are valid on the MoveWorker thread
  in any VBI slot after 3 and **invalid on any other thread**.
- Pins: `src/pins.rs` holds, per export, the Linux VA / Windows RVA and the SHA-256 of
  the first 16 code bytes on each platform. `tests/pins_offline.rs` (both files) and
  `x4mp-sigcheck` (Linux) re-verify name -> address through the export tables and the
  code heads.

## Export sets

| | Linux `X4` | Windows `X4.exe` |
|---|---|---|
| Defined `T` (text) dynamic symbols / named exports | 20,835 | 2,378 |
| Plain C names (not `_Z…`/`_…`) | 11,250 | 2,378 |
| … starting with an upper-case letter (Lua FFI style) | 7,155 | 2,378 |
| Shared names | 2,376 | 2,376 |

Every Windows export except `AmdPowerXpressRequestHighPerformance` and
`NvOptimusEnablement` (GPU selection hints) also exists on Linux. **The cross-platform
FFI surface is therefore the 2,376 Windows exports**; the ~4,800 additional upper-case
Linux names (engine-internal C helpers, LuaJIT, OpenSSL, …) must not be relied on for
code that also has to run on Windows.

**39 shared exports are compiled-out stubs** in this release build (a bare `ret`, or
`return 0/false/0.0`) on both platforms: `AreAssertsEnabled AreMandatoryAssertsEnabled
AreOptionalAssertEnabled CheatDockingTraffic CheatLiveStreamViewChannels EnableAllCheats
GetResourceBoxSize GetVRControllerAutoHide GetVRViveTouchpadLockTime GetVRWindowMode
IsAppStoreVersion IsDemoVersion IsGOGVersion IsGestureSteeringActive
IsLanguageSettingEnabled IsMissionBoard IsMouseVRPointerAllowed IsNetworkEngineEnabled
IsTradeShowVersion IsVRMode IsVROculusTouchActive IsVRPointerActive IsVRVersion
IsVRViveControllerActive MakePlayerOwnerOf MovePlayerToSectorPos RequestLanguageChange
RevealEncyclopedia RevealMap RevealStations SetMouseVRPointerAllowed
SetMouseVRSensitivityPitch SetMouseVRSensitivityYaw SetSavesCompressedOption
SetVRControllerAutoHide SetVRVivePointerHand SetVRViveTouchpadLockTime SetVRWindowMode
ToggleScreenDisplayOption`. Some of these legitimately return a constant (`IsDemoVersion`);
the ones that matter here are **`MakePlayerOwnerOf` and `MovePlayerToSectorPos`: empty
`ret` on both platforms, so they do nothing.** The binder refuses any export whose code
starts with `ret` (status `stub`).

## Motion-core functions

Structs (from the game's cdefs; layouts asserted in `src/ffi.rs` tests):
`typedef uint64_t UniverseID;`
`typedef struct { float x, y, z, yaw, pitch, roll; } UIPosRot;` (24 bytes)
`typedef struct { const char* factionID; const char* factionName; const char* factionIcon; } FactionDetails;`
`typedef struct { const char* name; float hull; float shield; int speed; bool hasShield; } ComponentDetails;`
`typedef struct { int major; int minor; } GameVersion;`

L = Linux export, W = Windows export, Bound = bound by `x4mp-core` (capability).

| Need | C signature | L | W | Decls | Bound | Confidence | Notes |
|---|---|---|---|---|---|---|---|
| Pose read (sector frame) | `UIPosRot GetObjectPositionInSector(UniverseID objectid)` | ✓ 0xbefa30 | ✓ 0x1a2600 | 7 (game, mod) | pose_read | **high** signature; **medium** angle unit | Position in metres in the object's sector. Angles: radians per the mod's native use (it treats them as radians and wraps by 2π). Verify natively. |
| Pose write (sector frame) | `void SetObjectSectorPos(UniverseID objectid, UniverseID sectorid, UIPosRot offset)` | ✓ 0xc0e1c0 | ✓ 0x1b8be0 | 5 (game, mod) | pose_write (writes off) | **high** signature; **medium** angle unit | Angles in **degrees** (static analysis: deg -> rad at entry; mod converts rad -> deg before calling). Re-parents into a zone/temp zone; authoritative for the next pathing pass (`motion.md` §1). Slots 4..16 only. |
| Pose relative to a space | `UIPosRot GetPositionalOffset(UniverseID positionalid, UniverseID spaceid)` | ✓ 0xbeffd0 | ✓ 0x1a5000 | 3 (game, mod) | offset | high | |
| Pose write relative to parent | `void SetPositionalOffset(UniverseID positionalid, UIPosRot offset)` | ✓ | ✓ | 2 (game) | no | high | Parent-relative only; not needed yet. |
| Velocity read | — | — | — | — | — | — | **No export.** Core derives velocity from consecutive poses over TLS game time. Engine paths for later: local velocity vfunc650, relative velocity 0x3dc690 (Windows RVAs, `entity.md`). |
| Speed (scalar) | `ComponentDetails GetComponentDetails(const UniverseID componentid, const char*const connectionname)` | ✓ 0xa49cc0 | ✓ 0xafc250 | 4 (game) | details | high signature; medium semantics | `speed` is an `int` (m/s as the target monitor shows it). Game passes `""` for the connection. Used as a cross-check of derived speed. |
| Velocity write | — | — | — | — | — | — | **No export.** MD `set_object_velocity` (MD executor) or NavigationController `SetVelocity` (hook, `entity.md`). |
| Angular velocity | — | — | — | — | — | — | No export; derived. |
| Sector of an object | `UniverseID GetContextByClass(UniverseID componentid, const char* classname, bool includeself)` | ✓ 0xbe2530 | ✓ 0x18aab0 | 41 | pose_read, pose_write | high | `classname = "sector"`. Also `GetContextByRealClass` (✓/✓, 4 decls, not bound). |
| ID -> name | `const char* GetComponentName(UniverseID componentid)` | ✓ 0xbe1c10 | ✓ 0x189d60 | 7 | identity | high | String owned by the engine; copied at once (≤ 256 bytes). |
| ID -> id code | `const char* GetObjectIDCode(UniverseID objectid)` | ✓ 0xbfb1f0 | ✓ 0x1a2430 | 32 | identity | high | e.g. `ABC-123`. |
| String -> ID | `uint64_t ConvertStringTo64Bit(const char* idstring)` | ✓ 0xbcf880 | ✓ 0x178260 | 14 | identity | high | Converts Lua/MD id strings (`tostring(component)`). |
| Class | `const char* GetComponentClass(UniverseID componentid)` | ✓ 0xbe16d0 | ✓ 0x1899a0 | 12 | identity | high | |
| Class test | `bool IsComponentClass(UniverseID componentid, const char* classname)` | ✓ 0xc079f0 | ✓ 0x1b2230 | 31 | identity | high | `IsRealComponentClass` also ✓/✓ (12 decls, not bound). |
| Validity | `bool IsValidComponent(const UniverseID componentid)` | ✓ 0xa43530 | ✓ 0xb002e0 | 2 | identity | high | |
| Alive | `bool IsComponentOperational(UniverseID componentid)` | ✓ 0xc02cb0 | ✓ 0x1b2320 | 23 | identity | high | |
| Wreck | `bool IsComponentWrecked(const UniverseID componentid)` | ✓ 0xa4c290 | ✓ 0xaff7e0 | 10 | identity | high | |
| ID -> macro | — | — | — | — | — | — | **No export** returns a component's macro (only slot/upgrade/weapon macro getters). Use MD (`$obj.macro`) via the executor, or Lua `GetComponentData(id,"macro")` (a Lua C function, not an export). `GetMacroClass(const char* macroname)` ✓/✓ (18 decls) maps macro -> class. |
| ID -> owner | `FactionDetails GetOwnerDetails(UniverseID componentid)` | ✓ 0xa4af50 | ✓ 0xafdfd0 | 10 | owner | high | `GetOwnerDetails2` also ✓/✓ (1 decl, layout not extracted). |
| Player faction | `const char* GetPlayerFactionName(bool userawname)` | ✓ 0xbde960 | ✓ 0x1a4390 | 4 | owner | high | |
| Owner write | `void SetComponentOwner(UniverseID componentid, const char* factionid)` | ✓ 0xc0c8d0 | ✓ 0x1b7720 | 2 (game) | no | high signature | Real body on both. Not bound (no writes in this step). |
| Make player-owned | `void MakePlayerOwnerOf(UniverseID objectid)` | ✓ stub | ✓ stub | 4 | **never** | high | **Empty `ret` on both platforms**: does nothing. |
| Player ship (aboard) | `UniverseID GetPlayerOccupiedShipID(void)` | ✓ 0xa44e40 | ✓ 0xb05e30 | 34 | player_ship | high | Ship the player is in, piloting or not. Used by the pose probe. |
| Player ship (piloting) | `UniverseID GetPlayerControlledShipID(void)` | ✓ 0xc2d060 | ✓ 0x1e9bd0 | 7 | player_ship | high | |
| Last piloted ship | `UniverseID GetLastPlayerControlledShipID(void)` | ✓ | ✓ | 10 | no | high | |
| Player entity / object / container / zone | `UniverseID GetPlayerID(void)`, `GetPlayerObjectID(void)`, `GetPlayerContainerID(void)`, `GetPlayerZoneID(void)` | ✓ | ✓ | 34 / 8 / 11 / 10 | player | high | `GetPlayerShipID(void)` ✓/✓ (4 decls) not bound. |
| Player valid | `bool IsPlayerValid(void)` | ✓ 0xc03260 | ✓ 0x1b3a50 | 6 (game, mod) | player | high | |
| Pause | `bool IsGamePaused(void)` | ✓ 0xc1ea20 | ✓ 0x1b2d70 | 11 | clock | high | Reads TimerBase pause counters. |
| Game time | `double GetCurrentGameTime(void)` | ✓ 0xa41960 | ✓ 0xafcdc0 | 25 | clock | high | Calling thread's TLS game time (the time base of pose reads). |
| Version | `GameVersion GetGameVersion(void)` | ✓ 0xa41a60 | ✓ 0x354a30 | 3 | version | high | Windows body is `mov eax,9; ret` (9.0). |
| Spawn / remove (later) | `UniverseID SpawnObjectAtPos2(const char* macroname, UniverseID sectorid, UIPosRot offset, const char* ownerid)`, `void RemoveComponent(UniverseID componentid)` | ✓ | ✓ | 4 / 6 | no | high | Mutate the universe; MoveWorker only. Candidates for replicas. |

All 25 bound functions exist on **both** platforms at the pinned addresses with matching
code heads (verified offline against both files).

## Uncertain points to settle natively

1. **Angle units** of `UIPosRot`: radians from `GetObjectPositionInSector`, degrees into
   `SetObjectSectorPos` (asymmetric). Evidence: the mod's working Lua and static analysis
   of the setter; the getter's conversion was not traced. `Engine::set_pose_in_sector`
   converts rad -> deg accordingly.
2. **`ComponentDetails.speed`** meaning (scalar m/s, integer, possibly target-monitor
   specific). The pose probe logs it next to the derived speed (`export_speed=`).
3. **Linux thread safety** of the exports from VBI slot 13. The analysis was done on
   Windows; the Linux binary has the same structure (TLS `CurTime` read confirmed), but a
   native run is the evidence.
4. **Windows calling convention edge**: `ComponentDetails`/`FactionDetails`/`UIPosRot`
   are > 8 bytes, so on Windows x64 they are returned through a hidden pointer and
   `UIPosRot` is passed by reference to a copy. Rust's `extern "C"` follows that ABI; the
   fake-engine tests ran natively on Windows (msvc) with those struct returns, but not
   against X4.exe.
5. `GetOwnerDetails2` layout and `GetComponentDetails` with a non-empty connection name
   were not examined.
