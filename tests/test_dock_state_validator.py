import unittest
from dev.validate_dock_state import validate


def trace():
    lines=['DOCK_STATE_NATIVE selected code=CHD-001 carrier=CAR-001 ship=0x65 carrierHandle=0x66 inside=0x10 outside=0x20']
    for phase in range(6):
        stored=phase in (0,2,5);docked=phase!=4
        if phase:lines.append(f'DOCK_STATE_NATIVE applied phase={phase} code=CHD-001')
        lines.append(f'DOCK_STATE read phase={phase} stored={str(stored).lower()} docked={str(docked).lower()} ship=101 carrier=102')
        lines.append(f'DOCK_STATE_NATIVE read phase={phase} code=CHD-001 live=1 stored={int(stored)} docked={int(docked)} sameCarrier={int(docked)} sameInside={int(stored)} sameOutside={int(phase in (1,3))}')
    return '\n'.join(lines+['DOCK_STATE complete phases=6'])


class NativeDockStateTests(unittest.TestCase):
    def test_complete_native_transition_cycle(self):
        self.assertEqual(validate(trace())['phases'],6)

    def test_partial_wrong_dock_changed_ship_and_unresolved_headers_rejected(self):
        raw=trace()
        mutations=[raw.replace('sameOutside=1','sameOutside=0',1),raw.replace('sameInside=1','sameInside=0',1),
                   raw.replace('code=CHD-001 live=1','code=OTH-002 live=1',1),raw.replace('ship=101 carrier=102','ship=103 carrier=102',1),
                   raw.replace('DOCK_STATE_NATIVE read','DOCK_STATE_NATIVE unavailable'),raw.replace('live=1','live=0',1),
                   raw.replace('DOCK_STATE complete phases=6',''),raw.replace('DOCK_STATE_NATIVE applied phase=3','unavailable'),
                   raw+'\nDOCK_STATE failed timeout phase=5',raw+'\n[=ERROR=] extensions\\x4_lan_lab\\md\\dock_state_probe.xml(5): required attribute missing',
                   raw+'\n'+raw.splitlines()[2],raw.replace('outside=0x20','outside=0x10')]
        for bad in mutations:
            with self.subTest(bad=bad[-80:]),self.assertRaises(ValueError):validate(bad)

    def test_native_handles_action_identity_and_causal_order_are_required(self):
        raw=trace();lines=raw.splitlines()
        actions_last='\n'.join([line for line in lines if ' applied ' not in line]+[line for line in lines if ' applied ' in line])
        mutations=[actions_last,raw.replace('ship=0x65','ship=unavailable'),raw.replace('ship=0x65','ship=0x0'),
                   raw.replace('ship=0x65','ship=0x67'),raw.replace('carrierHandle=0x66','carrierHandle=0x65'),
                   raw.replace('applied phase=1 code=CHD-001','applied phase=1 code=OTH-002'),
                   '\n'.join([lines[0],lines[2],lines[1]]+lines[3:]),
                   raw+'\nDOCK_STATE_NATIVE read unavailable',raw+'\nDOCK_STATE_NATIVE selected unavailable']
        for bad in mutations:
            with self.subTest(bad=bad[-80:]),self.assertRaises(ValueError):validate(bad)
