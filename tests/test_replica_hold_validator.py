import unittest
from dev.validate_replica_hold import validate
from test_validate_attachment_guest import trace,HOST


class ReplicaHoldDiagnosticTests(unittest.TestCase):
    def evidence(self):
        _,replay,_,stats=trace();replay['diagnostic_native_hold']=True
        lines=[f'[X4LAN] ATTACH_WORLD READY epoch={HOST}',
            f'[X4LAN] REPLICA_HOLD request id=1 epoch={HOST} code=NAT-001 macro=ship_arg_s_scout_01_a_macro owner=argon',
            '[X4LAN] REPLICA_HOLD_NATIVE applied id=1 code=NAT-001 macro=ship_arg_s_scout_01_a_macro owner=argon sector=cluster_14_sector001_macro mode=constant_zero',
            '[X4LAN] REPLICA_HOLD ack id=1 outcome=held',
            '[X4LAN] ATTACH_GUEST complete slot=p2']
        return lines,replay,stats

    def test_complete_comparison_is_explicitly_not_production_or_ai_order_acceptance(self):
        lines,replay,stats=self.evidence();r=validate('\n'.join(lines),replay,stats)
        self.assertEqual(r['held_free_ships'],1);self.assertFalse(r['production_acceptance']);self.assertFalse(r['ai_order_suppression_accepted'])

    def test_missing_reordered_foreign_or_unrequested_native_mutation_is_rejected(self):
        lines,replay,stats=self.evidence()
        variants=[lines[:i]+lines[i+1:] for i in range(len(lines))]
        variants+=[lines[:2]+lines[3:]+[lines[2]],lines+[lines[2]]]
        for a,b in (('id=1','id=2'),('code=NAT-001','code=NAT-003'),('outcome=held','outcome=busy'),('mode=constant_zero','mode=other')):
            variants.append([v.replace(a,b) for v in lines])
        for changed in variants:
            with self.assertRaises(ValueError):validate('\n'.join(changed),replay,stats)
        replay['diagnostic_native_hold']=False
        with self.assertRaises(ValueError):validate('\n'.join(lines),replay,stats)
