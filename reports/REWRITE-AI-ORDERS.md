# Rewrite: native AI order behaviours (ai-orders)

Owner: ai-orders agent, branch `contrib/ferrite-rw-ai-orders` (base `contrib/ferrite-rewrite-base` e63d9cf).
Own files: `rust/x4mp-ai/`, this report. Program and method: `reports/REWRITE-PROGRAM.md`.
Target: X4 9.00, Steam build 23660954, Windows `X4.exe` SHA-256 19750A65...6891. All addresses are RVAs.

**Status (26 Oct 2026):** M1 offline (static ranking, engine model, exact RNG semantics, X4AITRACE format,
`order.fight.patrol` + `move.seekenemies` in Rust, replay tests against **hand-built (synthetic) traces**);
M2a **native**: the first bracket capture of Patrol job ships (section 9a) confirms the model with no
contradiction (24/24 wait wake times bit-exact, constant draw counts per bracket class). Everything else
below is offline unless marked native.

## 1. Ranking of AI orders

### 1.1 Static census (jobs.xml caps)

Method: `x4-re/scripts/rw-ai-orders/jobs_census.py` reads `libraries/jobs.xml` of the base game and the
ego DLC diffs (read-only extraction from the catalogs, `extract_libs.py`), takes each active job's default
order and galaxy quota, and adds subordinate wings (`<subordinates>` x wing quota, recursively). Quotas are
caps, not counts; station-owned traders/miners/defenders (assigned by MD, `lib.request.orders`) and mass
traffic are not in jobs and are missing here.

| default order | script | ships (cap) | share | notes |
|---|---|---|---|---|
| Escort | order.fight.escort (2,802 lines) | 10,016 | 64.6 % | subordinates of patrols, police, carriers; follows a commander in another script's state |
| TradeRoutine | order.trade.routine (877) + trade.find.* | 2,263 | 14.6 % | global trade-offer searches (cross-sector reads) |
| Patrol | order.fight.patrol (536) + move.seekenemies (683) | 1,258 | 8.1 % | zone 65 jobs, sector 40, cluster 9; mostly sector-local |
| MiningRoutine | order.mining.routine (1,855) | 883 | 5.7 % | sector-local mining, sells via trade |
| Police | order.move.recon.police (62) -> order.move.recon (1,926) | 408 | 2.6 % | |
| Middleman | order.trade.middleman | 188 | 1.2 % | |
| FindBuildTasks | order.build.find.task | 164 | 1.1 % | builders |
| Plunder | order.plunder | 154 | 1.0 % | |
| Recon | order.move.recon | 94 | 0.6 % | |

Top-level job ships only (without subordinates): TradeRoutine 42 %, Patrol 23.5 %, MiningRoutine 14.7 %,
Police 7.6 %. Profile context (8-sector run): AIDirector 8.0 %, ExecState_Run 8.4 %, Expression_Evaluate 4.1 %
of MoveWorker samples; the ai-driver agent's profile split of `AIDirector::Move`: ExecState_Run 91 %,
DoIf 24 %, property lookup 25 %, FindTradeOffer 13 %. The **measured** per-script census (ai-driver stage 0,
running natively now) replaces this table when it lands.

### 1.2 Choice of the first order

Patrol: common (third by cap, second among top-level jobs, and the parent of most Escort wings), mostly
sector-local (zones of its own sector, gravidar contacts and distress signals of its own sector), and short.
Escort (largest) reads its commander's state every leg (cross-entity), TradeRoutine searches trade offers
galaxy-wide; both come after the order interface and capture path are proven on Patrol.

## 2. Engine model of an AI order (what we reproduce)

Sources: the game's XML (`x4-re/gamescripts/resolved`), `x4-re/coord/reports/script.md`,
`sector-partition-scripts.md`, `rng.md`, and this agent's disassembly/decompile notes of 0xb58bb0, 0x10a5040,
0x109a490, 0xc58da0, 0xc59be0, 0x14ddc90..0x14ddfe0, 0x14ddd10, 0x1066750, 0x1075280, 0xc581e0, 0xc4d120,
0xc6f7b0, 0xbe0ba0, 0xbe2320, 0xb87a30, 0x106d250 (private outputs in `x4-re/work/rw-ai-orders/out`; no
decompiled code is reproduced here).

1. **Contexts and frames.** One `AIScriptContext` per entity (the pilot); `orders.base` runs the order script
   with `run_order_script`; `run_script` pushes a callee frame. A context runs only when woken: start, `<wait>`
   timer (timed-wake map, wake time = TLS+0x300 + duration in f64), blocking action done, called script
   returned, interrupt. One run (`Scripts::ExecState_Run`) executes actions until a blocking action yields;
   the callee's first run and the caller's continuation after a return are separate runs in later passes of
   `AIDirector::Move` (up to 0x40 passes per frame).
2. **Labels.** `<resume label>` and `abort_called_scripts resume=` jump to a label of the same script
   (`ExecState_JumpToLabel` clears the loop frames). `abort_called_scripts` without `resume` restarts at the
   label passed last.
3. **Interrupts are two-phase** (decompile of 0x1066750, 0x1075280, 0x106d250):
   - *Delivery* (whenever the engine posts the event, possibly inside the component walk) evaluates only
     conditions. Global handlers (`<interrupts>`, listener `AICallStackContext`) are queued (wake flag 0x40).
     The blocking action's own `<interrupt>` (listener `AIScriptContext`, only while the frame is blocked)
     is tested by `AIScript_CheckInterruptHandlers`; a match sets flag 4 and later events are ignored until it
     ran; otherwise the blocking action's own completion test (slot 4) may set flag 2.
   - *Actions* run in the context's turn in `AIDirector::Move`, through the interrupt runner 0xb58ce0, in this
     order: queued global handler actions, then the blocking interrupt's actions, then (on attention change)
     `on_attentionchange`, then the main `ExecState_Run`.
   - Handler order (xsd): current script first, then its caller, and so on; within a script in declaration
     order; the first match consumes the event (`consume` default true).
4. **Attention blocks.** A script holds one program per `<attention min=>`; Patrol, move.seekenemies and
   lib.target.selection have only `min="unknown"`, so attention changes never switch their program.
5. **Not modelled in M1:** `<patch sinceversion>` blocks (they run only when an older savegame is loaded),
   the MD side, savegame import of running scripts.

## 3. RNG semantics (bit-exact, `x4mp_ai::rng`)

One generator: `s' = ror64(s * 0x5851F42D4C957F2D + 0x14057B7EF767814F, 30)`, state at TLS+0x318 of the
executing thread (MoveWorker for AI). Verified against native evidence: the Stage 1 MoveWorker states of
contrib/ferrite-rng-streams are exactly 150 steps apart (unit test `step_matches_native_evidence`).

| script construct | engine routine | draws | value |
|---|---|---|---|
| any action with `chance=` | ExecState_Run 0xb58bb0 | **1, always** (also chance 0 and 100) | runs iff `!(f32(low32) * (100*2^-32 f32) >= chance)`; a failed roll skips to the action's sibling, so a failed `<do_if chance>` acts as a false condition |
| condition child with `chance=` | AIScript_CheckInterruptHandlers 0x1075280 / MultipleCondition 0xc581e0 | 1 when the parent reaches the child (short-circuit), rolled **before** the child is evaluated | same comparison |
| condition `<debug_text debugchance=>` | DebugTextCondition::Check 0xc4ecc0 | 1 **only if** the text is set and its debug filter channel is enabled (global debug log `*(0x6cf1508)`, per-channel table) | `FloatState(100.0) < debugchance` (other rounding than chance rolls) |
| `min= max=` (flat) | EvaluateRandomRange 0xc58da0 | 1 if `max-min > 0`, else 0 | `diff` in the type of max (f32 lengths, f64 times); `r = FloatState(f32(diff))`; result `f64(min) + f64(r)` converted to max's type |
| `profile=`/`scale=` | Random::WithDistribution 0x14ddd10 | `scale` (1 if flat) | sum / scale, folded: 2 increasing, 3 decreasing, 4 bell, 5 inversebell |
| `<wait exact>` / `<wait min max>` | WaitAction 0x10a5040 | 0 / as range | wake = TLS+0x300 + duration (f64); no duration = infinite wait |
| `Random::FloatState(max)` | 0x14ddc90 | 1 if `max >= 0` (0 for negative/NaN) | `(f32(low32) * 2^-32) * max` in f32; can return `max` itself |
| `Random::U64State(n)` | 0x14ddb30 | 1 if `n != 0` | `state % n` |
| `create_position ... max=` | position evaluator 0xc6f7b0 | radius range + 2 (angle in [0, 2pi), z in [-1, 1]) | uses CRT trig routines (bit-exact port needs them; M1 keeps it an engine query) |

Consequence: **every `debug_text chance="$debugchance"` that is reached draws once**, so draw counts depend
on control flow through debug lines (dozens per Patrol leg), and every event delivered to
move.seekenemies's move_to interrupt that the first five conditions do not decide (e.g. every gravidar scan
without an enemy) draws once for the distress check's `chance="80"`. No global handler condition in base AI
has a chance; blocking-interrupt conditions with one exist in move.seekenemies, masstraffic.flee and
masstraffic.police. Condition-level `debug_text` with `debugchance` (e.g. move.seekenemies L383 and the
ego_dlc_pirate diff) draws only when X4's debug channel is on, so the debug filter settings are a world input
(lanes run with debug output on; a player's game usually without).

**Per-entity streams.** In the rewrite each entity's AI scripts draw from their own stream,
`stream_seed(session, uid, 'AISC')`, in the script's draw order. In replay mode the stream is reset to the
captured TLS state at the start of every interpreter bracket (run or interrupt run), so the values are the
original ones; in authoritative mode the stream simply continues (deterministic, thread-count invariant;
the original's thread-global stream seeded from wall clock is not reproducible anyway).

## 4. Equivalence rules: what "identical" means for an AI order

Given the same world inputs, a native order is identical to the script when, per entity:

1. **Same brackets.** The same sequence of interpreter brackets (runs and interrupt runs) with the same
   script frame on top and the same wake reason (start, timer, block, return, restart).
2. **Same draws.** In each bracket the same number of RNG steps in the same order: every chance roll at the
   same XML line with the same chance value and outcome, every random range with the bit-identical value,
   and engine-side draws inside services (captured counts) at the same point. Checked by the RNG state at the
   end of every bracket (the LCG is a bijection, so equal end states mean equal counts).
3. **Same queries.** The same engine queries (find_zone, get_global_path, create_position, get_safe_pos with
   reservation, ...) with bit-identical arguments, in the same order.
4. **Same effects.** The same externally visible actions (move_to, create_order, edit_order_param,
   signal_objects, cancel_space_reservation, set_order_syncpoint_reached, run_script with its parameters,
   set_command, ...) with bit-identical arguments, in the same order and bracket.
5. **Same timing.** The same yields: wait wake times bit-identical (f64), the same blocking actions, the same
   called scripts and return values.
6. **Same interrupt outcomes.** Which handler matched, and whether it aborted frames and restarted at which
   label, or ended / resumed the blocking action.

"World inputs" are: facts read in expressions (classes, parents, order state, job flags, sizes, distances),
query answers, event deliveries and their condition verdicts (for conditions not yet ported), and the
behaviour of not-ported callees and handlers. Floats compare by bits. A native behaviour that reaches a branch
it does not implement stops with `Unsupported` **before any side effect** and the original keeps the context
(fail closed). Mods that patch these scripts (e.g. galaxy_trader patches orders.base and move.generic) change
the effective program: the engine binding must hash the effective program of every ported script at load and
disable the native behaviour on any difference (`FEATURE ai_orders DISABLED reason=script-mismatch`).

## 5. Behaviour specification: order.fight.patrol (version 10)

Implemented in `rust/x4mp-ai/src/orders/patrol.rs` with the XML line on every action. Labels: start (L125),
find_endzone (L315), find_patrolzone (L408), find_position (L458), patrol (L491), finish (L520).
`$debugchance` is 0 for NPC jobs; each `[roll L]` below is one draw.

| state | actions (XML lines) | draws | queries / effects | next |
|---|---|---|---|---|
| init | L89 set_command patrol, L90 set_command_action calculating | 0 | 2 effects | start |
| start | L127 $capship; L129-156 $startzone = job main zone / $space / find_zone(normal) then find_zone(not highway) / this.zone / highway destination | 0 | 0-2 find_zone | |
| | L158 if $space missing: [roll 160 if $space non-null], [roll 165 if job ship without main zone]; startzone missing or highway -> finish; $space = startzone / .sector / .cluster by $range; [roll 199 chance alarm*dc]; L201/L204 edit_order_param space on default and current order if 'Patrol'; or [roll 209] -> finish | 1-3 | 0-2 edits | |
| | L215 organizesubordinates, L221 stayhidden: **Unsupported (M1)** | | | |
| | L279 not in $space -> run_script move.generic; on failure set_order_failed, [roll 291] -> finish | 0-1 | call | |
| | L311 set_order_syncpoint_reached | 0 | 1 effect | find_endzone |
| find_endzone | L317 `wait min=1ms max=1s` | 1 | yield wait | |
| | L320 zone: $endzone = $space; else find_zone(normal, 40 km from ship [, knownto]) then find_zone(normal); none: [roll 346] restart at start in this.zone, or cancel default order -> finish; L379 get_global_path ship->endzone, append endzone, drop non-normal zones [roll 391 each]; [roll 398] | 1+ | 0-3 queries | find_patrolzone |
| find_patrolzone | L410-422 $patrolzone = $space (zone) / next route zone / $endzone / else find_endzone; L426 job expired or timeout -> finish; [roll 430]; L433 other sector -> move.generic (failure: [roll 446] finish if it was $space, else [roll 449] next zone); L455 $inittime | 1+ | 0-1 call | find_position |
| find_position | L461 exit check; L466 zones missing/highway: [roll 468/471/474/477], `wait min=1s max=2s`, restart | | | |
| | L484 create_position(patrolzone, max = size/2); L485 $y = range f32 [-2 km, 2 km]; L486 get_safe_pos(radius ship.size+200 m, ignored ship, value (base.x, y, base.z), zone, no y axis) | 1 + engine | 2 queries | patrol |
| patrol | L493 run_script move.seekenemies (13 params) | 0 | call | |
| | L509 `wait min=1s max=2s`; L511 [roll 30] and player.age > $next_resupply_check: [roll 512], signal_objects ship 'resupply' [false] dc, next check +30 min | 2-3 | 0-1 effect | find_patrolzone |
| finish | L522 cancel default order if running; L527 job expired: [roll 528], create_order MoveDie immediate (byhighway = not L/XL, byhostile) | 0-1 | 0-2 effects | return |

Handlers (declaration order): SectorChangeHandler (native: entered anomaly/gate of the ship; actions
[roll 18], set_known x4 if player-owned, [roll 54] if dc, abort_called_scripts resume=start), AttackHandler,
MissileLockHandler, ScannedHandler, InspectedHandler, FoundAbandonedHandler, FoundLockboxHandler,
ResupplyHandler, TideHandler (opaque in M1), inline (native: target invalid or $space destroyed ->
abort_called_scripts resume=start).

## 6. move.seekenemies (version 7), Patrol path

`rust/x4mp-ai/src/orders/seekenemies.rs`. Ported for escort = null, recon = plunder = false, radius = null
(all Patrol calls). Labels start (L110), moveto (L157), attack (L568), end (L642).

- init: L90 create_position (destination's sector, value $pos, object destination); destination becomes the
  sector; reference object = ship; default target classes [ship_s, ship_m, ship_l, ship_xl, station].
- start: destination missing / not zone-or-sector: [roll 114/118] end; other sector: [roll 124] move.generic
  (failure [roll 137], set_order_failed, end); in a highway: wait for a non-highway zone change.
- moveto: [roll 159]; cancel the previous reservation or create `$reservedspace` (1800 s); L170/L177
  get_safe_pos with space reservation (radius safesize/2); [roll 183, chance global spam * 100]; L187 move_to.
- move_to interrupt (conditions: engine verdict in M1, including the distress roll; actions native):
  [roll 394]; gravidar: filter `$detected` in reverse (other sector or not visible: [roll 398]; ignore list,
  purposes, classes, online), [roll 432], none left: [roll 445] resume moveto, else [roll 448] end the move;
  police/distress signal: enemy = param2/param3, LoseCover signals, [roll 498/505/510/515];
  BoostRelations: **Unsupported (M1)**; attention change: resume start; destroyed/abandoned/owner change: end
  the move.
- after the move: [roll 538 if the move ended within 1 s and > 1 km away]; L547 lib.target.selection (opaque
  in M1); attack: [roll 572], L584 edit_order_param space to the enemy's zone (job ship, zone patrol, enemy
  elsewhere in the sector), [roll 587 chance 0], commander + pilot: **Unsupported (M1)** (comm chatter table),
  [roll 615], L621 create_order Attack immediate (14 params); no target: [roll 639].
- end: cancel the reservation; job expired: create_order MoveDie; return target null, list_targets [].
- handlers: Attack, MissileLock, Scanned, Inspected, FoundAbandoned, FoundLockbox (opaque), TargetInvalid
  (native: [roll 14 chance 0], [roll 15], abort_called_scripts at the current label), inline
  patch_reset_moveto (native). on_abort: cancel the reservation.

## 7. Capture format X4AITRACE v1 and capture points

Text, one record per line, tab-separated: `<tag> <entity> <name> key=value ... [-> key=value ...]`;
values: `n`, `b:`, `i:`, `f:<f32 bits>`, `d:<f64 bits>` (or `F:`/`D:` decimal when hand-written), `c:0x` component,
`o:0x` order, `x:` u64, `p:x,y,z` position, `k:` keyword, `s:` string, `l:[..;..]`. Header
`X4AITRACE 1 build=23660954 source=native|synthetic|handbuilt [records=roll,rnd,q,fx]`; a reader refuses other
builds and versions. Example: `rust/x4mp-ai/tests/data/patrol_zone.x4aitrace` (hand-built, 137 lines).

| record | engine source | content |
|---|---|---|
| `fact` | snapshot reads (mirror columns or a per-run dump) | world facts used by expressions |
| `start` | run_order_script / PushOrderScriptOntoStack | order script and its parameters |
| `run` / `yield` | ExecState_Run call site 0x106e3f6 (ai-driver `ad_observer.run_begin/run_end`, irq 0) | script, wake, game time, TLS+0x318 at entry; yield kind, wake time, callee, return values, TLS+0x318 at exit |
| `irun` / `irend` | interrupt runner call sites (ad_observer irq global / blocking / attention) | handler or blocking interrupt, TLS+0x318 in/out, outcome (abort/resume label) |
| `ev` | HandleScriptEvent slot 565 (ad_observer.event_begin/end, kind 1 global, 0 blocking) | event record (keyword id, object, params), verdict (matched handler / flag 4), TLS+0x318 in/out |
| `q` | slot-3 Execute of query actions (FindComponent, GetGlobalPath, CreatePosition, GetSafePos) | arguments, result, TLS steps inside |
| `fx` | slot-3 Execute of effect actions (MoveTo, CreateOrder, EditOrderParam, SignalObjects, ...) | arguments |
| `roll` / `rnd` | optional fine trace (per-action hook in the interpreter) | line, chance, outcome / range value |
| `abort` | orders.base `abort_called_scripts` | the order script is aborted |

Capture points are agreed with the ai-driver agent (Q1): it owns the call-site and slot-565 hooks and offers
the `ad_observer` callback table (aidriver.h, contrib/ferrite-rw-ai-driver 27beaf0). The capture module
`native/engine/aicapture.h` (ai-orders) chains there: **no hook site of its own, read-only** (hooks.tsv
CHAIN-NOTE). Stage M2a is a *bracket capture* (`capture=bracket`): `run`/`yield`, `irun`/`irend` and `ev`
records with game time, wake mask, TLS+0x318 at both ends, pc, the blocking action (keyword id, XML line),
the timed wake, the abort/resume index, the global-interrupt queue entries a delivery added, and event
parameters (components by UniverseID, numbers raw). Engine layouts used (decompile notes): ExecState
`+0x00` caller, `+0x10` program, `+0x18` pc, `+0x20` blocking action index, `+0x30` yield flag, `+0x38`
context value (tag 0x66, context + 0xa8); context `+0xc0` queue of `{handler index (-1 = blocking), event
record}` (0x50 bytes), `+0x1a0` abort/resume index; event record at handler `+0x18`. Value capture for
queries and facts (M2b) needs slot-3 swaps on the query action classes and is not built yet.

Integration (the lead merges ai-driver + ai-orders): in `native/engine/fixedtick.c` add
`#include "aicapture.h"` after `#include "aidriver.h"`, `static void ac_uninstall(void);` next to the
`ad_uninstall` forward declaration, `ac_uninstall();` before `ad_uninstall();` in `disarm_because`, and
`method(L, "aicapture", ac_lua_start); method(L, "aicapstats", ac_lua_stats);` after the aidriver methods;
in `dev/fixed_tick_probe.lua`, after `FEATURE aidriver ENABLED`, call `owner:aicapture(X4LAN_AICAPTURE,
X4LAN_AICAPTURE_MAX or 8)` when `X4LAN_AICAPTURE` is set, log `owner:aicapstats()` each second as
`[X4LAN] AICAPTURE ...`, and stop with `owner:aicapture('off')` + `[X4LAN] AICAPTURE done` after
`X4LAN_AICAPTURE_SECONDS`. Records go to X4's stdout (`AITR	` prefix); `x4ai-check
<engine-output.log>` (rust/x4mp-ai) extracts and checks them (`x4mp_ai::native`): bit-exact wake times
of every `<wait>` of Patrol and orders.base, exact draw counts per bracket class, the Patrol leg's engine
share (create_position + get_safe_pos draws), and delivery draws per event class.

## 8. Implementation (`rust/x4mp-ai`, no third-party crates)

| module | content |
|---|---|
| `rng` | engine LCG, FloatState/U64State, chance roll, WithDistribution, random ranges, per-entity `Stream`, `stream_seed` |
| `val`, `trace` | values (floats by bits) and the X4AITRACE codec (fail closed on build/version) |
| `world` | `Facts` (typed reads), `Query`/`Answer`, `Effect`, `Io`, `Cx` (roll, range, query, effect helpers) |
| `runtime` | `Agent` (frame stack, waiting states, timed wake), calls/returns, two-phase interrupts, aborts with on_abort |
| `orders::patrol`, `orders::seekenemies` | the two scripts as resumable state machines |
| `replay` | trace replay with bit-exact comparison and first-difference report |
| `system` | per-sector driver (passes, outbox, barrier merge in sector order), synthetic world, thread-invariance bed |

**Order interface** (for the faction AI that will command orders): an order is started with
`Agent::start_order(Frame::Patrol(Patrol::new(PatrolParams{..}, ship)), ..)`; its parameters are the
script's documented order parameters (space, range, pursue, timeout, ...); it reports progress only through
`Effect`s and its yields, and ends with `Yield::Return` or an abort. New orders implement the same frame
contract (`resume`, `matches`, `handler_actions`, `block_interrupt`, `on_abort`, `restart_at`).

## 9. Tests (offline)

`cargo test -p x4mp-ai`: 24 tests pass (Windows 11, Rust 1.94.1); clippy clean.

- unit (13): rng 10 (engine constants by bits, step vs native 150-step evidence, inverse step,
  FloatState/U64 edge cases: no draw on n=0 or negative max, max returned at the top; chance comiss
  semantics incl. NaN; range without width draws nothing; flat range = one float draw; WithDistribution
  fold; stream seeds), val 2 and trace 1 (round trips, refusal of another build/version).
- patrol replay (9, SYNTHETIC hand-built traces, draws computed with an independent transcription of the
  formulas): zone patrol (11 native runs, 2 opaque runs, 5 deliveries, 4 interrupt runs, 12 RNG end-state
  checks: recovery of a missing $space, two legs, nested ResupplyHandler delivery, gravidar interrupt, Attack
  order, opaque AttackHandler, orders.base abort); sector patrol (find_zone with 40 km filter, path with a
  highway removed, TargetInvalidHandler restart at moveto, failed resupply roll back to find_endzone);
  text round trip; the checked-in example file; a changed draw value is reported at its trace line; a missing
  debug roll and a wrong engine draw count are detected; stayhidden is refused before any side effect.
- system (2, SYNTHETIC world, stand-in services): 8 sectors x 6 patrol ships, 600 frames: 1, 2, 3, 8 and 16
  threads give identical effect logs and state hashes; an entity's effects are identical with 0 or 5
  neighbours in its sector (per-entity streams).

## 9a. Native result (M2a bracket capture)

NATIVE, lane b3 (profile 45853), new world `custom_creative`, about 8 min (ended early when the queued job was
moved to the release x4lane copy; the capture streams to stdout and is complete up to then), 8 Patrol job
ships, 85 interpreter brackets, 503 deliveries. Evidence: `.local/evidence-061-withheld`
(engine-output.log, `x4ai-check` output); summary: rewrite-coord/results/2026-10-26-ai-orders-m2a-native-capture.md.

| check | result |
|---|---|
| `<wait>` wake times (Patrol L317, L509) | **24/24 bit-exact** (formula of section 3; the wait draws last) |
| Patrol first run (job ship, `$space` null) | 2 draws on 8/8: L199 `chance="0"` debug + L317 (chance-0 lines draw) |
| Patrol restart (`$space` set by L201/L204 edits) | 1 draw on 10/10 |
| Patrol leg L317 -> L493 | 10 draws on 12/12 = 3 script draws (L398, L430, L485) + 7 engine (create_position + get_safe_pos) |
| L493 -> L509; sector patrol L509 -> L317 | 1 (4/4); 2 (2/2: L511 roll failed, L317) |
| lib.target.selection without target | 3 (L67, L80, L293) on 3/3 |
| seek after distress interrupt / gravidar hit | interrupt run 1 (L394) then 3 (L572, L587, L615) -> Attack; 3 (L394, L432, L448) |
| orders.base | 1 before each order (L349 `chance="0"`), abort handler 1 (L54 `chance="0"`) |
| gravidar scans while flying, no match | **419/419 drew exactly 1** (distress `check_all chance="80"`) |
| unmatched signals / matching scan / global deliveries | 1 (73/73) / 0 / 0 |
| open | seek start 6 (8x) vs 4 (4x): get_safe_pos engine draws vary; one matched distress signal drew 2 (condition-level `debug_text debugchance` at L383, to confirm) |

## 10. Native steps needed (next stage)

1. Done offline (M2a): `aicapture.h` bracket capture on ai-driver's `ad_observer`, tested with synthetic
   engine structures (test_aicapture.c, both modes) and in a local integration build of the ai-driver tree
   (all ai-driver/fixedtick tests still pass); probe Lua hookup checked under LuaJIT with a mock module.
2. Done natively (section 9a); a full-length run (from lanes/release x4lane.py, `--wait`) adds zone patrols'
   second legs and resupply rolls.
3. M2b: value capture (slot-3 swaps on FindComponent, GetGlobalPath, CreatePosition, GetSafePos, the effect
   classes; facts per bracket) for a full replay of native Patrol traces. Register sites in hooks.tsv first.
4. Cheap native check of the chance-0 finding: a test MD cue that executes 100,000 `debug_text chance="0"`
   in one action list, with the rng-streams counter: the MoveWorker draw count of that frame must rise by
   100,000.

## 11. Open questions

- AICallStackContext global-handler delivery (0x1062f70) and the order of the two listeners for one event
  (does a consumed global handler hide the event from the blocking interrupt?): the capture shows it.
- When exactly `<on_abort>` runs relative to the aborting handler's interrupt run (M1 emits it inside that
  bracket).
- Whether `get_safe_pos` draws (no direct RNG in 0xc72380; callees not checked): captured `draws` answers it.
- find_zone without `multiple`: first match in tree order or random (0xd2ab50 result store not yet read).
- Value tags: time literals are assumed tag 8 (f64) and lengths f32 (wait asks for tag 8; ranges subtract in
  the type of max); the capture's bit-exact wake times confirm or refute it.
- Event conditions with `object="$space"` are evaluated against the current `$space`; whether the listener
  follows a later `$space` change is unknown.
