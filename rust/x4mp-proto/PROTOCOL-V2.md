# X4 multiplayer wire protocol v2

Status: **library, relay mode and tests only**. It implements migration step 1 of
`preparation/TARGET-ARCHITECTURE-PROPOSAL.md` (player IDs instead of slots,
frame-stamped messages, RTT telemetry, an owner on every replicated object) and the
sector-channel and relevance-tier rules of its section 4. Nothing in the game uses it
yet. All relay evidence comes from **synthetic UDP clients**. That is not evidence of
native multiplayer behaviour.

Code: `rust/x4mp-proto` (pure: no sockets, no clock, no hooks, no third-party crates)
and the opt-in relay mode `rust/x4lan-relay/src/v2/`. The version-1 text protocol and
relay are unchanged.

## 1. Datagram and header

One message per UDP datagram, at most **1200 bytes** (`MAX_DATAGRAM`; this fits the
1280-byte IPv6 minimum MTU after the IP and UDP headers). All integers are little-endian.

| Offset | Size | Field | Rule |
|---:|---:|---|---|
| 0 | 2 | magic | `F4 32`. `0xF4` is not ASCII, so a v1 relay always drops a v2 datagram and a v1 datagram is never mistaken for v2 |
| 2 | 1 | major | `2` |
| 3 | 1 | minor | `0` in this version |
| 4 | 1 | kind | 1-11, see section 4 |
| 5 | 1 | flags | reserved, `0` |
| 6 | 2 | body_len | must equal datagram length - 18 |
| 8 | 2 | player | client to relay: the sender. Relay to client: the recipient. `0` before admission |
| 10 | 8 | lease | the 64-bit lease from Welcome; `0` before admission |

A datagram is authenticated when `player`, `lease` (compared in constant time) and the
UDP source address all match the relay's record. The relay sends Hello replies only
when the session token is right, and it never answers a wrong token.

## 2. Versioning rules

* **Major** (`2`): any incompatible change. A decoder rejects other majors
  (`UnsupportedMajor`); the relay drops them silently. The refusal code `VERSION` is
  reserved for a later reply.
* **Minor**: only additive changes. A newer minor may
  * set new header flag bits, record flag bits or Subscribe/Roster flag bits;
  * append fields at the end of a message body;
  * append bytes at the end of a snapshot record (every record starts with its own
    length byte).

  A decoder that sees **its own or an older minor is strict**: it rejects reserved bits
  and trailing bytes. Given a **newer minor**, it ignores unknown bits and appended bytes.
  The fuzz tests assert that every datagram this decoder accepts at minor 0
  re-encodes to exactly the same bytes.
* New behaviour is negotiated through **capability bits** in Hello/Welcome (the relay
  grants the intersection with what it supports): `COMBAT_INTENT` 1, `MAP_TIER` 2,
  `INTENTS` 4, `EVENTS` 8, `ROSTER` 16. Unknown bits are kept. In 2.0 the relay sends
  Events only to peers with `EVENTS` and Rosters only to peers with `ROSTER`.
* New message kinds need a capability bit. Old decoders reject unknown kinds
  (`UnknownKind`), and the relay drops them.
* Enumerations whose values may grow keep unknown values: `Manoeuvre::Other(n)`,
  `RefuseReason`, `Reason`, `IntentKind`, `Bye.reason`. `MotionMode`, `Interest`,
  `Role`, event kinds and outcomes are closed in major 2.
* Decoding never panics. Every read is bounds-checked, every count is bounded, and
  every enumeration is validated (fuzzed: 200k mutated + 100k random datagrams, all
  truncations).

## 3. Identifiers and quantisation

| Type | Wire | Meaning |
|---|---|---|
| `PlayerId` | u16 | humans `1..=0x7FFE`, assigned by the relay (lowest free first, kept on resume); `0x7FFF` = server authority; `0` = none |
| `Owner` | u16 | `0` unowned; `1..=0x7FFE` human player; `0x8000 \| f` NPC faction `f` (index into a session faction table, see open questions); `0x7FFF` invalid |
| `ObjectId` | u64 | session network ID; `0` = none |
| generation | u32 | instance generation; a reused ID gets a higher generation |
| `SectorId` | u32 | session sector index; `0` invalid |
| frame | u64 | the publisher's simulation frame (fixed tick). Authority and client frames are separate clocks |
| token | 16 bytes | the 32-hex session token from `session.json` (v1 format) |
| nonce | 16 bytes | random per client instance |
| lease | u64 | random per admission (OS CSPRNG); never `0` |

All motion values are fixed-point integers. Equal states therefore produce equal
bytes (delta/XOR friendly), and resolution does not degrade far from the sector origin:

| Field | Wire | Unit | Resolution | Range |
|---|---|---|---|---|
| position (sector coordinates) | 3 x i32 | 1/32 m | 3.1 cm | +-50,000 km (v1 corruption bound) |
| orientation yaw, pitch, roll | 3 x i16 | 2*pi/65536 rad | 0.0055 deg | full circle, wraps |
| linear velocity | 3 x i32 | 1/256 m/s | 3.9 mm/s | +-1,000 km/s |
| angular velocity | 3 x i16 | 1/1024 rad/s | 0.056 deg/s | +-31 rad/s |

Values are rounded to the nearest step, so the error is at most half a step (property
test). Non-finite or out-of-range input is an error and is never clamped.

## 4. Messages

| Kind | Name | Direction | Size (bytes) |
|---:|---|---|---|
| 1 | Hello | client/authority -> relay | 67 + build + world (69-195) |
| 2 | Welcome | relay -> client | 67 |
| 3 | Ping | any -> any | 30 |
| 4 | Pong | any -> any | 38 |
| 5 | Subscribe | client -> relay | 44 + 5 per sector (44-204) |
| 6 | Snapshot | authority/client -> relay -> subscribers | 29 + 61 per record (71 with combat intent); at most 19 (16) records |
| 7 | Intent | client -> relay -> authority | 48 + params (48-560) |
| 8 | IntentResult | authority (or relay) -> client | 35 |
| 9 | Event | authority -> relay -> clients | 47 (owner, destroy), 51 (sector change) |
| 10 | Roster | relay -> all | 24 + 7 per player (24-472) |
| 11 | Bye | any -> relay | 19 |

All sizes include the 18-byte header. Body offsets follow.

**Hello**: 0 token[16], 16 nonce[16], 32 role u8 (0 player, 1 authority), 33
want_player u16 (0 = assign; the authority must send 0), 35 resume_lease u64 (0 =
fresh), 43 capabilities u32, 47 build (u8 length + 1-64 bytes `[A-Za-z0-9_.-]`), then
world (same form). The header player and lease are 0.

**Welcome**: 0 nonce[16] (echo), 16 status u8 (0 accepted, 1 refused), 17 reason u8,
18 player u16, 20 lease u64, 28 capabilities u32, 32 max_players u8, 33 relay_epoch
u64, 41 relay_time_us u64. Accepted requires reason 0 and a non-zero player and lease.
Refused requires a non-zero reason with player and lease 0. Reasons: 1 FULL, 2
BUILD_MISMATCH, 3 WORLD_MISMATCH, 4 AUTHORITY_TAKEN, 5 RESUME_REJECTED, 6 VERSION, 7
NONCE_RETIRED, 8 ADDRESS_IN_USE. `relay_epoch` is random per relay process; a change
means the relay restarted (the v1 `RESET` case).

**Ping**: 0 seq u32, 4 time_us u64 (sender clock). **Pong**: 0 seq u32, 4
echo_time_us u64, 12 time_us u64 (responder clock).

**Subscribe**: 0 revision u32 (a revision not above the current one is ignored), 4
current sector u32 (implicitly Full), 8 own_ship u64, 16 target u64, 24 flags u8 (bit
0: every other sector at Map), 25 count u8 (at most 32, no duplicates), 26 entries of
{sector u32, interest u8: 0 Full, 1 Adjacent, 2 Map}.

**Snapshot**: 0 source u16 (the publisher: the authority, or the human whose ship
this is), 2 frame u64 (the publisher's frame when the message was assembled), 10 count
u8, 11 records. The relay groups records by publisher, so the frames in one Snapshot
come from one clock.

Record (61 bytes, 71 with the combat block):

| Offset | Size | Field |
|---:|---:|---|
| 0 | 1 | length of this record (61 or 71; more in a newer minor) |
| 1 | 8 | object id |
| 9 | 4 | generation |
| 13 | 4 | age: record frame = snapshot frame - age (the relay rewrites only this field when it re-bases) |
| 17 | 2 | owner |
| 19 | 4 | sector |
| 23 | 12 | position 3 x i32 |
| 35 | 6 | orientation 3 x i16 |
| 41 | 12 | linear velocity 3 x i32 |
| 53 | 6 | angular velocity 3 x i16 |
| 59 | 1 | motion mode: 0 normal, 1 boost, 2 travel, 3 highway |
| 60 | 1 | flags: bit 0 combat block present, bit 1 ambient mass traffic |
| 61 | 8 | combat: target id (0 none) |
| 69 | 1 | combat: firing, bit n = weapon group n+1 firing |
| 70 | 1 | combat: manoeuvre (0 idle, 1 pursue, 2 attack run, 3 orbit, 4 evade, 5 retreat, others kept) |

Decoding records is zero-copy: `SnapshotView::records()` yields `RecordRef`s that
borrow the datagram, and the relay forwards `RecordRef::raw()` bytes with only the age
patched.

**Intent**: 0 client_seq u32, 4 player u16 (must equal the authenticated sender), 6
kind u16 (non-zero: 1 cargo drop, 2 cargo pickup, 3 dock, 4 undock, 5 trade, 6 fleet
order, 7 mission, 8 pilot; others reserved), 8 object u64, 16 generation u32, 20 frame
u64 (the client frame of the optimistic application), 28 params_len u16 (at most 512),
30 params (layout defined per kind, opaque to the relay).

**IntentResult**: 0 client_seq u32, 4 player u16, 6 outcome u8 (0 accepted, 1
refused, 2 uncertain), 7 reason u16 (0 if and only if accepted), 9 frame u64 (the
authority frame from which the outcome is visible in the state stream; the v1 "baseline
fence"; 0 = none). Reasons: 1 NOT_OWNER, 2 STALE_GENERATION, 3 INVALID_PARAMS, 4
OUT_OF_RANGE, 5 BUSY, 6 UNKNOWN_KIND, 7 RULES. The relay produces 0x0101
AUTHORITY_UNAVAILABLE, 0x0102 AUTHORITY_LOST and 0x0103 RELAY_BUSY.

**Event**: 0 event_seq u32 (authority-wide, monotonic, the deduplication key), 4 frame
u64, 12 kind u8, 13 object u64, 21 generation u32, 25 payload: kind 1 OwnerChange {from
u16, to u16}; kind 2 Destroy {sector u32}; kind 3 SectorChange {from u32, to u32}.

**Roster**: 0 revision u32 (incremented when membership changes), 4 flags u8 (bit 0:
authority connected), 5 count u8 (at most 64, strictly ascending player IDs), 6 entries
of {player u16, flags u8 (bit 0 connected), rtt_ms u16, jitter_ms u16; `0xFFFF` = no
sample yet}.

**Bye**: 0 reason u8 (0 leaving, 1 removed by the relay).

## 5. Relay v2 mode

Selected only by `"protocol": "v2"` in session.json (`x4lan-relay --session
session.json`). Any other session file runs the unchanged version-1 relay. The dedicated
supervisor (`x4lan-dedicated`) still runs version 1.

```json
{ "token": "<32 hex>", "build": "world1-...", "bubble": "<world id>", "port": 45841,
  "protocol": "v2",
  "v2": { "max_players": 6, "lease_timeout_s": 30, "resume_window_s": 300, "tick_hz": 60,
          "ping_interval_s": 1, "ping_timeout_s": 5, "roster_interval_s": 1,
          "authority_token": "<32 hex, optional>", "event_repeats": 3, "max_objects": 8192,
          "tier1_hz": [20, 30], "tier2_hz": [20, 30], "tier3_hz": [1, 2],
          "speed_for_max_rate": 300, "weapons_range_m": 6000, "refresh_hz": 1,
          "map_refresh_s": 30, "max_records_per_tick": 256,
          "disable": { "<feature>": "<reason>" } } }
```

**Admission.** This reuses the v1 ideas: a shared token, a client nonce, a relay-issued
random lease bound to the source address, a 30-second lease, resume by lease, a ring
of the last 64 retired nonces, and one identity per endpoint.

* Wrong token: no reply. A wrong build or world gets an explicit refusal.
* Players get the lowest free ID in `1..=max_players` (default **6**, at most 64).
  A full session refuses with `FULL`.
* A lost Welcome (same nonce, same address) is answered again with the same identity.
* Resume: `want_player` plus `resume_lease`. This works from a new address and nonce
  while the player is live (NAT rebinding) or within `resume_window_s` after its lease
  expired. Its ID stays reserved for that window, and its intent history is kept.
* `Bye` frees the ID immediately and is not resumable.
* One authority (player `0x7FFF`). It may use a separate `authority_token`. It can
  resume by lease, but **a fresh authority is refused once one has been admitted**: a
  new world needs a new relay (the v1 "fresh host requires a fresh relay" rule).

**State routing.**

* Authority Snapshots update the relay's object table (latest record per object,
  indexed by sector). Stale generations, older frames from the same publisher, and
  records of objects destroyed by an Event (a tombstone keeps the generation) are
  dropped.
* A client Snapshot may carry only objects the authority has assigned to that player
  (owner = sender, same generation, not ambient). Accepted records are forwarded
  immediately to the authority, and the relay distributes them.
* While the owning client has published its ship within 0.75 s, the authority's copy of
  that ship's motion is not distributed ("own ship stays local"). After that, the
  authority's copy takes over.
* On every tick (default 60 Hz), the scheduler (section 6) decides per subscriber which
  records are due. The relay packs them per publisher into Snapshots of at most 1200
  bytes.

**Intents.**

* The `player` field must equal the authenticated sender; otherwise the intent is
  dropped and counted.
* New intents are forwarded to the authority. A pending intent is re-forwarded every
  250 ms (the v1 `CARGOEXEC` retry), so the authority must deduplicate on
  `(player, client_seq)`.
* A duplicate of a completed intent is answered from the relay cache (the first
  result wins; 256 per player).
* Without an authority the relay refuses with `AUTHORITY_UNAVAILABLE`. More than 32
  pending intents per player get `RELAY_BUSY`.
* When the authority leaves or expires, every pending intent becomes **Uncertain
  `AUTHORITY_LOST`** and is never replayed into another authority (the v1 UNCERTAIN
  rule).

**Events.**

* Events are deduplicated on `event_seq` over a window of the last 1024, so the
  authority may repeat them.
* The relay applies each event to its table: an owner change patches the record and
  can hand motion authority back to the authority, a destroy removes the object and
  leaves a tombstone, and a sector change re-indexes it.
* Recipients are the connected players that subscribe to an affected sector at any
  interest level, plus the old and new owner.
* Each event is sent 3 times, 50 ms apart; clients deduplicate.

**Ping/RTT.**

* The relay pings every peer every second and answers peer Pings with Pong.
* Statistics per peer are integer-only and deterministic: last, min, max, RFC 6298
  smoothed RTT and RTT variation, a p95 over the last 64 samples, the p95 change
  between consecutive samples, and lost pings (unanswered after 5 s).
* `RttStats::interpolation_delay_us` gives the proposal's adaptive buffer delay:
  send interval + 2 x p95 jitter.
* The Roster carries the smoothed RTT and variation in milliseconds.

**Feature flags** (development rule: on by default, disabled only with a reason).

* `rtt_stats`, `roster`, `event_repeat` and `intent_cache` are relay features;
  `speed_scaling` is a scheduler feature.
* At startup the relay prints one line:
  `FEATURES x4lan-relay protocol=v2 rtt_stats=on roster=on event_repeat=on intent_cache=on speed_scaling=on`.
* Each disabled feature adds `FEATURE <name> DISABLED reason=<reason>`. A disable
  without a reason string, or with an unknown feature name, is a configuration error.

## 6. Relevance tiers

The scheduler (`x4mp_proto::sched`) is pure and deterministic. It has no clock or
randomness and iterates maps in order. Output is in priority order (tier, then least
recently sent, then object ID), and identical inputs give identical output regardless
of their order.

| Tier | Objects (for one subscriber) | Rate (default, configurable) |
|---|---|---|
| 1 Player | ships published by another player's client, in your current sector | 20-30 Hz |
| 2 Combat | your target; anything whose combat target is your ship; objects within `weapons_range_m` (6 km) of your ship in your sector | 20-30 Hz |
| 3 Sector | other objects in your sector; everything in Adjacent sectors | 1-2 Hz |
| 4 Map | objects in Map sectors (listed, or all others with `all_map`) | on change only (owner, sector, generation, combat start/stop); optional `map_refresh_s` |
| 5 Ambient | records flagged ambient mass traffic | never |
| - | your own ship (by `own_ship` or because you published it); unsubscribed sectors | never |

Rules:

* **Speed scaling:** rate = min + (max - min) x clamp(speed / 300 m/s, 0, 1), so highway
  and travel speeds automatically use the band maximum. When `speed_scaling` is off,
  every tier uses its maximum.
* **Promotion is immediate.** When an object moves to a more relevant tier (it becomes
  your target, or starts attacking you), it is sent on the next tick. A demoted object
  waits for the slower interval.
* **Unchanged state is not resent at the tier rate.** It is refreshed at `refresh_hz`
  (1 Hz) to repair loss.
* An object that leaves a subscriber's view is forgotten, so it is sent at once when it
  comes back. At most `max_records_per_tick` (256) records go to one subscriber per
  tick; the rest stay due.
* `tolerance_us` (2 ms) lets an object be sent slightly early so tick jitter does not
  halve a rate.
* Effective rates are whole multiples of the relay tick. At 60 Hz, 30 Hz and 20 Hz are
  exact, while 25 Hz becomes 20 Hz (every 3rd tick).

Measured in `tests/scheduler.rs` (10 s, 60 Hz ticks) and repeated over real UDP in
`x4lan-relay/tests/v2_relay.rs` (2 s):

| Object | Sends in 10 s |
|---|---|
| tier 1 at 400 m/s | 300 (30 Hz) |
| tier 1 at rest | 200 (20 Hz) |
| attacker at 600 m/s | 300 |
| tier 3 at rest / at 300 m/s | 10 / 20 |
| map object, 2 changes | 3 |
| ambient, unsubscribed | 0 |
| tier 1 never updated | 10 (refresh) |

## 7. Migration from version 1

| Version 1 | Version 2 |
|---|---|
| `HELLO`/`WELCOME`/`RESUME`/`RESET`, slots `p1`/`p2`/`p3` | Hello/Welcome with role and a relay-assigned `PlayerId`; resume = `want_player` + `resume_lease`; `relay_epoch` change = `RESET`; explicit refusal reasons |
| `PING`/`PONG` (token readiness check) | authenticated Ping/Pong both ways; RTT statistics; Roster |
| `STATE`/`PEER`/`EMPTY` | client Snapshot of the own ship, distributed as tier 1 |
| `NAV2` ship ID, authority epoch, sequence, sector macro, pose, source time, linear/angular velocity | Snapshot record: object ID + generation + owner (the epoch's job), publisher frame (replaces sequence + source time), `SectorId`, quantised pose and both velocities, motion mode (`NMODE`) |
| `NAV2` dock ID | dock/undock Intents; the docked state itself is still open (see 9) |
| `NAV2` shot counter, `FIRE2` group + per-trigger counters | combat block: target, firing bitmask per weapon group, manoeuvre. This replicates intent, not shot counts, so a lost packet delays fire instead of losing it (proposal section 6) |
| `UMOVE` host epoch, object, sequence, sector, source time, pose, velocities | authority Snapshot record; host epoch = `relay_epoch` + generation; the relevance tiers replace the 32-records-per-tick budget |
| `UINTEREST` | Subscribe (sector set + interest, own ship, target) |
| `UBEGIN`/`UOBJECT`/`UCOMMIT`/`UACK` baselines, per-sector cache (8 sectors, 4096 objects) | first send on subscription + map tier + refresh; relay table up to `max_objects` (8192) across all sectors. There is **no atomic commit/ack** in 2.0 (see 9) |
| `HEALTH` (victim-authoritative hull) | not in 2.0; planned as a victim-sent message under the same owner rules |
| `CARGOREQ` lease, request ID, host epoch, ship, pilot epoch, DROP/PICKUP, drop ID, ware, amount | Intent: client_seq = request ID, kind CARGO_DROP/CARGO_PICKUP, object + generation = ship + pilot epoch, params = drop ID/ware/amount |
| `CARGOEXEC` 250 ms retry; `CARGODONE`/`CARGOREPLY` with baseline fence; UNCERTAIN on host loss | relay re-forward every 250 ms; IntentResult with the `frame` fence; Uncertain `AUTHORITY_LOST` |
| `COOPORDER`/`COOP_EXEC` | Intent TRADE |
| `CREW`, pilot epochs, handoff | Owner field + OwnerChange event; Intent PILOT |
| `IDENT`, `QUEST`, `MSTATE`, `ECON` | not in 2.0 |

A game-side port can run both protocols side by side: the relay mode is selected per
session, and the two datagram families cannot be confused.

## 8. Tests

* `x4mp-proto/tests/roundtrip.rs`: 3000 random messages per kind round-trip and
  re-encode canonically. Snapshots split greedily within 1200 bytes, relay re-basing
  keeps frames, quantisation error stays within half a step (20k samples), the
  documented sizes hold, and the versioning rules are checked.
* `x4mp-proto/tests/fuzz.rs`: a deterministic mutation fuzzer (bit flips, boundary
  bytes, truncation, extension, shifted ranges, length fix-up). It checks 200k mutated
  and 100k random datagrams for no panic and canonical re-encoding, every truncation
  of 500 valid datagrams, and bad length/count fields.
* `x4mp-proto/tests/scheduler.rs`: the tier table, rate bands, exact send counts,
  promotion and demotion, budget order, determinism under input reordering, and
  configuration validation.
* `x4lan-relay/tests/v2_relay.rs`: the relay on real loopback UDP sockets, stepped
  with a simulated 60 Hz clock, with one authority and 5-7 synthetic clients. It
  covers admission (sixth player refused when `max_players` = 5, a wrong token gets
  no reply, a freed ID is reused, an expired ID stays reserved, resume, a wrong resume
  lease), the tier rates per subscriber, ambient and own-ship exclusion, map tier
  once, client-only publishing of assigned objects, intent forwarding, caching, retry,
  spoof rejection, Uncertain on authority loss, Events with dedup, repeats and
  tombstones, RTT (exactly one step) in the Roster, Bye, lease expiry, refusal of a
  fresh authority, and six players at the default limit.

These are synthetic peers. They test the relay's logic, not X4.

## 9. Open design questions

1. **Sector and faction tables.** `SectorId` and `FactionId` are session indices. Who
   publishes the macro-to-index tables (an authority message at admission, or a hash
   of the macro name), and how do late joiners get them?
2. **Reliability of events and map state.** Events are repeated, not acknowledged, and
   map-tier objects are sent once. A client that loses all copies keeps stale state
   until something changes. Options: an ack/resync message, a periodic
   `map_refresh_s` (available, off by default), or a baseline/commit like `UCOMMIT`.
3. **Atomic sector baselines.** v1 never applies a partial baseline. v2 converges
   record by record. Does replica creation or removal in the game need a "sector
   complete" marker before it deletes local NPCs?
4. **Plausibility checks on client records** (proposal section 3: range, rate of fire,
   target alive) are not in the relay. Should they live in the relay or the authority?
5. **Authority identity.** A shared or separate token over a trusted LAN/ZeroTier is
   v1-equivalent. There is no encryption, and the 64-bit lease is bound to the
   address. Is that enough for Internet play through the VPS?
6. **Frame clocks.** Client and authority frames are separate. Should Welcome or
   Ping carry the authority frame so clients can map frames to server time for
   interpolation?
7. **Bandwidth.** A record is 61-71 bytes. Smallest-three quaternions, i16 velocities
   and deltas against acknowledged state could roughly halve it. This needs the real
   RTT and jitter measurements from proposal section 10 first.
8. **Tick quantisation.** A 60 Hz relay tick turns 25 Hz into 20 Hz. Accumulate
   fractional intervals, or tick at 120 Hz?
9. **Version refusal.** Other majors are dropped silently. Should a Welcome `VERSION`
   reply be sent, with an anti-amplification size check?
10. **Ambient traffic.** Can mass-traffic objects be shot or otherwise interacted with
    (proposal section 4)? If so, an interaction must promote the object, and the
    ambient flag alone is not enough.
11. **Health/hits, docking state, identity and missions** (`HEALTH`, `IDENT`,
    `QUEST`) have no v2 message yet.
12. **Dedicated supervisor.** `x4lan-dedicated` still runs v1. Switching it is part of
    the game integration.
