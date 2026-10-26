# Engine rewrite: combat (weapons, turrets, projectiles, hits, damage, targeting)

Owner: combat agent, branch `contrib/ferrite-rw-combat` (base `contrib/ferrite-rewrite-base` e63d9cf).
Program and method: `reports/REWRITE-PROGRAM.md`. Build: X4 9.00, Steam build 23660954 (Windows RVAs;
VA = 0x140000000 + RVA). Labels: `x4-re/labels/rw-combat.tsv` (merged). This report describes behaviour;
it contains no game code. Every result below says what kind of evidence it is:
**native** (a lane run of X4), **oracle** (the original machine code of the pinned `X4.exe`, mapped and
called offline in a test process, no game running), **static** (reading the binary), **synthetic**.

## 1. Status

| step | state | evidence |
|---|---|---|
| combat load scenario (`dev/combat_scenario/`) | done; full native pass (smoke-003) | native (functional) |
| understand: weapon/turret update, aiming, firing, bullets, hits, damage | done (sections 3-7) | static |
| battle profile | complete run battle-002 (120 s window, 11 simprof windows, 110,233 samples) | native, not timing-trustworthy |
| Rust: turret joint step `ikjoint::rotate_toward` | done, bit-identical | oracle: 1.7 M steps incl. NaN inputs |
| capture/replay format and replay tool | done (binary + text form) | oracle + synthetic |
| in-game recorder + shadow (`x4mp_combat.dll`, code patch 0xe1f840) | done: battle-002, 985,687 calls shadowed, 0 mismatches; 300,000-call capture replayed offline exact | native + oracle |
| switch (Rust authoritative, verified) | done: battle-switch-001, 590,910 calls served by Rust, every call verified against the original, 0 mismatches | native + oracle |
| joint step as an x4mp-sim system (`src/sim.rs`) | done: same state hash on 1/2/4/16 threads, equal to the serial step | synthetic |
| combat census (data-only vtable hooks, section 9a) | built and offline-tested; native run pending | oracle (mapped image) + synthetic |
| scaled battles `battle-200/400/800` (scenario v3) | built, md.xsd-valid; native scaling series pending | offline |

## 2. Cost picture

Idle evidence (lead's 8-sector attention harness, no fighting): `U::Weapon::Move` 0x8132d0 4.6 % inclusive of
MoveWorker samples. Inside it: `U::Weapon::StopAiming` 0x81c7a0 1.2 % (all self), `U::Destructible::IsFunctional`
0x51e8b0 1.0 %, `U::Weapon::UpdateShootDecision` 0x816d20 0.7 %, Weapon Move self 1.0 %. The idle path does
almost no arithmetic: StopAiming only tests the turret's at-rest byte through two dependent pointer loads
(Weapon+0x208 -> +0x48 -> +8) and the aiming flag; its self time is cache misses on cold weapon objects,
once per weapon per frame. A data-oriented rewrite removes that cost by keeping the per-weapon state the
idle test needs (at rest, aiming, functional, has target) in dense arrays per sector.

Battle profile (native lane b1, run battle-002: 44 Argon vs 43 Xenon ships, complete 120.3 s
measurement window, Xenon 43 -> 12 ships; 11 complete 10-s simprof windows, 110,233 samples; other X4
instances on the PC, so shares are functional, not timing-trustworthy; the cut-short battle-001 gave the same
ranking):

| function | inclusive share of MoveWorker samples |
|---|---|
| `U::Weapon::Move` 0x8132d0 | 4.88 % (self 0.29 %) |
| - `UpdateShootDecision` 0x816d20 | 1.90 % (launch transform 0x81cb80 0.79 %, aim point 0.44 %, self 0.21 %, angle tolerance 0x8169b0 0.13 %, LOS ray cast 0.06 %) |
| - `Fire` 0x817d20 -> `FireShot` (vfunc 998 0x8185b0) | 0.77 % (SpawnEffect 0.38 %, zone bullet creation 0x83b500 0.14 %, bullet setup 0.09 %, heat 0.06 %, PostEvent 0.04 %) |
| - `IsFunctional` 0x51e8b0 (twice per weapon), all callers | 0.59 % |
| - `AimAt` / `StopAiming` (turret IK) | 0.29 % / 0.32 % (StopAiming all self) |
| - `IKValue_RotateToward` 0xe1f840 (reimplemented) | 0.05 % |
| launch-point transform `Weapon::vfunc985` 0x81c960, all callers / node transform 0x74c2d0 / 0x74be30 | 0.88 % / 1.31 % / 1.39 % |
| aim point `ComputeAimPoint` 0x7e72b0 -> intercept solver 0x7e7460, all callers | 0.65 % (solver self 0.13 %) |
| `Bullet::UpdateFlight` 0xd4f240 | 0.79 % (hit path OnHit/ApplyHit 0.20 %, impact effect 0.13 %, damage 0.08 %) |
| zone ray cast 0xbc4d0, all callers (0x8401f0 0.31 %, bullets, navigation, missiles, fire decision) | 0.96 % |
| SpawnEffect 0xd73e00 / PostEvent 0x99a3f0 / ComponentFactory::Create 0x8e0250, all callers | 0.55 % / 1.40 % / 0.21 % |
| for scale: AIDirector::Move / Jolt Scene::Step / Sector::Move walk / ProcessDueEvents / BuildCache | 10.3 / 13.8 / 14.2 / 6.7 / 8.2 % |

So in an 87-ship battle the weapon and bullet engine code is about 6-7 % of the MoveWorker; the joint step
is negligible, the hottest pure part is the launch-point transform (generic node-transform code, transforms
agent, Q12), then the intercept solver and the fire-decision geometry. A run in a quiet window gives the
timing-trustworthy ranking.

## 3. Weapon and turret update (`U::Weapon::Move`, vtable slot 674)

One function serves `U::Weapon`, `U::Turret`, `U::MissileLauncher` and `U::MissileTurret` (slot 674 at
0x2b69788, 0x2b6fe20, 0x2b03e98, 0x2b01b30). It runs for every weapon of a ship whose subtree the sector walk
visits (Positional::Move recursion), i.e. at high attention. Order of work each frame:

1. **Functional and fire decision.** `IsFunctional` (0x51e8b0: mode +0x68 is 0, not disabled, hull at least
   the macro's function threshold of max hull). With a bullet macro (+0x2e0) it runs
   `UpdateShootDecision` (0x816d20, below), which returns "fire" and an "aimable" flag.
2. **Fire loop.** If the weapon is firing (+0x30d) the next shot time comes from `GetNextShotTime`
   (0x81dfd0), otherwise it is now. While that time is not in the future and the weapon may fire
   (vfunc 988 0x816a90), `Fire` (0x817d20) runs for that time and the next shot time is recomputed: a slow
   frame fires several catch-up salvos at their exact due times. Start of firing spawns the muzzle effect
   (SpawnEffect 0xd73e00) and sets +0x30d; cosmetic camera shake for the player only.
3. **Heat/cooldown.** Beam and charge weapons (bullet defaults +0x520 > -1) track their burst time in the shoot
   controller (+0x88) and stop or spawn an overheat effect when it runs out.
4. **Target distance.** The controller's target (controller vfunc 18 +0x90) gives the squared distance
   (`GetDistanceSqTo` 0x3e1930), used for range tests and passed to the controller.
5. **Aim.** A turret with an IK system (+0x208, its state at +0x48) and a non-zero rotation speed asks the
   controller for the aim point (vfunc 21 +0xa8, `ComputeAimPoint` 0x7e72b0 -> intercept solver 0x7e7460),
   expresses it in the launch point frame (`GetLaunchTransform` 0x81cb80), derives the angular velocity of the
   line of sight for feed-forward, and calls `AimAt` (0x81c580) -> IK system (0x7569a0 -> 0xe221a0 -> 0xe21110)
   -> per joint `IKValue_RotateToward` (0xe1f840). Without a target it calls `StopAiming` (0x81c7a0), which
   returns the turret to rest (0x756a80, same joint step toward 0) and posts StoppedAimingEvent once.
6. **Controller upkeep.** A target that no longer resolves in the ID map is cleared (controller vfunc +0xd0);
   for weapons with a line-of-sight beam a ray cast (0x840d80) shortens the beam (controller +0x50).
7. `Destructible::vfunc674` (base Move: children, physics pose).

**Fire decision (0x816d20).** Needs a shoot controller (Weapon+0x318); same zone/sector rules for the target;
`IsFunctional`; controller checks. It computes the launch transform in the zone frame, the aim point
(controller vfunc 21), the angle between the launch axis and the aim direction (acos of the normalised z
component; lengths use `rsqrtss`+`rcpss`, i.e. hardware approximations, so these results are CPU-specific),
compares it with the allowed angle (weapon defaults +0xa04/+0xa00 or controller +0x80), checks the range
(weapon range x global factors, settings block 0x6cf1538 +0x1a0/+0x1a4) and, at most once per second per
controller (+0x40), casts a line-of-sight ray through the zone's spatial query (0xbc4d0) and caches the
result (+0x84 blocked by friend, +0x85 blocked). A fire decision stamps the controller (+0x28 = now).

**Firing (0x817d20).** Salvo size = barrels (vfunc +0x1f20) x weapon defaults +0x8f8; each shot is the
virtual +0x1f30 = `U::Weapon::vfunc998` 0x8185b0 (`FireShot`): shot time = shot index x inter-shot delay
(bullet properties +0x60) + the salvo time + a **thread-RNG** jitter (Random::FloatState TLS+0x318, up to
+0x68); the zone creates the bullet (zone vfunc +0x1d10 -> `Zone::vfunc930` 0x83b500 -> universe component
creation: new UniverseID); start pose = launch-point transform at the shot time in the zone's space;
bullet setup 0xd50f30; WeaponFiredEvent (immediate); muzzle effect (SpawnEffect, bullet defaults +0x4e0);
heat 0x815f50; a timed kill at the range limit (bullet event 0xe through vfunc +0x1548, from range
0xd50ce0 and the target distance); and, when bullet defaults +0x364 is set, three more thread-RNG draws
for a random spin (bullet vfunc +0x1b98). Then
last shot time +0x310, magazine counter +0x350 (reload: StartAmmunitionReloadEvent, immediate), cancels
pending events 0x1ba/0x14f, heat (vfunc +0x2010) and OutOfAmmoEvent (deferred) when empty.
`GetNextShotTime` = last shot + max(reload time, shot interval), shot interval (0x7c7470) = 1 / (rate x
modifiers).

**Weapon fields used** (offsets from the component): +0x60 macro; +0x68 mode; +0x208 turret IK system;
+0x2e0 bullet macro; +0x2e8/+0x2f0 launch point indices; +0x300 cooldown block (+0x48 blocked-until time);
+0x30c limited rotation; +0x30d firing; +0x30e beam active; +0x310 last shot time; +0x318 shoot controller;
+0x350 magazine counter; +0x354 aiming flag. Shoot controller (`U::BasicShootController` 0x2b7fcc0, 35 slots;
Player/LargeTarget/Mindless/Multiple variants): +0x18 target id, +0x28 last decision time, +0x40 LOS check
time, +0x50/+0x60 sampled target offset and its time, +0x80 angle override, +0x84/+0x85 LOS cache, +0x88
burst start, +0x90 burst effect spawned.

## 4. Turret joints: the IK value step (reimplemented)

A turret's IK definition (component template +0x58 -> +0x1c8) is a list of chains; each chain lists joints
(usually yaw, then pitch). The per-joint state is a `U::IKValue` (0x680 bytes, constructed in 0xe6acc0):

| offset | field |
|---|---|
| +0x00 | vbptr (vbtable 0x2c450c0: virtual base at +0x678) |
| +0x08 | angle (float) |
| +0x10 | time of the angle (double) |
| +0x18 | angular velocity (float) |
| +0x1c | target angle of the last step |
| +0x20 | active byte (checked by 0xe209a0) |
| +0x21 | continuous joint (no end stops) |
| +0x24..+0x34 | sub-stepped branch state |
| +0x38 | embedded controller object (sub-stepped branch only) |
| +0x660..+0x670 | sub-step accumulator, interpolation endpoints |
| +0x678 | `XLib::ValueSource<float>` virtual base, vftable `U::IKValue::vftable` 0x2c450d0; slot 3 (thunk 0xe837d0 -> 0x474620) stores angle and time |

`IKValue_RotateToward` (0xe1f840; joint, target, min, max, max speed, acceleration, feed-forward speed, snap)
has two branches selected by the byte 0x3d19b14 (0 after static initialisation, no other writer found;
native confirmation pending). The default kinematic branch, as reimplemented:

1. Continuous joints take the target the short way round: target mod 2*pi into [0, 2*pi), then the
   difference to the current angle mod 2*pi around pi, back to an angle near the current one.
2. The target is stored (+0x1c). Snap flag or |target - angle| < 1e-4: angle := target with the joint's
   *old* time; velocity := feed-forward unless the target lies within 1e-4 of a limit (then 0); returns true.
3. No game time passed since the joint's time: return false (the target was still stored).
4. Braking speed = 0.9 * sqrt(2 * accel * |target - angle|) toward the target; wanted speed = dt *
   feed-forward + braking speed (dt = now - previous frame time, f64 -> f32), clamped to +-max speed.
5. Velocity moves toward the wanted speed by at most accel * dt (or jumps to it within 1e-4).
6. angle += velocity * dt. If the signed step overshoots the target and the velocity has settled within
   accel * dt of the wanted speed: angle := target, velocity := wanted speed, reached. (A negative step never
   satisfies the overshoot test; reproduced as is.)
7. If either limit is not within 1e-4 of zero the angle is clamped to [min, max]. Within 1e-4 of either limit
   (also of a zero limit when the joint has none) the velocity becomes 0 and the step counts as reached.
8. angle and the current game time are stored through the value source; return reached.

The sub-stepped branch integrates at 50 Hz through the embedded controller and is not reimplemented.
Callers per frame: aiming turrets (every joint of every chain), turrets returning to rest until all joints
settle (then the at-rest byte stops further calls), and the chain update from `Positional::Move`.

**Rust:** `rust/x4mp-combat/src/ikjoint.rs` keeps the original's f32 operation order, the f64 -> f32 dt
conversion, x86 `minss`/`maxss`/`comiss` semantics for every comparison (NaN included), and the constants'
bit patterns from .rdata (2*pi 0x40c90fdb, pi 0x40490fdb, 1e-4 0x38d1b717, 0.9 0x3f666666). The game's
`fmodf` (0x1a1a080) computes the exact remainder, which Rust's `%` also computes.

**Oracle results** (`cargo test -p x4mp-combat --release`, tests/oracle_ikjoint.rs): bit-identical value,
velocity, target, time and return for 400,000 realistic steps, 400,000 steps with arbitrary finite bit
patterns (subnormals, huge values, signed zeros), an edge grid of about 19,000 cases (limits, tolerance
boundaries, dt 0, snap, wrap), 400 turrets x 600 frames with the original's output fed back as the next
input (240,000 steps), 200,000 steps with MXCSR flush-to-zero on the same thread, 60,000 steps with NaN
inputs (payloads and signs included; non-wrapping joints, since NaN would reach the CRT fmodf error path
offline), and a 20,000-record oracle capture replayed. Batched steps on 1/2/3/8/32 threads give identical bits (5000 joints x 60 frames).

**How the oracle works** (`src/oracle.rs`): it maps the local pinned copy (`x4-re/bin/X4.exe`, size, SHA-256
and PE timestamp checked; base relocations applied; no imports bound, no TLS callbacks, no entry point),
builds a 0x680-byte IKValue with the real vbtable/vftable pointers and calls 0xe1f840 directly. The function
reads its clocks from executable TLS slot 0 (`gs:[0x58]`[0] +0x300 now, +0x328 previous, +0x10 init flag), so
the call temporarily points the thread's ThreadLocalStoragePointer at a private block. Only self-contained
leaf code can be called this way (an import, the CRT error path or the game allocator would fault).

**MXCSR.** X4 sets flush-to-zero on its main thread (0x14dd0e0 from GameClient::Init); worker threads start
with 0x1f80. Natively the MoveWorker runs with 0x1fbf (default control bits, only sticky exception flags set;
no FTZ/DAZ), so x4mp-sim worker threads, which start with 0x1f80, compute the same bits; FTZ would change
results with subnormal intermediates.

**As an x4mp-sim system** (`src/sim.rs`): components `combat.joint` (the joint fields), `combat.joint_input`
(this frame's command, written by the engine-side aim logic) and `combat.joint_output` (return value and
outcome); `TurretJointSystem` (`combat.turret_joint`) steps every active joint of a sector with
`ikjoint::step_with_dt(now = FrameInfo::game_time, dt = FrameInfo::dt as f32)`, the same operations as the
original. It sends no messages. Tests: 12 sectors x 40 entities, 60 frames with changing commands give the same
`state_hash` on 1, 2, 4 and 16 threads, and every joint equals the plain serial step bitwise.

## 5. Targeting and the intercept solver

`ComputeAimPoint` (controller vfunc 21, 0x7e72b0) takes the target from vfunc 17 (+0x88). With lead
enabled it periodically re-samples an offset point on the target (0x3dee20, interval from vfunc +0xa0 and
+0x48) and calls `ShootController_SolveIntercept` (0x7e7460): starting from the target position, up to six
iterations of direction to the predicted point -> bullet flight time for that direction and the shooter's
motion (weapon vfunc +0x1f28) -> target transform at now + t (0x3e2120, extrapolated by its movement
controller); stops early when the target has no linear velocity; finally subtracts shooter velocity x t. It
reads world state through view caches (TLS+0x314 selects the cache), takes references, and is pure apart from
those reads: a later Rust piece once the per-sector snapshot (ecs-core) provides target poses and
velocities. Target selection itself (fight scripts, `SetTurretTargets`, controller SetTarget 0x7e5c40) is
AI/MD-driven and stays with the AI agents.

## 6. Projectiles

`U::Bullet::Move` (0xd4f090, slot 674 at 0x2c43fd8). Bullets attached to a non-zone parent with a
damage-over-time duration (bullet defaults +0x328) apply hits every 0.1 s (0xd528f0). All others run
`UpdateFlight` (0xd4f240):

- Runs when now >= next check (+0x270). Next check = now (every frame) while the bullet is visible or within
  5 km (squared 2.5e7) of the player, else now + 0.1 + Random(0.1 s) from the **thread RNG** (TLS+0x318).
- Segment from the bullet's transform at the last check (+0x268) to now, in the space of the zone
  (zone vfunc +0x1d80), lengths with `rsqrtss`/`rcpss` approximations, plus a margin (settings +0x6224).
- Ray cast through the zone's spatial query (zone vfunc +0x1d78): nearest hit (0xbc4d0) or all hits (0xbc750)
  for piercing bullets (defaults +0x354 hits). For each hit: shield adjustment (if the hit object's shield
  is up, GetShieldPercent 0x51f5b0 above settings +0x622c, the hit point moves to the shield sphere of radius
  defaults x settings +0x6228, earlier along the segment), hit time interpolated between the checks, the hit
  object's pose at that time, then `OnHit` (0xd50500) -> `ApplyHit` (0xd528f0).
- After a hit: ricochet if Random() from the thread RNG < defaults +0x358 (new direction and speed from
  +0x35c, set on the bullet's movement controller +0x200), else removal (0x99aa90, vfunc +0x1548).
- Beams (+0x4a0) tick damage every 0.1 s instead and re-test each frame.

Bullet fields: +0x60 macro; +0x70 parent; +0x200 movement controller; +0x264 travelled length; +0x268 last
check; +0x270 next check; +0x280 hit count; +0x288 last damage tick; +0x2a0 shooter; +0x2e0 embedded
`U::BulletDamageSource`.

**Earlier description corrected.** parallel-sim.md section 1/5 calls 0xd528f0 the hit test run every 0.1 s.
0xd528f0 applies a hit; the geometric test is the ray cast inside 0xd4f240, per frame near the player and
every 0.1-0.2 s far away. Candidate A1 (two-phase bullet hit tests) therefore splits 0xd4f240: phase 1 the
segment + ray cast + shield geometry per due bullet (reads the zone's spatial structure; the RNG draw for
the next check must move to phase 2 or to per-bullet streams), phase 2 the existing apply in walk order.

## 7. Hits, damage, shields

`ApplyHit` (0xd528f0): attacked events (`PostAttackedEvents` 0x51ff10: immediate AttackedEvent to the victim
and its ancestors, attacker events, player copies), statistics and achievement checks, kill credit, an
impulse on the hit body, damage (`ApplyDamage` 0x51f7f0), the impact effect (0xd519e0, effect system) and the
bullet's removal when its hit count reaches the limit.

`ApplyDamage` (0x51f7f0): finds the component that takes the damage (0x51cc00), scales the source's damage
terms (source vfuncs +0x28/+0x30/+0x38/+0x40 at the hit time) by the victim's modifier (vfunc +0x1d08); shields
absorb first (shield group at +0x290, 0x791760), the remainder plus hull-only damage goes to the hull
(0x51d570), and a damage-reaction counter (+0xc38/+0xc40, settings +0x2b0/+0x2b8) fires a reaction when it
runs out. Shield level is time-dependent (`GetShieldPercent` 0x51f5b0 evaluates recharge lazily).

## 8. What is shared across entities (parallel hazards)

| stage | writes outside the entity | consequence for per-sector jobs |
|---|---|---|
| joint step 0xe1f840 | none (joint only) | parallel now (done) |
| aim point / intercept | reads target poses via view cache | parallel with a snapshot |
| StartedAiming/StoppedAiming events | immediate PostEvent | buffer, apply at barrier |
| fire decision LOS ray cast | reads spatial query | parallel read, if the query is thread-safe (open) |
| Fire: bullet creation | UniverseID allocation, component factory, effects | serial apply phase |
| bullet next-check jitter, ricochet | thread RNG (TLS+0x318) | per-bullet streams or serial order |
| bullet ray cast | reads zone spatial structure | phase 1 candidate |
| hit apply, damage, kills | immediate events into MD/AI, kill (deferred), statistics | serial apply phase |
| cross-sector hits | bullets live in their zone; a hit object is in the bullet's zone | through the barrier messages of ecs-core (rewrite-coord/interfaces.md) |

## 9. In-game recorder and shadow (`x4mp_combat.dll`)

Built in `rust/x4mp-combat/src/game/` (cdylib `x4mp_combat.dll`, Lua module `luaopen_x4mp_combat`), loaded in
lane runs only by `dev/combat_scenario/combat_capture.lua` through the overlay from
`dev/combat_scenario/capture_overlay.py`. Registered in rewrite-coord/hooks.tsv.

- Site 0xe1f840; refuses unless the process image matches the pins (PE timestamp, SizeOfImage, file size,
  file SHA-256), the site holds the 16 pinned bytes and its first 32 bytes hash to
  `205cd3d06b0fa29ba103a4cdfe381705a7143cb22499d950a90b3e7861320e11`, it runs on the main Lua thread.
- Prologue: `mov rax,rsp`, `mov [rax+10h],rbx`, `mov [rax+18h],rsi`, `push rbp`, `push rdi`, `push r12` =
  15 bytes, position-independent. Patch: `jmp [rip+0]` + detour address + `int3`, written together with the
  unchanged 16th byte in one `lock cmpxchg16b` (the site is 16-byte aligned); trampoline = the 15 original
  bytes + absolute jump to 0xe1f84f. Removal swaps the original 16 bytes back with the same compare-exchange
  and fails (leaving everything allocated) if anything else changed them. The module pins itself.
- The detour always calls the original. On the arming thread it copies the joint (+0x08/+0x10/+0x18/+0x1c/
  +0x21) before and after, reads both clocks from executable TLS slot 0, appends a record to a buffer
  reserved at start (full: counted as dropped) and, in shadow mode with the mode byte 0, runs
  `rotate_toward` on a copy and compares the five outputs bitwise. Calls on other threads pass through and
  are counted. Panics in the bookkeeping are caught and switch it off.
- Lane flow: the scenario's MD raises `X4MP.CombatScn` `begin`/`end`; the script arms at `begin`, logs
  `COMBATCAP stats ...` every 10 s, and at `end` logs the final counts (and the first mismatch), writes
  `<profile>/x4mp-combatcap.txt` (text capture, collected by x4lane into `profile-files/`), and restores
  the code. `x4mp-combat-replay <file>` replays it.
- Offline evidence: `tests/session_mapped.rs` arms the real patch on the mapped pinned X4.exe, calls the
  original 5,000 times through it (5,000 recorded, 5,000 shadowed, 0 mismatches, records equal to direct
  oracle results), passes a foreign-thread call through, writes/reads/replays the text capture, restores
  the bytes (head hash checked again), refuses a second session and a modified site, counts drops when the
  buffer is full. `native/engine/combat_check_lua.py` loads the DLL in a standalone LuaJIT: module table,
  argument checks, coroutine refusal, refusal outside the X4 process. Synthetic patch tests use a fake
  function with the same prologue.
- Native (lane b1, battle-002, inside the 120-s window): 985,687 calls, all on the MoveWorker (foreign 0),
  985,687 shadowed, **0 mismatches**; mode byte 0; MoveWorker MXCSR 0x1fbf (default control bits, sticky
  exception flags, no FTZ/DAZ), unchanged during the window; the first 300,000 calls were captured and
  `x4mp-combat-replay` replays them exactly offline. Code restored at window end.

## 9a. Combat census (`x4mp_combat.dll` census mode)

What a parallel walk has to buffer, lock or split for combat, measured per simulation frame in a lane run
(overlay `capture_overlay.py --mode census`, output `<profile>/x4mp-combatcensus.txt`, lines `COMBATCENSUS`).
Data-only hooks only (rewrite-coord/hooks.tsv): the vtable entries of slot 674 (Move) of the four weapon
classes (all 0x8132d0) and of `U::Bullet` (0xd4f090) get pass-through wrappers, slot 998 (FireShot: 0x8185b0
for Weapon/Turret, 0x609b50 MissileLauncher, 0x60aaf0 MissileTurret) gets signature-free counting thunks
(`lock inc` + jump to the original). Each entry is verified against the pinned original, swapped with one
compare-exchange and swapped back at the end; the originals always run with their own arguments.

Per frame (the frame's game time TLS+0x300 delimits frames): weapon calls by class and nesting, controller
target set, aiming and firing flags, salvos (last shot time +0x310 changed), shots and missile shots, bullet
calls, flight checks (last check +0x268 became now), hits (+0x280 grew) and ricochet resets, first moves of
new bullets (last check still equals the creation time +0x278) and how many of those were fired in the same
frame, thread-RNG steps inside outermost weapon and bullet updates and on the whole thread between frames
(steps between two TLS+0x318 states with the engine generator `rotr64(s * 6364136223846793005 +
1442695040888963407, 30)`), rdtsc time in weapon and bullet updates. Static findings it rests on: FireShot
returns the shot time in xmm0 (index x inter-shot delay + salvo time + a thread-RNG jitter) and `Fire` uses
it, so a deferred FireShot would have to return that time at once; the bullet setup 0xd50f30 draws the thread
RNG twice more (spread) and stores the shot time as last check, next check and creation time; bullets are
removed through posted events, never inside their own Move.

Offline evidence: a synthetic engine behind read-only fake vtable entries (frames, nested calls, RNG steps,
thunk argument and result pass-through, a foreign thread, restore); on the mapped pinned X4.exe the nine
entries hold the pinned originals and are swapped and restored; the RNG step equals the original
`Random::U64State` 0x14ddb30 on 10,000 states.

## 10. Shadow and switch design for the joint step

- **Shadow:** built into the recorder (section 9): `rotate_toward` on a copy of the pre-state with the same
  arguments and clocks after the original returned, five outputs compared bitwise, mismatches counted and
  the first 16 kept (`COMBATCAP final ... mismatched=..`, `COMBATCAP first_mismatch ...`). The original
  stays authoritative.
- **Switch (built):** `mode='switch'` in the lane overlay. The detour serves the call with `ikjoint::step` and
  applies the result with the original's writes for that outcome (target; velocity; then SetValue through
  the joint's value-source vftable slot 3 with the angle and the time, i.e. now or the joint's own time
  after a snap; only the target when no game time passed). It passes through (original) for a foreign
  value-source class and switches off for good when the mode byte is not 0. `verify=N` runs the original on
  a scratch copy of the IKValue (header + vbase vptr) every N-th call first; any difference switches the
  reimplementation off for good (`FEATURE ikjoint DISABLED reason=verification mismatch`) and the original
  serves that and every later call. Offline on the mapped image: 4,000 calls with verify 1/7/0 equal the
  original alone bitwise; an injected one-bit fault is caught on the first verified call and every call
  still gets the original's result. **Native** (lane b4, battle-switch-001, verify 1, 120.3-s window of
  the battle, Xenon 43 -> 24 ships): 590,910 joint steps served by the Rust step, all 590,910 verified
  against the original on a scratch copy, 0 mismatches, no foreign value-source class, no switch-off; code
  restored at window end and the scenario finished normally.
- **Per sector:** the walk agent's per-sector jobs (rewrite-coord/interfaces.md) call the same function on
  each sector's turrets; joint steps are entity-local, aim events go into the sector's event buffer.

## 11. Combat load scenario (`dev/combat_scenario/`)

Test-only extension `x4mp_combat_scenario` (MD only, `save="false"`, version 3). In a fresh `custom_creative` world it
waits for `$WarmupS`, moves the player ship to a cleared point 10 km above the start sector's plane
(`get_safe_pos`, radius 8 km; lane worlds: `cluster_14_sector001_macro`), makes the player ship
invulnerable and neutral to both fleet factions, spawns the fleets 5 km in front of it in the player's zone
with fixed ship seeds, orders them to attack each other (`Attack`, primary target round robin, the whole
enemy list as secondary targets, no other targets) and logs `COMBATSCN` markers. Variants: `battle`
(40 Argon fighters, 2 frigates, 2 destroyers vs 40 Xenon fighters, 2 P, 1 K), `idle` (same layout, both
Argon, Wait orders: every turret idle), `capitals` (4+4 vs 3+4 capital ships), `smoke` (5+1 vs 5+1), and
the scaled battles `battle-200` (100 vs 100), `battle-400` (200 vs 200), `battle-800` (400 vs 400) with the
battle's class mix (shares within 3 %), spawned 25 ships per second (half per side, `$SpawnBatch`) by an
instantiated `SpawnBatch` cue; `COMBATSCN spawn begin` / `spawn complete` frame the spawning and the
measurement window starts `$EngageDelayS + $SettleS` after it. `$SpawnBatch` 0 (the other variants) spawns
in one frame in the version 2 order, seeds and positions. `dev/combat_scenario/scaling.py <runs>` turns
profile-mode runs into a scaling table (fps, MoveWorker frame and simulation ms, shares, ms per frame per
system, cost per ship, X4 memory).

```
python dev/combat_scenario/install.py stage --variant battle --out .local/evidence-063-withheld
x4lane run --agent combat --overlay <wt>/.local/evidence-069-withheld ^
  --extension <wt>/.local/evidence-064-withheld --until "COMBATSCN cleaned" --timeout 900 ^
  --fail-on "COMBATSCN refused|Error in MD cue md.X4MP" --collect <wt>/.local/evidence-076-withheld
python dev/combat_scenario/markers.py summary <run>/x4lan-debug.txt
python dev/combat_scenario/markers.py profile <run>/x4lan-debug.txt --folded battle.folded
```

**MD behaviour learned natively** (lane b1, run smoke-002): sub-cues are enabled as soon as their parent cue
triggers, before the parent's `<delay>` and actions, so nesting and delays cannot order steps (the first
version engaged before it spawned); `checkinterval` must be a literal time; a cue with non-event conditions
needs `checkinterval` or `checktime`. The scenario is now a stage machine of sibling cues polling `$Stage`;
offline tests enforce all three rules.

## 12. Next pieces

1. Joint step natively: done (capture + replay exact, shadow 0 mismatches, switch with verify 1 0
   mismatches). Still open: a quiet-window battle profile for the timing-trustworthy ranking.
2. Combat inside the parallel walk (with the walk agent's deferred-event buffer): the combat-specific
   hazards are bullet creation in `FireShot` (Weapon vtable slot 998: component factory, UniverseID, zone
   add), effect spawns, the thread-RNG draws (shot jitter and spin in `FireShot`, next-check jitter and
   ricochet in `UpdateFlight`) and the immediate events (StartedAiming/StoppedAiming, WeaponFired,
   AttackedEvent and hit events). Plan: a per-sector shot buffer filled in walk order and applied at the
   barrier, sized by a native census of shots, bullet checks and hits per frame.
3. Fire decision 0x816d20 (2 % in the battle, entity-local apart from the 1-s LOS ray cast) as a Rust port
   whose engine queries (launch transform, target, aim point) go through a pinned-callback interface,
   verified by an in-game shadow like the joint step's; the launch transform itself waits for the
   transforms agent (Q12).
4. Intercept solver 0x7e7460 (0.7 %) with the same callback pattern (target pose at a future time).
5. Bullet flight/hit test 0xd4f240 phase 1 (segment, shield sphere, hit time) once the physics agent knows
   whether the XPhys ray cast 0xbc4d0 is safe for concurrent readers (Q12).
6. Damage: `ApplyDamage` + shield absorption 0x791760 + hull 0x51d570 (apply phase, serial).

## Open questions

- Is the sub-stepped joint branch ever active (0x3d19b14)? Only the static initializer writes it (static).
- Thread safety of the zone spatial query under concurrent readers (0xbc4d0, global 0x6cf1670 in 0xd528f0).
- Answered natively: MoveWorker MXCSR 0x1fbf; turret aiming in a battle is AimAt 0.29 % + StopAiming
  0.32 % of the MoveWorker, the joint step 0.05 %.
