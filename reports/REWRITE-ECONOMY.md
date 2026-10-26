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
| 1 data model crate `rust/x4mp-economy` | done: ef13948, da802ad |
| 2 stress scenario + simprof on a lane | done [native]: `stress3` pass (160 clones, 306 station traders); economy ~2 % of MoveWorker, scales with trade activity (2.2; rewrite-coord/results/2026-10-26-economy-stress.md) |
| 3 production capture/replay | lab5 native isolated factories: 3762 catch-ups, 2992 exact + 639 with one recorded retry draw, 131 RNG-undecidable, zero mismatches (section 9); trading capture pending |
| 4 trade offers + reservations with the barrier snapshot | reservations at the owner applied at the barrier, price curve, reservation totals (693712d) [synthetic]; UpdateTradeOffers structure (section 6.3) [static]; offer amounts next |
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
| lane `stress1`: plain custom_creative world (stress MD failed, no clones), game time 30-554 s, 53 windows; **cut short by the PC reservation, not a result**; other X4 instances ran | 1.95 % | 1.02 % | 0.48 % | 0.27 % | 0.24 % | 0.19 % | AI 1.37 %, events 0.25 %, walk 0.27 % |
| lane `stress2` (b2): ~157 extra factory clones (+16 % of 983 factories), 314 station traders whose trade loop failed (no defence NPC: `trade.find.commander` "Failed to set null.$lookingfortrades"), game time 130-794 s, 67 windows; **ended by an all-lanes stop**; other X4 instances ran | 1.85 % | 0.90 % | 0.54 % | 0.22 % | 0.25 % | 0.19 % | AI 1.30 %, events 0.28 %, walk 0.22 % |
| **lane `stress3` (b1): 160 factory clones, 160 managers and defence NPCs, 306 working station traders**, game time 90-538 s, 45 windows; **pass**; shares exclude the econcap sampler (slot 5, 26.6 %; fixed in 36750e5); other X4 instances ran | 2.65 % | 1.49 % | 0.71 % | 0.18 % | 0.38 % | 0.28 % | AI 2.04 %, events |

All rows are [native] samples of MoveWorker time with untrustworthy timing (shares only). Census:
`x4-re/scripts/rw-economy/econ_census.py` on the symbolized folded stacks
(`.local/evidence-096-withheld`). Economy samples **per wall second** (game time ran at
wall time; economy work is per game second, so this is the comparable number):

| function (inclusive) | stress1 /s | stress3 /s |
|---|---|---|
| trade search 0xb8f000 (core 0x7f2880, filter 0x894df0) | 10.2 (8.1, 2.6) | 10.9 (8.9, 3.0) |
| offer refresh 0x4079e0 (ware prices 0x403a60) | 4.6 (1.6) | 5.0 (1.8) |
| production catch-up 0x41a910 | 1.8 | 1.9 |
| trade execution 0x10a90e0 / order creation 0xbb66d0 | 1.0 / 1.3 | 1.2 / 1.3 |
| zone reservations 0x841720 | 2.7 | 1.3 |
| economy union | 19.4 | 19.3 |

16 % more factories with 306 more station traders added 7 % to the trade search and 9 % to the
offer refreshes per second; the union stayed level (zone reservations fell). stress2 (clones without
working traders) shows that idle stations cost almost nothing: production is lazy and their offers
are refreshed by events only.

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

## 6. Trade offers, prices and reservations [static; models synthetic]

### 6.1 Reservations (Container+0x980)

- 48-byte entries in insertion order: +0x00 u32 word (bits 0-1 type, bits 2-4 flag classes), +0x08
  reserver id, +0x10 linked trade order id (0 = plain reservation), +0x18 WareClass*, +0x20 amount,
  +0x28 expiry (f64, unset = -1). Type bit 0 = **outgoing** (wares of this container that someone will
  take), bit 1 = **incoming** (wares someone will bring); valid type mask `*(0x2f54898)` = 3.
- `AddWareReservation` 0x4128c0, `RemoveWareReservation` 0x413060, `GetWareReservationAmount`
  0x412490, per-ware totals 0x4126d0, expiry test 0x4133e0, entry amount 0x3ef840, per-change offer/cache
  update 0x413480.
- **Expired** (removed by every scan): linked order no longer in the order registry, expiry passed,
  empty type bits, or reserver gone. A linked entry's amount is its order's amount (Trade+0x40), or 0 for
  an outgoing entry whose order has flag 0x200000.
- **Add** does not check availability: it merges a plain entry of the same reserver, ware and flag bits
  (adds, or replaces with `replace`), sets expiry = now + duration (default settings+0x5170 = 3600 s,
  `parameters.xml <reservation trade="3600"/>`), and ends with a forced offer refresh
  (`UpdateTradeOffers(container, 1, 0)`). Remove reduces one entry (positive amount below the entry's)
  or removes the matching ones; it refreshes when something was removed.
- **Totals** 0x4126d0 split by type for one flag class (bit 2 and bit 4 as requested, bit 3 clear); the
  ware price asks for class (0, 0).

### 6.2 Prices

- Conversions of an offer's unit price (cents, Trade+0x30 tag): total price with a modifier 0x891d00,
  relative price 0x891b20 (f32, -2..2), quantity factor 0x891de0; mixed f32/f64 as in the disassembly
  (the decompiler shows all-float). Modelled bit for bit (`prices.rs`).
- **Station ware prices** 0x403a60(container, out map, ...) fill a map ware → (buy price, sell price);
  callers: `UpdateTradeOffers`, FFI `GetContainerWarePrice`, `GetObjectValueAction`, build/repair price
  estimates. Order of work: production catch-up at the thread time (0x41a910), allocation refresh
  (0x420aa0) when +0x931 is clear, cargo (0x3fa290), stock = cargo + incoming - outgoing (totals class
  0/0), production needs per operational production module and non-research item over two horizons
  (cycles in settings+0x5138 and +0x5140, products and resources scaled by the module efficiency
  0x7602d0), habitation consumption, build-storage needs (0x4350d0 gives a build time, 300 s when none;
  settings+0x51a8 = 0.25 is the share of build resources that must be present for the maximum price),
  player overrides (Container+0x798: buy and sell price per ware), trade-ware flags (Container+0x778:
  sorted (ware, flags), bit 1 sellable 0x403690, bit 2 buyable 0x403610), a buy-price factor
  (Container+0xac8, f32) and an addition of the ware's maximum price times a 0x14dd240 interpolation
  for wares of a further list whose amount reaches settings+0x5148 (reading, not yet confirmed).
- **The price curve** 0x34cde0(ware, stock, target), f64: minimum price when stock > target; maximum
  price when stock <= 0 or target <= 0; otherwise with h = target/2, d = clamp(stock - h, -h, h)
  (average when |stock - h| < 1e-7), s = avg - min (d >= 0) or max - avg (d < 0), k = -s/0.7:
  `trunc(((-s - k) / (target^2 * (h/4))) * d^3 + (k * d) / h + avg)`: maximum at an empty stock,
  average at half the target, minimum at the target, steepest in the middle. Modelled bit for bit
  (`prices::stock_price`, operation order from the disassembly).
- Resource buy price (static reading, to be confirmed with captured offers): curve(stock - need over the
  short horizon, min(allocation, need over the long horizon - need over the short horizon)); wares
  without production use the allocation as target.
- **Settings block** (`*(0x6cf1538)` +0x5130..+0x51b8, filled from `parameters.xml <economy>` by
  0x632360; defaults from 0x63e760): +0x5130 rounding up (false), +0x5138 144000.0, +0x5140 10800.0
  (default; XML resource min/max are 3600/144000), +0x5148 1800.0, +0x5150 500000000 (product factor
  5 M Cr in cents), +0x5158 7200.0, +0x5160 43200.0 (storage normalize), +0x5168 7200.0 (player
  reservation), +0x5170 3600.0 (trade reservation), +0x5180 18000.0, +0x5188 14400.0, +0x5190 300.0,
  +0x5198 10000, +0x51a0 20, +0x51a8 0.25 (build min), +0x51b0 0.1f, +0x51b4 0.3f (ship sale factor).
  Which XML attribute fills which field is not yet traced (the parser uses attribute-id tables); a
  one-time capture record of the block is the cheap way to pin the values natively.

### 6.3 UpdateTradeOffers 0x4079e0(container, force)

1. Returns at once for a container in state 3, when application flag 8 is set, or (after setting the
   next-update time +0x928 = now + 1200 s) when trade offers are disabled (vfunc 0x2318 false or
   +0x930 bit 2: `set_tradeoffers_enabled`).
2. **Workforce update** 0x431f90(container, 0) with its own timer (+0xa38): it calls the production
   catch-up and draws from the global RNG (`Random::FloatState`, `Random::I32`); so offer refreshes are
   one of the places where workforce randomness happens.
3. Unless `force`, returns while now < +0x928 (when set).
4. Moves the current offer vector +0x8f8 out, computes the price map (6.2) and the amount lists:
   wanted amounts (0x41f310), sellable amounts (0x41ea20), the sellable and buyable ware lists
   (0x41b4b0, 0x41bfb0), processed wares (0x41d7e0, mapped to their products), reservation totals
   (0x4126d0), build-storage and supply needs (0x42ae40, 0x428970) and a budget check (0x425260
   against an account amount; reading, not yet confirmed).
5. Per ware: **buy offer** amount = max(wanted - incoming, 0), desired (Trade+0x50) = max(desired - incoming,
   0), unit price = buy price; **sell offer** amount = max(sellable - outgoing, 0), unit price = sell price.
   An existing offer of the same ware and side (found in the old vector with trade filters) is updated in
   place (+0x40, +0x50, +0x30, 0x891ee0) and moved back into +0x8f8, so offer ids stay stable; a new one is
   created (`U::ProductionSpawnSource`, 0x894530). Each offer's average price and amount go to
   0x6a5c80 on the global object *(0x6cf1868) via 0x407650 (probably the economy log, category
   `tradeoffer`). Old offers not reused are removed.

### 6.4 Footprint (for per-sector execution)

- Reads: the container (cargo, modules, reservations, allocation, overrides, trade rules, subordinates'
  supply and build needs), global read-only data (ware DB, settings block), the order registry (linked
  reservations, existing offers).
- Writes outside the container: **order registry** (new offers get registry slots), **event queue**
  (UpdateTradeOffersEvent scheduling, workforce events), **thread RNG** (workforce update), **economy
  log** (probably; the global object at *(0x6cf1868)).
- Consequence: a sector job may run a station's refresh only with per-sector (or barrier-deferred)
  registry allocation, its own event queue and RNG stream, and a deferred log; reservations from other
  sectors arrive as barrier messages (`msg.rs`), and the refresh they cause runs once per owner at the
  barrier before the next snapshot is published.

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
  storage/resource checks, cargo changes, efficiency and offer refresh requests; `ProductionRng` the draws.
- `reservations`: `ReservationList` (6.1: add, remove, amount, totals, expiry, linked amounts) with a
  `ReservationEnv` for reserver and order lookups; `from_entries` builds a captured list.
- `prices`: offer price conversions and the stock price curve (6.2).
- `market`: `Offer` (id, owner, sector, tree key, list, position, ware, side, amount, unit price in
  cents, flags, version), per-sector `OfferBook` (`touch` gives an owner's offers of a ware a new
  version), `OfferSnapshot` (tree order; `find(ware, side)`, `get(id)`).
- `msg`: `EconMsg { stamp, time, actor, seen: (offer, version), op: Reserve | Release at owner }`;
  `apply_in_order` applies them in stamp order to the owners' reservation lists (stale decisions become
  `Conflict`, never silently applied) and returns the owners to refresh, once each, first request first.
- `sector` (on x4mp-sim v0.5, merged 03623ab): components `EconStation` (sector-local: times,
  modules, cargo, capacity, reservations, offer-refresh flag, the station's 'ECON' generator
  stream) and `EconOffers` (published: other sectors read it from the snapshot); system
  `economy` (run_sector: catch-up, offer refresh, synthetic demand; barrier: reservations in
  x4mp-sim total order, then one refresh per touched owner). Offer refresh and storage are
  stand-ins (engine price curve and reservation totals, capacity-share target) until 6.3 step 4 and
  0x424b90/0x356010/0x420aa0 are modelled. Ids and the engine generator are x4mp-sim's.
- `capture`: X4ECON parser, replay (`x4mp-econ-replay`).

Cheap queries for other systems (faction AI): per station `cargo` (sorted ware list), per module `state`,
`next` (next event time), `factors`, item list; per sector `OfferBook`; galaxy `OfferSnapshot::find`
(O(offers of that ware)). All are plain data, no engine call.

Fit with x4mp-sim (interfaces.md v0.5, economy section there): stations are entities with economy
components; cross-sector reservations are `Target::Barrier` messages in x4mp-sim's total order (order
key, sector, seq; the AI driver can set the original order key); the offers are a published column.

## 9. Capture and replay (stage 3)

- Engine side `native/engine/econcap.h` (in the fixed-tick probe, `X4LAN_ECONCAP=true`): MD raises
  `X4MP.EconWatch` with a station; per frame (slot 5) the sampler resolves it by UniverseID (AutoIDMap,
  generation-checked), checks vtables and writes records when something changed (`[X4LAN] ECONCAP ...`
  in the debug log): W (ware: id, volume +0xc0, tags +0x100), D (method), F (frame), C (station times,
  cargo), A (production allocation +0x940 and its valid flag +0x931), S (per operational storage module:
  capacity and cargo tags of its storage defaults, own contents), O (offers: seller +0x10, buyer +0x18,
  amounts, unit price, flags), V (reservations; linked entries add the order's amount and flags), M
  (production modules). Data only: no writes, no engine calls, no new hook site (hooks.tsv row "VBI
  slot 5 (no new site)"); readable-region checks are cached per frame (a VirtualQuery per field cost a
  quarter of the MoveWorker in stress3). The format is documented in `capture.rs`. [synthetic:
  test_econcap.c PASS]
- Replay `x4mp-econ-replay [--all] <log>`: for each station record whose +0xa18 changed, the model runs
  the catch-up from the previous record and must reproduce every module field bit for bit and the cargo.
  Storage decisions use the captured allocation (0x424b90) and storage modules (0x356010: a module
  accepts a ware when the tag sets intersect, 0x806e10; modules of one tag group pooled); captures
  without them take the decision from the recording. Classes: exact, exact with the recorded retry draw
  (one per module, checked to lie in [0, max]), exact with the recorded storage decision (old captures),
  rng-dependent (several draws in one catch-up: undecidable from two snapshots), external (cargo changed
  by something else, e.g. trades), mismatched (every difference listed with `--all`). [synthetic:
  replay_synthetic.rs PASS, incl. allocation and physical-storage scenarios]
- **Native results** [native, lane runs, timing not trustworthy]:

| capture | catch-ups | exact | + recorded draw | + recorded storage | undecidable | external | mismatched |
|---|---|---|---|---|---|---|---|
| stress3 (12 trading clones; no A/S records) | 896 | 579 | 97 | 16 | 140 | 63 | 1: partial store of pending products (allocation) |
| lab4 (40 isolated clones; A records, no S records) | 1722 | 646 | 387 | - | 517 | 0 | 172: products blocked by full physical storage |
| lab5 (40 isolated clones; A/S records; short-gap reads every 3 s) | 3762 | 2992 | 639 | 0 | 131 | 0 | 0 |

  Every mismatch so far was a storage decision; the production state machine, the catch-up order,
  the efficiency factors and the retry arithmetic reproduced the engine everywhere else. lab4's
  storage-full stations (two container wares filled to half of what fits each) led to the S records
  and the physical check. lab5 completed on 26 October 03:33 EDT: all 40 clones and 74 production
  modules appear in the capture, with 3803 station records, 7122 storage records and 28 methods.
  Replayed using the hardened comparator at `60db4b5`, it has zero observed mismatches. The 639
  recorded-draw cases are conditional on one draw inferred from the recorded retry time; the 131
  multi-draw cases remain undecidable. This is bounded isolated production replay, not complete
  native economy equivalence. `trade1` (active traders with the full capture) remains queued.
  Evidence: `.local/evidence-086-withheld,x4lan-debug.txt}`, native b3/profile 45853,
  `timing_trustworthy=false`, lane restored. Log SHA-256:
  `c7d9fa8975c984b7483b354435a4323b6178ec05bd545a006af9f89ae8e8e210`.
- Lab scenario `dev/econ_scenario/x4mp_econ_lab`: 40 isolated factory clones (trade offers disabled,
  no traders); each resource filled to 1 %, 50 % or 25 % of what fits; cargo read every 3 s (even)
  and 61 s (odd stations).

## 10. Tests

- `cargo test -p x4mp-economy`: 18 unit tests, 4 replay tests, thread invariance (48 sectors x 4
  stations, 120 frames, identical state hash and barrier log with 1/2/4/16 threads; 134 reservations
  applied, 2719 conflicts (all demanders pick the same cheapest offer: worst-case contention), 134
  barrier refreshes, expiry exercised) [synthetic]. Clippy with `-D warnings` and rustfmt clean.
- `build.py --fixedtick --test`: test_fixedtick PASS (with econcap compiled in); test_econcap PASS
  (run directly); test_simprof's 3.0 % suspend-overhead bound failed at 3.01-3.11 % while four lane X4
  instances and other jobs ran (timing-sensitive test, unrelated to this branch).
- MD scenarios validate against the game's `md.xsd` (.NET XmlSchemaSet); the engine additionally rejects
  cues with only non-event conditions and no check interval (stress2 log), which the schema cannot see.

## 11. Native steps needed

Next: collect the already-queued `trade1` from `.local/evidence-073-withheld`, replay its
production transitions, then check offer targets/reservations against its full capture.
Preserve the earlier failed captures and distinguish external trades from model differences.
Historical run plans below describe how the lab/stress captures were prepared.

### Replay validation hardening (26 October, Codex continuation)

The replay previously omitted accumulated pause time, module identity/functional state,
efficiency factors, queue item contents and the container's `a4c` flag from its equality
test. These fields are now compared, with bit equality for floating point and named
first-difference diagnostics. Module identity/order changes between samples are rejected;
the snapshots do not explain entity replacement. Synthetic negative controls cover eleven
mutations, including signed zero, module replacement/reordering, queue contents and container
state. The replay still uses recorded efficiency as an input; comparing the resulting factors
does not independently validate the native efficiency calculation.

Validation: `cargo test -p x4mp-economy` (19 unit + 9 replay + 1 thread-invariance tests),
Clippy all targets with `-D warnings`, and rustfmt pass. Reanalysis of the existing native
captures preserves the previous counts: lab4 1722 transitions / 172 mismatches; stress3
896 / 1. These remain failed captures, not native acceptance. The replay now identifies
container time differences directly instead of misleadingly reporting equal module counts.

Capture integrity is also checked before replay: each station must contain the number of
module records its `C` record declares, including at EOF; module IDs/addresses cannot repeat;
and storage/allocation/offer/reservation records must name their enclosing station. Synthetic
deleted/truncated/duplicate/cross-station records are rejected. The suite is now 19 unit,
10 replay and 1 thread-invariance tests; both old native logs still parse and retain the same
failed replay counts.

1. `stress4` (lane, simprof profile mode + econcap of 12 clones, ~12 min; `stress3` was stopped): economy share with working
   station traders; offers and reservations of trading stations for 6.2/6.3.
2. `lab3` (lane, probe mode + econcap, ~12 min): production capture; the replay must report 0 mismatches.
Both run from the released lane tool with `--wait` (`.local/evidence-072-withheld`, `run_econ_lane4.ps1`).
3. Later: a capture record for the allocation list (+0x940), the trade-ware flags (+0x778), the price
   overrides (+0x798) and the settings block, to check the offer model against native offers.
