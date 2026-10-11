# Historical local weapons and cargo test

X4 9.00 (611726), Steam build 23660954, one native Linux game in a fresh creative
sandbox, isolated profile 45841. A synthetic network peer supplied the proxy;
Windows and a second real game were not part of this run.

| Measurement | P1 controlled ship | P2 proxy |
| --- | ---: | ---: |
| Native shot events in about 5 seconds | 30 | 30 |
| Target attack/hit events | 5 | 5 |
| Target shield before | 8,840 | 5,400 |
| Target shield after | 7,796 | 4,320 |
| Target hull before/after | 6,000 / 6,000 | 6,000 / 6,000 |
| Initial energy-cell cargo | 0 | 0 |
| Container pickup | 20 | 20 |
| Dock permission | Granted | Granted |
| Station cargo transfer | 30 | 30 |
| Final energy-cell cargo | 50 | 50 |

Station stock changed 200 → 170 → 140; the donor's stock changed 40 → 0.
Both execute_trade calls succeeded. Native collect_drop, event_object_collected_ware,
shoot_at/shoot, event_weapon_fired and event_object_attacked supported the measurements.
Five attack events are not a reliable count of individual projectile collisions.
Different automatic target loadouts prevent a weapon-damage comparison. Shields
were not depleted, so hull damage and destruction were not demonstrated.

The fixture suspended network position writes while the local MD/AI test controlled
the proxy. Both ships had two weapons and capacity for 540 energy cells. Docking
permission was real, but dock shortened the flight by moving the ships. The offers
used real cargo and virtual money: quantities were checked; credit charging was not.
No cargo was directly added to the two player ships by this fixture. The cargo magnet,
manual docking approach and two-world combat/trade synchronisation were not tested.

Separate freshly created offers were needed for the two transfers. The controlled
player required target-based firing in a visible AI context. Delayed MD operations
needed ordered action blocks. The accepted run followed fixes to those issues;
failed exploratory runs are not counted. 24 automated tests passed. XML parsed and
executed in X4; a long external full-schema compilation was stopped, not passed.

Source-only reproduction: enable `mode="proxy"` and `interaction_test=true` in a
fresh disposable profile, never an existing main game. The legacy fixture moves the
player automatically; do not fly/trade during it. It is excluded from user packages.
At 118.25 game seconds the control readback still showed 50 / 50 / 140 energy cells.

All seven original save files retained their SHA256, size and modification time.
Steam cloud metadata is separate. No test save was created. Test processes stopped
and the installed development extension was archived out of the game directory.
[Results](reports/linux-interactions-results.json), [log](reports/private-runtime-log-withheld.txt).
