"""Opt-in UATTACH relay inside UniverseSession: authentication, view, acknowledgement and bounds."""
import unittest
from unittest.mock import patch

from x4lan.attachment import decode
from x4lan.attachment_relay import ATTACH_BUDGET, ATTACH_TTL, AttachmentRelay
import x4lan.attachment_relay as relay_module
from x4lan.universe import Baseline, Receiver, UniverseSession
from test_host import TOKEN, P1, P2, FOREIGN

SECTOR = 'cluster_14_sector001_macro'
OTHER = 'cluster_01_sector001_macro'


def row(i, kind='ship', macro='ship_arg_s_scout_01_a_macro', owner='argon'):
    return f'{i},{kind},{macro},{owner},{i},2,3,0,.1,-.1,' + ('none,0' if kind == 'ship' else 'energycells,20')


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.s = UniverseSession(TOKEN, 'world1-test', 'test', attachments=True)
        self.a = self.join('p1', P1, '1' * 32)
        self.b = self.join('p2', P2, '2' * 32)
        self.guest = Receiver()
        self.interest = 0

    def join(self, slot, address, nonce):
        packet = f'HELLO|1|{TOKEN}|world1-test|{SECTOR}|test|{slot}|{nonce}'.encode()
        return self.s.receive(packet, address, 0)[0].decode().split('|')[3]

    def publish(self, baseline, now):
        for i in range(len(baseline.rows) + 2):
            self.s.receive(baseline.packet(self.a, i), P1, now)

    def look(self, sector, now):
        self.interest += 1
        self.s.receive(f'UINTEREST|1|{self.b}|{self.interest}|{sector}'.encode(), P2, now)

    def acknowledge(self, now, ticks=20, interval=.05):
        """Drive the existing W01 delivery to the guest until it acknowledges a baseline."""
        for i in range(ticks):
            for address, packet in self.s.snapshots(now):
                if address == P2 and packet.startswith((b'UBEGIN|', b'UOBJECT|', b'UCOMMIT|')):
                    f = packet.decode().split('|')
                    ack, _ = self.guest.receive(f, now)
                    if ack is not None:
                        self.s.receive(f'UACK|1|{self.b}|{f[3]}|{f[4]}|{ack}'.encode(), P2, now)
            if self.s.delivery and self.s.delivery.done:
                return
            now += interval

    def observe(self, seq, ship, revision, dock=7, state='EXTERNAL', lease=None, epoch=None, address=P1, now=1.0):
        lease = lease or self.a
        text = f'UATTACH|1|{lease}|{epoch or self.a}|{revision}|{seq}|{ship}|{dock if state != "FREE" else 0}|{state}'
        self.assertEqual(self.s.receive(text.encode(), address, now), [], 'the relay never replies')

    def delivered(self, now):
        return [decode(p.decode()) for a, p in self.s.snapshots(now) if a == P2 and p.startswith(b'UATTACH|')]

    def ready(self, rows, revision=1, sector=SECTOR, now=.5):
        b = Baseline.create(self.a, revision, sector, rows)
        if not self.s.interest or self.s.interest[2] != sector:
            self.look(sector, now)                      # a changed view waits for a fresh capture
        self.publish(b, now)
        self.acknowledge(now)
        self.assertEqual(self.s.delivered, revision)
        return b

    def test_disabled_by_default_and_never_establishes_an_epoch(self):
        plain = UniverseSession(TOKEN, 'world1-test', 'test')
        self.assertIsNone(plain.attachments)
        self.observe(1, 1, 1)                                   # no baseline yet: no universe epoch
        self.assertEqual(self.s.attachments.marks, {})
        self.assertIsNone(self.s.universe_epoch)

    def test_only_the_authenticated_host_epoch_and_endpoint_publish(self):
        self.ready([row(1)])
        self.observe(1, 1, 1, address=FOREIGN)                  # right lease, wrong endpoint
        self.observe(1, 1, 1, lease=self.b, address=P2)         # guest forging host state
        self.observe(1, 1, 1, epoch='c' * 32)                   # wrong epoch
        self.assertEqual(self.s.receive(b'UATTACH|1|' + b'x' * 800, P1, 1), [])
        self.assertEqual(self.s.receive(b'UATTACH|1|garbage', P1, 1), [])
        self.assertEqual(self.s.attachments.live, {})
        self.observe(1, 1, 1)
        self.assertEqual([r.ship for r in self.delivered(1.0)], [1])

    def test_delivery_substitutes_only_the_recipient(self):
        self.ready([row(1)])
        self.observe(5, 1, 1, dock=9, state='STORED')
        [r] = self.delivered(1.0)
        self.assertEqual((r.recipient, r.epoch, r.revision, r.sequence, r.ship, r.dock, r.state),
                         (self.b, self.a, 1, 5, 1, 9, 'STORED'))

    def test_only_saved_ships_of_the_committed_current_view_baseline(self):
        self.ready([row(1), row(2, kind='drop'), row(250000)])
        self.observe(1, 2, 1)                                   # a drop row is never an attachment
        self.observe(2, 3, 1)                                   # not in the baseline
        self.observe(3, 1, 2)                                   # future revision
        self.observe(4, 1, 0)                                   # invalid revision (decode fails)
        self.assertEqual(self.s.attachments.live, {})
        self.ready([row(1)], revision=2, sector=OTHER)
        self.observe(5, 1, 1)                                   # obsolete revision of another view
        self.assertEqual(self.s.attachments.live, {})

    def test_replay_never_refreshes_and_expired_records_are_never_revived(self):
        self.ready([row(1)])
        self.observe(1, 1, 1, now=1.0)
        self.assertEqual(len(self.delivered(1.0)), 1)
        self.observe(1, 1, 1, now=2.5)                          # same sequence later: no refresh
        self.observe(1, 1, 1, state='STORED', now=2.6)          # conflicting replay: ignored
        self.assertEqual(self.delivered(1.0 + ATTACH_TTL + .01), [])
        self.observe(1, 1, 1, now=4.0)                          # an old sequence cannot revive it
        self.assertEqual(self.delivered(4.0), [])
        self.observe(2, 1, 1, now=4.0)                          # a NEW observation can
        self.assertEqual([r.sequence for r in self.delivered(4.0)], [2])

    def test_delivery_requires_the_acknowledged_baseline_not_an_in_flight_one(self):
        self.ready([row(1)])
        self.observe(1, 1, 1)
        self.publish(Baseline.create(self.a, 2, SECTOR, [row(1)]), 1.0)   # newer, not yet acknowledged
        self.s.snapshots(1.0)
        self.assertFalse(self.s.delivery.done)
        self.observe(2, 1, 2)                                   # admitted at the committed revision 2...
        self.assertEqual([r.revision for r in self.delivered(1.3)], [])   # ...but not delivered before the ack
        self.acknowledge(1.4)
        self.assertEqual(self.s.delivered, 2)
        self.assertEqual([r.revision for r in self.delivered(1.6)], [2])

    def test_descriptor_change_or_removal_needs_fresh_observations(self):
        self.ready([row(1), row(2)])
        self.observe(1, 1, 1); self.observe(2, 2, 1)
        self.assertEqual(sorted(r.ship for r in self.delivered(1.0)), [1, 2])
        self.ready([row(1, macro='ship_arg_m_trans_01_a_macro')], revision=2, now=1.1)   # ship 1 changed, 2 removed
        self.assertEqual(self.delivered(1.5), [], 'revision-1 records stop once revision 2 is acknowledged')
        self.observe(3, 2, 2, now=1.5)                          # removed ship: rejected
        self.observe(4, 1, 2, now=1.5)                          # fresh observation at the new revision
        self.assertEqual([(r.ship, r.revision) for r in self.delivered(1.5)], [(1, 2)])

    def test_sector_departure_and_return_do_not_leak_old_state(self):
        self.ready([row(1)])
        self.observe(1, 1, 1)
        self.look(OTHER, 1.1)
        self.assertEqual(self.delivered(1.1), [])
        self.assertEqual(self.s.attachments.live, {}, 'view change clears live records')
        self.ready([row(1)], revision=3, sector=SECTOR, now=1.2)
        self.observe(1, 1, 3, now=1.3)                          # stale sequence: marks survive the view change
        self.assertEqual(self.delivered(1.3), [])
        self.observe(2, 1, 3, now=1.3)
        self.assertEqual([r.sequence for r in self.delivered(1.3)], [2])

    def test_guest_reconnect_starts_clean(self):
        self.ready([row(1)])
        self.observe(1, 1, 1)
        del self.s.peers['p2']
        self.b = self.join('p2', P2, '3' * 32)                  # new guest lease
        self.guest = Receiver()
        self.assertEqual(self.delivered(1.0), [])
        self.assertEqual(self.s.attachments.live, {})

    def test_host_loss_stops_output(self):
        self.ready([row(1)])
        self.observe(1, 1, 1)
        del self.s.peers['p1']
        self.assertEqual(self.delivered(1.0), [])
        self.assertEqual((self.s.attachments.live, self.s.attachments.marks), ({}, {}))

    def test_fair_bounded_transmission_budget(self):
        count=ATTACH_BUDGET+8
        self.ready([row(i) for i in range(1, count+1)])
        for i in range(1, count+1):
            self.observe(i, i, 1)
        first = {r.ship for r in self.delivered(1.0)}
        second = {r.ship for r in self.delivered(1.05)}
        self.assertEqual(len(first), ATTACH_BUDGET)
        self.assertEqual(first | second, set(range(1, count+1)), 'every ship is served')
        self.assertFalse(first & second, 'no ship is resent before the retry interval')
        self.assertEqual(self.delivered(1.1), [])

    def test_fresh_observations_and_retry_filtering_cannot_starve_other_ships(self):
        # Keep a small budget here: this regression detects fairness independently
        # of the production budget increase. Source observers refresh every .5s.
        self.s.attachments.budget=32
        self.look(SECTOR,.5)
        self.publish(Baseline.create(self.a,1,SECTOR,[row(i) for i in range(1,513)]),.5)
        self.acknowledge(.5,ticks=600,interval=0)
        self.assertEqual(self.s.delivered,1)
        first={};last={};gaps=[]
        for step in range(61):
            now=1+step*.05
            if step%10==0:
                for ship in range(1,513):self.observe(step+1,ship,1,now=now)
            records=self.delivered(now)
            self.assertLessEqual(len(records),32)
            for record in records:
                first.setdefault(record.ship,now)
                if record.ship in last:gaps.append(now-last[record.ship])
                last[record.ship]=now
        self.assertEqual(len(first),512)
        self.assertLessEqual(max(first.values())-1,.8)
        self.assertLessEqual(max(gaps),.8+1e-9)

    def test_full_admitted_view_receives_each_fresh_observation_within_ttl(self):
        from x4lan.host import TICK_HZ
        count=4096
        self.look(SECTOR,.5)
        self.publish(Baseline.create(self.a,1,SECTOR,[row(i) for i in range(1,count+1)]),.5)
        # Drive the public baseline handshake at a fixed clock, independently of
        # the later real 20Hz attachment schedule.
        self.acknowledge(.5,ticks=600,interval=0)
        self.assertEqual(self.s.delivered,1)
        first={};last={};gaps=[]
        for step in range(TICK_HZ*4+1):
            now=1+step/TICK_HZ
            if step%(TICK_HZ//2)==0:
                for ship in range(1,count+1):self.observe(step+1,ship,1,now=now)
            records=self.delivered(now)
            self.assertLessEqual(len(records),ATTACH_BUDGET)
            for record in records:
                first.setdefault(record.ship,now)
                if record.ship in last:gaps.append(now-last[record.ship])
                last[record.ship]=now
        self.assertEqual(len(first),count)
        self.assertLess(max(first.values())-1,ATTACH_TTL)
        self.assertLess(max(gaps),ATTACH_TTL)

    def test_constructor_validates_its_limits(self):
        for kwargs in (dict(ttl=0), dict(ttl=float('inf')), dict(retry=-1), dict(budget=0), dict(budget=True)):
            with self.subTest(kwargs), self.assertRaises(ValueError):
                AttachmentRelay(self.s, **kwargs)

    def test_baseline_parsing_is_linear_across_observations_and_send_ticks(self):
        self.ready([row(i) for i in range(1,129)])
        with patch.object(relay_module, 'saved_ships', wraps=relay_module.saved_ships) as parse:
            for i in range(1,129):
                self.observe(i,i,1)
            served=set()
            for step in range(4):
                served.update(r.ship for r in self.delivered(1+step*.05))
            self.assertEqual(served,set(range(1,129)))
            self.assertLessEqual(parse.call_count,1,
                'one immutable baseline must not be parsed for every ship or send tick')
            # A replacement baseline in the same sector must invalidate the map.
            self.ready([row(129)],revision=2,now=1.2)
            self.observe(999,1,2,now=1.3)  # no longer in the committed baseline
            self.observe(1,129,2,now=1.3)
            self.assertEqual([r.ship for r in self.delivered(1.3)],[129])
            self.assertLessEqual(parse.call_count,2)
            # The single cached map must follow the active view, not old sectors.
            self.ready([row(130)],revision=3,sector=OTHER,now=1.4)
            self.observe(2,129,3,now=1.5)
            self.observe(1,130,3,now=1.5)
            self.assertEqual([r.ship for r in self.delivered(1.5)],[130])
            self.assertLessEqual(parse.call_count,3)
            before=parse.call_count
            self.s.attachments.reset()
            self.observe(2,130,3,now=1.6)
            self.assertEqual([r.ship for r in self.delivered(1.6)],[130])
            self.assertEqual(parse.call_count,before+1,'epoch reset must discard its cached map')


if __name__ == '__main__':
    unittest.main()
