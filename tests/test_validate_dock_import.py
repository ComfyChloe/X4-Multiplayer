import unittest
from dev.validate_dock_import import validate

SEED='a'*32


def log(mode, offset=0):
    result=[f'[X4LAN] DOCK_IMPORT_PAUSE mode={mode} paused=true']
    def emit(s): result.append('[X4LAN] DOCK_IMPORT '+s)
    def event(kind, serial, rest): emit(f'event mode={mode} kind={kind} value={SEED}|{serial}|{rest}')
    def row(index, serial):
        external,storage=('1','0') if index==1 else ('0','1')
        event('Row',serial,f'{index}|{hex(offset+index+10)}|{hex(offset+10)}|{hex(offset+10)}|dock_a_macro|ship_carrier_macro|{external}|{storage}|0|0.001')
    fingerprints={1:f'{SEED}:1:dock_a_macro:ship_carrier_macro:1:0:0:1:2:2',
                  2:f'{SEED}:2:dock_a_macro:ship_carrier_macro:0:1:0:3:2:2'}
    event('Begin',0,'1|2|2');row(1,0);row(2,0);event('End',0,'1|2|2|1')
    for i,f in fingerprints.items():emit(f'fingerprint mode={mode} index={i} value={f}')
    for i,state in ((1,'EXTERNAL'),(2,'STORED')):
        event('Begin',i,f'{i}|{i}|2');row(i,i);event('End',i,f'{i}|{i}|2|1')
        emit(f'queried mode={mode} index={i} state={state} fingerprint={fingerprints[i]}')
    emit(f'complete mode={mode} seed={SEED} count=2')
    return result


class DockImportValidatorTests(unittest.TestCase):
    def test_renumbered_native_graph_and_both_correlated_queries(self):
        result=validate('\n'.join(log('capture')), '\n'.join(log('load',100)), SEED)
        self.assertTrue(result['passed'])
        self.assertEqual(result['local_handles_renumbered'],2)
        self.assertEqual(result['queried_states'],['EXTERNAL','STORED'])

    def test_missing_reordered_early_callback_and_tampered_graph_rejected(self):
        original=log('capture')
        changes=[]
        for index in range(16):
            changed=log('load',100);changed.pop(index);changes.append(changed)
        changed=log('load',100);changed[9],changed[10]=changed[10],changed[9];changes.append(changed)
        changed=log('load',100);changed[2],changed[3]=changed[3],changed[2];changes.append(changed)
        changed=log('load',100);changed[5]=changed[5].replace(':1:2:2',':1:3:2');changes.append(changed)
        changed=log('load',100);changed[12]=changed[12].replace('|0.001','|0.051');changes.append(changed)
        for changed in changes:
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):validate('\n'.join(original),'\n'.join(changed),SEED)


if __name__=='__main__': unittest.main()
