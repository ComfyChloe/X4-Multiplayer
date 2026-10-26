# Engine rewrite: economy (production, storage, trade offers, reservations)

Owner: economy agent, branch `contrib/ferrite-rw-economy` (rewrite program: reports/REWRITE-PROGRAM.md).
X4 9.00, Steam build 23660954, Windows RVAs (VA = 0x140000000 + RVA). Static analysis of the disassembly
and of decompiled functions in a private read-only Ghidra copy (`x4-re/work/rw-economy/gh`); labels in
`x4-re/labels/rw-economy.tsv`. This report describes behaviour; it contains no engine code.

Evidence is marked **[static]** (RE only), **[synthetic]** (our tests, no X4) or **[native]** (a lane run,
with the log path). Nothing in this report is a native economy result unless marked [native].

## 1. Status

| stage | state |
|---|---|
| 1 data model crate `rust/x4mp-economy` | done: ef13948, da802ad (14 synthetic tests) |
| 2 stress scenario + simprof on a lane | scenario written (`dev/econ_scenario/x4mp_econ_stress`); first run cut short by the PC reservation at 01:13 (not a result, see 2.2); rerun queued |
| 3 production capture/replay, exact | sampler `native/engine/econcap.h` + replay `x4mp-econ-replay` built and synthetic-tested; lab scenario `x4mp_econ_lab` written; native capture queued |
| 4 trade offers + reservations with the barrier snapshot | crate side done (market, msg); engine RE in progress (section 6) |
| 5 shadow | not started |

## 2. Where economy work runs (census)

### 2.1 Static: no per-frame economy pass

- **Production is not walked.** `U::ProductionModule` keeps `U::Destructible::vfunc674` as its Move
  (slot 674 of vtable 0x2b2e9c0); the station's Move is `U::Container::vfunc674` 0x41a430 (children,
  attention), with no production in it. [static]
- **Production is caught up lazily** by `U::Container::UpdateProduction` 0x41a910 (time stamp
  Container+0xa18). About 45 callers, all *readers or writers of the station's cargo*: FFI `GetCargo`,
  `GetNumCargo`, `GetStorageData`, `GetProductionModuleData`, `UpdateProduction` 0x1f20a0, cargo filters
  (`ContainerFilterByCargo`), the cargo read 0x3fa850, the single-ware and filtered cargo paths
  0x3faa90 / 0x3fa8d0 used by cargo add/remove 0x3ff1c0, trade code, workforce (0x430d40, 0x431f90).
  A call with a time not later than +0xa18 returns at once, so only the first touch per frame works. [static]
- **Trade offers** are recomputed by `0x4079e0` (target of the FFI `UpdateProductionTradeOffers`
  0x1f21a0 and of `U::Container::OnUpdateTradeOffersEvent` 0x3f6670, listener slot 288), and by the
  production state machine when an item starts waiting for resources and after products are stored. [static]
- **Trade search** is script-driven: `Scripts::FindTradeOfferAction::Execute` 0xb8f000 (`find_buy_offer`,
  `find_sell_offer`; 41 sites in base AI, 28 in MD) → 0x7f31b0 (filter set-up) → 0x7f2880, which takes the
  space's zones (vfunc 0x1cf8), walks each zone's stations (child bucket 2, class 0x62, state 0) and other
  containers (0x8382b0) and filters each container's two offer lists at **Container+0x8f8 and +0x910**
  (0x894df0). Build storages are walked from child bucket 14. [static]
- **Jobs** (ship quotas) run in VBI slot 11, `U::JobEngine::vfunc1` 0xf0b870. **MD** faction economy
  (`factionlogic_economy.xml`, `GenerateShortageData` scanning stations every 100 ms with offer finds)
  runs in the MD phase. [static]
- Workforce: container upkeep 0x43ed20 / 0x43fa80 run inside every catch-up; parameters
  `libraries/parameters.xml` `<workforce><update interval="600" variation="60"/>`. [static, partial]

### 2.2 Measured shares

| run | economy union | trade search | offers/prices/storage | zone reservations | trade actions | production | where |
|---|---|---|---|---|---|---|---|
| 8 high-attention sectors (lead, 2026-10-25, `X4-Multiplayer-fixed-tick/.local/evidence-239-withheld`) | ~1.5 % | 1.02 % | - | 0.26 % | 0.11 % | ~0 | AI phase |
| lane b2 `stress1`, plain custom_creative world, **cut short at 551 s by the PC reservation; stress MD failed (script error), so this is the unloaded universe; other X4 instances ran** (`.local/evidence-097-withheld`, census `x4-re/scripts/rw-economy/econ_census.py`) | 1.89 % | 0.99 % | 0.47 % | 0.27 % | 0.23 % | 0.18 % | AI 1.32 %, walk 0.27 %, events 0.24 % |

Both are [native] samples of MoveWorker time, but neither is an economy stress result: the second run is
explicitly *not a result* (lead, 01:13). Shares are functional (timing not trustworthy). The stress run
(160 factory clones with 320 station traders) is queued (`.local/evidence-071-withheld`).

**Reading:** the economy's engine-side cost today is dominated by the AI trade search, which runs inside
`AIDirector::Move` (ai-driver/ai-orders own that loop); production itself is cheap because it is lazy.
The economy matters for the rewrite less as CPU than as **shared state**: every trade search reads other
sectors' stations and *triggers their production catch-up* (section 7).

## 3. Production (U::ProductionModule, U::Container) [static, decompile-confirmed]

### 3.1 Catch-up (`U::Container::UpdateProduction(container, time, force)` 0x41a910)

1. Unless `force`, return when +0xa18 holds a time `>= time`. Store `time` in +0xa18.
2. Collect the operational production modules: child bucket 8 onwards, class 0x4f, component state
   +0x68 == 0, a global filter object at 0x2f57c98, not paused (+0x398 unset).
3. Run container upkeep 0x43ed20 and 0x43fa80 with the thread's game time; fill a running cargo list
   (0x3fa850 → 0x3fa290); recompute the production storage allocation 0x420aa0 when +0x931 is clear.
4. For each module `PrepareUpdate` 0x761320 gives a queue time; a time `t` with `-0.9999 < t <= time`
   enters a queue sorted descending by `(t, module address)`.
5. `spread = 0.5f * queue length`. While the queue is not empty: take the back (smallest time), call
   `Step(module, t, ...)` repeatedly while it returns the same `t` (instantaneous transitions), store the
   returned time; if it is still due, re-queue it **unless the element before it has a time `>= t2`
   (then it stays at the back; the engine compares only the time there)**, otherwise pop it.
   After each step, if the module *now in the back slot* (after a possible move) is producing and
   functional: `+0xa30 = max(+0xa30, t2)`; and when no module was producing at the start and +0xa4c is
   clear, `+0xa28 = min(+0xa28, t2)` (only when +0xa28 is set).
6. `UpdateActivity` 0x7653f0 per module (activate/deactivate: `vfunc682/683` post immediate
   Activate/Deactivate events, visual state only); processing modules (class 0x4e) 0x576b70.

**Consequence:** every transition happens at its own event time, independently of when the catch-up is
called, except where Step or PrepareUpdate uses the call time (next section). With no outside cargo
change, a late catch-up equals an eager one ([synthetic] test `lazy_and_eager_catch_up_agree...`).

### 3.2 State machine (`Step` 0x761720, switch on +0x3f0, jump table 0x763044)

States (enum table 0x24d5cd0): 1 empty, 2 queuenotstarted, 3 queuestarted, 4 choosingitem,
5 itemstarted, 6 waitingforresources, 7 cyclestarted, 8 producing, 9 waitingforstorage,
10 cyclefinished, 11 itemfinished, 12 queuefinished, 13 paused (not in the switch).

| from | condition | effect | to |
|---|---|---|---|
| paused (+0x398 set) | - | returns -1 (leaves the queue) | - |
| any but 1 | no items | next=-1, item=cycle=-1, start=t | 1 |
| 1 | items | start=next=t | 2 |
| 2 | t >= next | start=next=t | 3 |
| 3 | - | item = 0, or `Random::I32(n)` when the last item repeats | 4 |
| 4 | pending products | store what fits (0x8f15b0 free amounts, 0x3faff0, cargo add reason 0x12) | - |
| 4 | products of a cycle can be stored (0x424b90 allocation and 0x356010 capacity) | as 5 | 5/6/7 |
| 4 | cannot store | another repeating item that can be stored: `next += 1 + FloatState(4)`, item=that, → 5; else `next += max(spread,5) + FloatState(spread)` | 4/5 |
| 5 | - | start=next=t, cycle=0, ItemStarted events (scheduled) | as 6 |
| 6 | primary resources present (0x356090) and (not player-owned or licence) | consume primary+secondary+tertiary (0x3fb9f0, cargo remove reason 0x11), start=next=t | 7 |
| 6 (first time) | missing | start=next=t, trade offers refreshed 0x4070e0 (thread time) | 6 |
| 6 (again) | missing | `next += max(spread,5) + FloatState(spread)` | 6 |
| 7 | - | start=t, `next = item.time_factor * factors.time * method.time + t` (zero duration: + retry delay); non-repeating or research items refresh offers | 8 |
| 8 | t >= next | - | 9 |
| 9 | research item | player research finished (0x154840) | 10 |
| 9 | can store | store products, pending adjusted (0x8f32f0), offers refreshed | 10 |
| 9 | cannot store | products go to the module's pending list (+0x3f8) | 10 |
| 10 | cycle+1 < item.cycles | start=next=t | 7 |
| 10 | else | cycle=-1, ItemFinished events (scheduled) | 11 |
| 11 | repeating item | next item (wrap) | 4 |
| 11 | one-shot item | removed; queue empty → 12 | 4/12 |
| 12 | - | next=-1 | 1 |

Arithmetic (all reproduced in `production.rs`):
- products per cycle (0xe37370): `cvttss2si((float)cvttsd2si((double)method.amount * (factors.product * item.product_factor)) * 1.0f)`;
- resources per item start (0xe37400/0x151930): primary in replace mode (empty when `|factor| < 1e-7`
  or `|cycles| < 1e-4f`), secondary/tertiary in append mode, factor `factors.k * item.k`, multiplied by
  `(float)item.cycles` — resources for **all cycles of an item are taken at its start**;
- retry delays in f32: `max(spread, 5.0f) + FloatState(spread)` widened to f64.

**Randomness:** only retry delays and the random item choice draw from the thread stream TLS+0x318.
A normal producing cycle draws nothing.

**Call-time dependencies** (the parts that are *not* event-time accurate):
- PrepareUpdate wakes a module waiting for resources *at the call time* when its primary resources are
  present: after a delivery, production restarts at the next touch of the station, not at a retry time;
- the trade-offer refresh 0x4070e0 and the posted events use the thread's game time.

### 3.3 Fields

ProductionModule: +0x68 component state; +0x360..+0x387 local efficiency base (5 x f64, input of
0x7602d0); +0x388 start, +0x390 next, +0x398 paused since, +0x3a0 paused total (f64); +0x3a8 item,
+0x3ac cycle (i32); +0x3b0..+0x3d7 current factors (time, product, primary, secondary, tertiary);
+0x3d8/+0x3e0 item vector (0x60 each); +0x3f0 state; +0x3f4 paused flag; +0x3f8 pending ware list;
+0x428 active flag; +0x430 operational-since time.
Item: +0x00 WareClass*, +0x08 WareProduction*, +0x10..+0x30 factors, +0x38 cycles, +0x3c repeat, +0x40 stats.
WareProduction: +0xb8 product, +0xc0 kind (`== *(0x2f61000)`: research), +0xe8 time, +0xf8 amount,
+0x170/+0x1a0/+0x1d0 resource lists. Pause 0x764d30 / Resume 0x765020 (shift start/next/item timers by
the paused time). Efficiency `ComputeEfficiency` 0x7602d0: base factors, then method effects
(efficiency, sunlight, manager, engineer, defence control, workforce, secondary resources), each validated
("Invalid efficiency value after ...").

## 4. Storage and cargo [static]

- **Station cargo = sum over operational storage modules** (child bucket 10 onwards, class 0x63) of
  each module's ware list at StorageModule+0x360 (0x3fa290). Ships use a container-level list (+0x710).
- **Ware amount lists** (small vectors) store entries self-relative XOR-encoded: entry at address A holds
  `q = A ^ ware` and `d = (low31(q)) ^ amount`; data inline when +0x20 == 2, count at +0x28. Any reader of
  engine memory must decode with the entry's own address (econcap.h does).
- **Can store** (state 4 and 9) = 0x424b90 (per ware: production storage allocation from the list at
  Container+0x940, recomputed by 0x420aa0 when +0x931 is clear, minus the running amount) **and**
  0x356010 (0x3faff0 computes what physically fits). **Has resources** = 0x356090 (0x3fb9f0 computes what
  can be taken). Cargo add/remove 0x3ff1c0(container, reason, list, flag) with reasons 0x11 (production
  consumption) and 0x12 (production output).

## 5. Ware IDs, classes

Class ids (table 0x22c6790): production 0x4f, storage 0x63, station 0x62, container 0x6f, buildstorage 8,
buildmodule 9, cargobay 0xc, habitation 0x31, processingmodule 0x4e, welfaremodule 0x6c, ship 0x75,
sector 0x58, zone 0x6d. Ware ids resolve through the WareDB map *(0x6cf1620) keyed by FNV-1a-32 of the id
(GetWareReservationsForWare 0x1ec500). WareClass+0x10 is the id string.

## 6. Trade offers and reservations [static, in progress]

- Offer lists per container at +0x8f8 and +0x910 (the search filters both). `U::Trade` (vtable
  0x2b97240) derives from `U::OrderIDBase`; Lua `GetTradeData` 0x2afab0 → 0x2e8500 maps the fields.
- Reservations: `add_ware_reservation` → `Container 0x4128c0(container, flags, reserver id, ware, amount,
  duration, ...)` (default duration from the settings block +0x5170, `parameters.xml` reservation
  trade=3600 s), removal 0x413060, query 0x412490. Layout: next batch.
- Next: UpdateTradeOffers 0x4079e0 (amounts, prices 0x403a60), the reservation store, ExecuteTrade
  0x10a90e0, ClampTradeAmount 0xc05d60, CreateTradeOrder 0xbb66d0.

## 7. What this means for per-sector execution

1. **Foreign reads write.** A trade search from sector B reads station S in sector A and runs S's
   catch-up (a write: cargo, module state, offers). In a parallel round this is a cross-sector write.
   The design: S's catch-up only ever runs in S's own sector job; a foreign search reads the barrier
   snapshot of S's offers (`market::OfferSnapshot`).
2. **The catch-up time is the frame time of the first touch.** All touches in one frame use the same
   thread game time, and only the first one works. So, to stay exact, the owning sector must run S's
   catch-up in frame k iff S was touched in frame k (by anyone), *with the cargo state of the first
   touch*. A foreign touch therefore becomes a stamped message "touched S in frame k at stamp x"; the
   owner applies it in stamp order relative to S's local cargo changes of that frame. Touch without
   cargo change in the same frame (the common case) is order-free: catch-up at t_k.
3. Without that, the only divergence is a waiting module waking one touch earlier or later (call-time
   dependency, 3.2). It is detectable (shadow field `start` of state 7) and counted, not silent.
4. **Thread-unsafe statics in efficiency:** 0x7602d0 keeps a single-entry sunlight cache in globals
   (time at 0x31c1fa0, value 0x31c1f9c, module 0x6d152f0). A worker running catch-ups must use its own
   cache (or the model's per-sector one); the cache is only an optimisation of a pure function.
5. Production events are posted scheduled (immediate flag 0) at the thread time, so they leave the
   catch-up and are delivered in the next ProcessDueEvents: an ordered message per event is enough.
6. Retry delays use the thread RNG; with per-entity streams (program rule) the rewrite's delays differ
   from the engine's by construction. The capture verifies the rest exactly and the draws against the
   recorded times (section 9).

## 8. Data model (`rust/x4mp-economy`) [synthetic]

- `production`: `Module` (the fields of 3.3), `Item`, `Method`, `Factors`, `ContainerTimes`;
  `update_production(...)` and `Module::step/prepare` are the engine algorithm; `StationEnv` supplies
  storage/resource checks, cargo changes, efficiency and offer refresh; `ProductionRng` the draws.
- `market`: `Offer` (id, owner, sector, tree key, ware, side, amount, reserved, unit price in cents,
  version), per-sector `OfferBook`, `OfferSnapshot` (tree order; `find(ware, side)`, `get(id)`).
- `msg`: `EconMsg { stamp, actor, seen_version, op }`, `apply_in_order` (stamp order, stale decisions
  become `Conflict`, never silently applied).
- `sector`: `SectorEconomy` + `step_frame` (parallel phase, barrier, snapshot); stand-in storage model
  until 0x424b90/0x356010/0x420aa0 are modelled.
- `capture`: X4ECON parser, replay (`x4mp-econ-replay`).

Cheap queries for other systems (faction AI): per station `cargo` (sorted ware list), per module `state`,
`next` (next event time), `factors`, item list; per sector `OfferBook`; galaxy `OfferSnapshot::find`
(O(offers of that ware)). All are plain data, no engine call.

Fit with x4mp-sim (interfaces.md v0): stations are entities with economy components; `run_sector` =
`SectorEconomy::run`; cross-sector reservations are `Target::Barrier` messages sorted by our `Stamp`
(the AI driver provides the original order rank); the offer snapshot is a published per-sector column.

## 9. Capture and replay (stage 3)

- Engine side `native/engine/econcap.h` (in the fixed-tick probe, `X4LAN_ECONCAP=true`): MD raises
  `X4MP.EconWatch` with a station; per frame (slot 5) the sampler resolves it by UniverseID (AutoIDMap,
  generation-checked), checks vtables, reads the fields of 3.3 and 4, and writes C/M/W/D/F records when
  something changed (`[X4LAN] ECONCAP ...` in the debug log). Data only: no writes, no engine calls, no
  new hook site (hooks.tsv row "VBI slot 5 (no new site)"). [synthetic: test_econcap.c PASS]
- Replay `x4mp-econ-replay <log>`: for each station record whose +0xa18 changed, the model runs the
  catch-up from the previous record and must reproduce every module field bit for bit and the cargo;
  classes: exact, exact with recorded draws (one retry draw per module, checked to lie in [0, max)),
  rng-dependent (several draws), external (cargo changed by something else), mismatched (first
  difference reported). [synthetic: replay_synthetic.rs PASS]
- Lab scenario `dev/econ_scenario/x4mp_econ_lab`: 40 isolated factory clones (trade offers disabled, no
  traders), resources half full or almost empty, cargo read every 7 s (even) / 61 s (odd).

## 10. Tests

- `cargo test -p x4mp-economy`: 14 pass [synthetic].
- `build.py --fixedtick --test`: test_fixedtick PASS (with econcap compiled in); test_econcap PASS
  (run directly); test_simprof's 3.0 % suspend-overhead bound failed at 3.01-3.11 % while four lane X4
  instances and other jobs ran (timing-sensitive test, unrelated to this branch).

## 11. Native steps needed

1. `stress2` (lane, simprof profile mode, ~12 min): economy share under 160 clones + 320 station traders.
2. `lab1` (lane, probe mode + econcap, ~12 min): production capture; replay must report 0 mismatches.
Both are queued with `--wait` (`.local/evidence-071-withheld`).
