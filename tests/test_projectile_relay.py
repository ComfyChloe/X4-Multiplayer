"""BOLT/BEND relay (x4lan/projectile_relay.py via CoopSession): authority, loss, reorder, pause, caps."""
import unittest
from x4lan import projectile_protocol as pp
from x4lan import projectile_relay as pr
from x4lan.coop import CoopSession
from test_host import TOKEN, P1, P2, FOREIGN, hello

WORLD = [100, 100, 0, 64.5, 30, 0, 100, 100, 100, 100, 200, 1, 1, 0, 0, 0, 0]
SECTOR = 'cluster_14_sector001_macro'


class ProjectileRelayTests(unittest.TestCase):
    def setUp(self):
        self.s = CoopSession(TOKEN, 'test', 'bubble')
        self.a = self.s.receive(hello(), P1, 0)[0].decode().split('|')[3]
        self.b = self.s.receive(hello('p2', '2' * 32), P2, 0)[0].decode().split('|')[3]
        self.s.receive(f'WORLD|1|{self.a}|1|{"|".join(map(str, WORLD))}'.encode(), P1, .05)
        self.s.receive(f'CREW|1|{self.a}|1|1|1'.encode(), P1, .08)      # host pilots ship 1
        self.s.receive(f'CREW|1|{self.b}|1|2|2'.encode(), P2, .1)       # guest pilots ship 2
        self.s.snapshots(.12)
        self.s.receive(f'HACK|1|{self.b}|2|{self.s.health_gen[2]}'.encode(), P2, .13)
        self.seq = 0
        self.nav(now=.2)

    def nav(self, entity=2, time=1000.0, now=.2, sector=SECTOR, kind='NAV2'):
        """The pilot's NAV2 at relay time `now` carries source time `time` (a different clock)."""
        self.seq += 1
        lease, address = (self.b, P2) if entity == 2 else (self.a, P1)
        tail = f'|{time}|0|0|0|0|0|0' if kind == 'NAV2' else ''
        self.s.receive(f'{kind}|1|{lease}|{entity}|{self.s.epochs[entity]}|{self.seq}|{sector}|1000|0|0|0|0|0|0|{self.seq}{tail}'.encode(),
                       address, now)

    def bolt(self, id=1, time=1000.05, entity=2, now=.21, address=None, lease=None, ttl=.7, **kw):
        lease = lease or (self.b if entity == 2 else self.a)
        fields = dict(direction='client', lease=lease, source_lease=None, entity=entity, epoch=self.s.epochs[entity],
                      gen=self.s.health_gen[entity], id=id, time=time, sector=SECTOR, kind=1, x=1030.0, y=-10.0, z=160.0,
                      vx=0.0, vy=0.0, vz=2300.0, yaw=0.0, pitch=0.0, roll=0.0, ttl=ttl)
        fields.update(kw)
        return self.s.receive(pp.serialize(pp.Bolt(**fields)), address or (P2 if entity == 2 else P1), now)

    def end(self, id=1, time=1000.1, entity=2, now=.22, **kw):
        lease = self.b if entity == 2 else self.a
        fields = dict(direction='client', lease=lease, source_lease=None, entity=entity, epoch=self.s.epochs[entity],
                      gen=self.s.health_gen[entity], id=id, time=time)
        fields.update(kw)
        return self.s.receive(pp.serialize(pp.End(**fields)), P2 if entity == 2 else P1, now)

    def out(self, now, to=P1):
        """Forwarded BOLT/BEND to `to` at relay time `now`, decoded in the forwarded form."""
        return [pp.parse(p, 'forwarded') for a, p in self.s.snapshots(now) if a == to and p[:4] in (b'BOLT', b'BEND')]

    def test_a_guest_birth_reaches_only_the_host_at_most_three_times_gap_apart(self):
        self.bolt()
        first = self.out(.25)
        self.assertEqual([(m.lease, m.source_lease, m.id, m.x) for m in first], [(self.a, self.b, 1, 1030.0)])
        self.assertEqual(self.out(.26), [])                                   # GAP
        self.assertEqual(len(self.out(.30)) + len(self.out(.35)) + len(self.out(.40)), 2)
        self.assertEqual(self.out(.45, to=P2), [])                            # never back to the pilot

    def test_the_host_needs_its_current_generation_and_a_guest_also_its_live_hack(self):
        self.nav(entity=1, now=.2); self.bolt(entity=1, now=.21)
        self.assertEqual([m.source_lease for m in self.out(.25, to=P2)], [self.a])
        self.s.health_live[2] = 0
        self.bolt(id=2, now=.26)
        self.assertEqual(self.out(.3), [])

    def test_every_authority_and_stream_check_refuses(self):
        cases = [dict(address=FOREIGN), dict(lease='f' * 32), dict(entity=1, address=P2, lease=None), dict(epoch=9), dict(gen=99),
                 dict(sector='cluster_01_sector001_macro'), dict(time=1000.0 + .02 + .2), dict(time=1000.02 - 1.1),
                 dict(time=999.2, ttl=.7)]
        for i, kw in enumerate(cases):
            if kw.get('entity') == 1: kw['lease'] = self.b
            self.bolt(id=10 + i, **kw)
        self.assertEqual(self.out(.25), [])
        self.bolt(id=30, now=.96)                                             # NAV2 at .2 is stale at .96
        self.assertEqual(self.out(.97), [])

    def test_no_world_dead_ship_nav1_or_a_former_pilot_never_forward(self):
        for setup in ('self.s.world = None', 'self.s.world = self.s.world[:3] + (1.0,) + self.s.world[4:]', "self.nav(kind='NAV', now=.205)",
                      "self.s.receive(f'CREW|1|{self.b}|2|0|0'.encode(), P2, .205)"):
            with self.subTest(setup=setup):
                self.setUp(); exec(setup)
                self.bolt(); self.assertEqual(self.out(.25), [])

    def test_source_time_is_judged_against_last_nav2_plus_elapsed_relay_time(self):
        # NAV2 at relay .2 says source 1000.0; at relay .65 the pilot's source clock is about 1000.45.
        self.bolt(id=1, time=1000.5, now=.65); self.bolt(id=2, time=1000.5, now=.3)
        self.assertEqual([m.id for m in self.out(.66)], [1])

    def test_duplicates_never_extend_and_a_changed_duplicate_is_refused(self):
        self.bolt(); self.bolt(x=5.0); self.out(.25); self.bolt(now=.27)
        sent = self.out(.30) + self.out(.35) + self.out(.40)
        self.assertEqual([m.x for m in sent], [1030.0, 1030.0])
        self.nav(time=1000.8, now=.84)                                        # source past 1000.05 + .7
        self.bolt(now=.85, time=1000.05)
        self.assertEqual(self.out(.86), [])                                   # a late repeat never resurrects

    def test_end_follows_its_birth_and_an_early_end_leaves_a_tombstone(self):
        self.bolt(id=1); self.end(id=1, time=1000.1)
        sent = [type(m).__name__ for m in self.out(.25) + self.out(.30) + self.out(.35) + self.out(.40) + self.out(.45)]
        self.assertEqual(sent, ['Bolt', 'End', 'End', 'End'])
        self.end(id=2, time=1000.2, now=.46); self.bolt(id=2, time=1000.21, now=.461)  # end before birth
        self.bolt(id=3, time=1000.3, now=.462); self.end(id=3, time=1000.25, now=.47)  # end before its birth time
        self.end(id=3, time=1000.32, now=.48); self.end(id=3, time=1000.34, now=.49)   # first end wins
        sent = self.out(.5) + self.out(.55) + self.out(.6) + self.out(.65) + self.out(.7)
        self.assertEqual([(type(m).__name__, m.id, m.time) for m in sent],
                         [('Bolt', 3, 1000.3)] + [('End', 3, 1000.32)] * 3)

    def test_reorder_window_and_bounded_forward_jumps(self):
        self.bolt(id=600); self.bolt(id=300, now=.211); self.bolt(id=88, now=.212)     # 88 <= 600 - 512
        self.bolt(id=1113, now=.213); self.bolt(id=700, now=.214)                       # 1113 > 600 + 512
        self.assertEqual(sorted(m.id for m in self.out(.25)), [300, 600, 700])
        self.bolt(id=1212, now=.26); self.bolt(id=650, now=.261)                        # 1212 evicts <= 700
        self.assertEqual([m.id for m in self.out(.3)], [1212])                       # 300/600/700 evicted, 650 refused

    def test_a_handover_retires_everything_and_the_new_generation_starts_fresh(self):
        self.bolt(id=1); self.out(.25)
        self.s.receive(f'CREW|1|{self.b}|2|0|0'.encode(), P2, .26)              # the guest leaves its ship
        self.assertEqual(self.out(.3), [])
        self.s.receive(f'CREW|1|{self.b}|3|2|2'.encode(), P2, .31)
        self.s.snapshots(.32); self.s.receive(f'HACK|1|{self.b}|2|{self.s.health_gen[2]}'.encode(), P2, .33)
        self.nav(time=1000.4, now=.34); self.bolt(id=1, time=1000.38, now=.35)
        self.assertEqual([(m.id, m.gen) for m in self.out(.36)], [(1, self.s.health_gen[2])])

    def test_pause_or_stale_nav_retires_and_a_resumed_stream_cannot_resurrect(self):
        # The host's ship: its generation stays current without HEALTH frames over these seconds.
        self.nav(entity=1, now=.2); self.bolt(entity=1, id=1, ttl=3.0); self.out(.25, to=P2)
        self.assertEqual(self.out(1.0, to=P2), [])                            # NAV2 stale: retired
        self.nav(entity=1, time=1000.3, now=1.1)                              # resumed, same key
        self.bolt(entity=1, id=1, ttl=3.0, now=1.11); self.bolt(entity=1, id=2, time=1000.3, now=1.12)
        self.assertEqual([m.id for m in self.out(1.2, to=P2)], [2])

    def test_birth_rate_live_records_and_per_tick_forwards_are_capped(self):
        self.nav(entity=1, now=.2)
        for i in range(pr.RATE + 1): self.bolt(entity=1, id=i + 1, ttl=3.0, now=.21 + i * .001)
        counts = [len(self.out(t, to=P2)) for t in (.3, .35, .4, .45, .5, .55)]
        self.assertEqual((counts[0], sum(counts)), (pr.PER_TICK, pr.RATE * pr.REPEATS))
        for t in (.7, 1.2): self.nav(entity=1, time=1000 + t - .2, now=t)
        for i in range(pr.RATE + 1): self.bolt(entity=1, id=100 + i, ttl=3.0, time=1001.0, now=1.21 + i * .001)
        for t in (1.7, 2.2): self.nav(entity=1, time=1000 + t - .2, now=t)
        self.bolt(entity=1, id=300, ttl=3.0, time=1002.0, now=2.21)           # 96 live: the cap refuses it
        self.assertEqual(sum(len(self.out(t, to=P2)) for t in (2.25, 2.3, 2.35, 2.4, 2.45, 2.5)),
                         (pr.LIVE - pr.RATE) * pr.REPEATS)

    def test_original_ttl_retires_records_that_still_had_repeats(self):
        self.bolt(id=1, ttl=.1); self.out(.25)
        self.nav(time=1000.2, now=.4)
        self.assertEqual(self.out(.41), [])

    def test_shot_and_fire_paths_are_unchanged_by_projectile_packets(self):
        self.bolt(); before = dict(self.s.shots), dict(self.s.fire2)
        self.assertEqual((self.s.shots, self.s.fire2), before)
        self.assertFalse(any(p.startswith((b'SHOT', b'FIRE2')) for _, p in self.s.snapshots(.25)))


if __name__ == '__main__':
    unittest.main()
