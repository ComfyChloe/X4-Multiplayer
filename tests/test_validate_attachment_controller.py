import unittest
from dev.validate_attachment_controller import validate,STATES
from test_validate_dock_import import log,SEED


def trace():
    lines=log('load')
    # A nonempty storage capacity for the independent controller fixture.
    lines=[s.replace('|0|1|0|0.001','|0|1|10|0.001').replace(':0:1:0:3:',':0:1:10:3:') for s in lines]
    def emit(s):lines.append('[X4LAN] '+s)
    emit(f'ATTACH_NATIVE selected seed={SEED} shipIndex=1 carrierIndex=2 ship=0x64 carrier=0xa code=ABC-123 inside=2 outside=1')
    emit(f'ATTACH_CONTROLLER selected shipIndex=1 carrierIndex=2 inside=2 outside=1 identity={SEED}:1:ship_fighter_macro:argon:ABC-123')
    operation=0;serial=2
    for phase,state in enumerate(STATES):
        dock=0 if state=='FREE' else (2 if state=='STORED' else 1)
        actions=[]
        if phase:
            if STATES[phase-1]!='FREE':actions.append(('UNDOCK','FREE',0))
            if state!='FREE':actions.append(('ATTACH',state,dock))
        for action,goal,target in actions:
            if action=='ATTACH':
                serial+=1
                emit(f'ATTACH_DOCK_EVENT kind=Begin value={SEED}|{serial}|{target}|{target}|2')
                flags='0|1|10' if goal=='STORED' else '1|0|0'
                emit(f'ATTACH_DOCK_EVENT kind=Row value={SEED}|{serial}|{target}|{hex(target+10)}|0xa|0xa|dock_a_macro|ship_carrier_macro|{flags}|0.001')
                emit(f'ATTACH_DOCK_EVENT kind=End value={SEED}|{serial}|{target}|{target}|2|1')
            operation+=1
            emit(f'ATTACH_CONTROLLER dispatch phase={phase} operation={operation} sequence={operation} action={action} state={goal} dock={target}')
            emit(f'ATTACH_NATIVE action operation={operation} phase={phase} action={action} state={goal} dock={target} allowed=true shipIndex=1 code=ABC-123')
        emit(f'ATTACH_CONTROLLER read phase={phase} state={state} dock={dock} shipIndex=1')
        emit(f'ATTACH_NATIVE read phase={phase} shipIndex=1 code=ABC-123 stored={str(state=="STORED").lower()} docked={str(state!="FREE").lower()} dock={dock}')
    emit(f'ATTACH_CONTROLLER complete phases=8 operations={operation}')
    return lines


class NativeControllerValidatorTests(unittest.TestCase):
    def test_full_ordered_cycle_requires_initial_store_and_nine_native_actions(self):
        result=validate('\n'.join(trace()),SEED)
        self.assertEqual((result['phases'],result['operations'],result['correlated_target_queries']),(8,9,5))

    def test_missing_action_early_read_wrong_actor_or_target_never_passes(self):
        original=trace();variants=[]
        for i,s in enumerate(original):
            if any(k in s for k in ('ATTACH_NATIVE action','ATTACH_NATIVE read','ATTACH_DOCK_EVENT kind=End')):
                variants.append(original[:i]+original[i+1:])
        actions=[s for s in original if 'ATTACH_NATIVE action' in s]
        variants.append([s for s in original if 'ATTACH_NATIVE action' not in s]+actions)
        for old,new in (
            ('allowed=true','allowed=false'),
            ('ATTACH_NATIVE read phase=1 shipIndex=1','ATTACH_NATIVE read phase=1 shipIndex=2'),
            ('code=ABC-123 stored=false','code=ZZZ-999 stored=false'),
            ('state=FREE dock=0 shipIndex=1','state=FREE dock=2 shipIndex=1'),
            ('sequence=1 action=ATTACH','sequence=0 action=ATTACH'),
            ('operations=9','operations=8'),
        ):
            variants.append([s.replace(old,new) for s in original])
        for variant in variants:
            with self.subTest(variant=variant),self.assertRaises(ValueError):
                validate('\n'.join(variant),SEED)

    def test_completed_import_must_precede_selection_and_native_actions(self):
        original=trace()
        import_lines=[s for s in original if 'ATTACH_' not in s]
        controller_lines=[s for s in original if 'ATTACH_' in s]
        with self.assertRaisesRegex(ValueError,'precedes completed saved dock import'):
            validate('\n'.join(controller_lines+import_lines),SEED)


if __name__=='__main__':unittest.main()
