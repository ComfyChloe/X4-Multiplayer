import re,unittest
from dev.validate_attachment_execution import validate
from test_validate_attachment_controller import trace as controller_trace,SEED


def trace():
    out=[]
    for line in controller_trace():
        value=line.split('[X4LAN] ',1)[1]
        if value.startswith('ATTACH_CONTROLLER dispatch '):
            operation=re.search(r'operation=(\d+)',value)[1]
            out.extend([line.replace(' dispatch ',' prepare '),f'[X4LAN] ATTACH_EXEC prepared operation={operation} accepted=true',line,f'[X4LAN] ATTACH_CONTROLLER window operation={operation} result=issued'])
        elif value.startswith('ATTACH_NATIVE action '):
            operation=re.search(r'operation=(\d+)',value)[1]
            out.extend([f'[X4LAN] ATTACH_EXEC applied operation={operation} outcome=applied',
                        re.sub(r' phase=\d+','',line).replace('ATTACH_NATIVE action','ATTACH_EXEC action').replace(' allowed=true','')])
        elif value.startswith('ATTACH_CONTROLLER acknowledged '):
            out.append(line.replace(' acknowledged ',' execution-ack ')+' outcome=applied')
        else:
            out.append(line)
            if value.startswith('ATTACH_CONTROLLER selected '):out.append('[X4LAN] ATTACH_CONTROLLER execution-ready epoch='+SEED)
    return out


class NativeExecutionValidatorTests(unittest.TestCase):
    def test_full_cycle_requires_native_prepare_window_consume_action_and_ack(self):
        result=validate('\n'.join(trace()),SEED)
        self.assertEqual((result['operations'],result['native_prepare_attempts'],result['native_rejected_consumptions']),(9,9,0))

    def test_missing_reordered_wrong_or_extra_boundary_evidence_never_passes(self):
        original=trace();variants=[]
        for i,line in enumerate(original):
            if any(s in line for s in ('ATTACH_EXEC ',' execution-ack ',' window ',' prepare ',' execution-ready ')):
                variants.append(original[:i]+original[i+1:])
        for token in ('ATTACH_EXEC action','ATTACH_EXEC applied','ATTACH_CONTROLLER execution-ack'):
            variants.append([x for x in original if token not in x]+[x for x in original if token in x])
        for before,after in (('accepted=true','accepted=false'),('outcome=applied','outcome=unknown'),
                             ('result=issued','result=expired'),('code=ABC-123','code=XYZ-789'),
                             ('shipIndex=1 code=ABC-123','shipIndex=2 code=ABC-123')):
            # Alter the actual consumer action only for actor/code changes.
            variants.append([x.replace(before,after) if not before.startswith(('code=','shipIndex=')) or 'ATTACH_EXEC action' in x else x for x in original])
        variants.append(original+['[X4LAN] ATTACH_EXEC applied operation=10 outcome=applied'])
        variants.append(original[:-1]+['Error in MD cue md.X4LAN_Attachment.Apply: native error']+original[-1:])
        for lines in variants:
            with self.subTest(lines=lines),self.assertRaises(ValueError):validate('\n'.join(lines),SEED)

    def test_exact_rejection_can_retry_but_unknown_action_cannot_be_hidden(self):
        original=trace();first=next(i for i,x in enumerate(original) if 'ATTACH_CONTROLLER prepare ' in x)
        # Insert one consumed/no-mutation rejection before the first successful attempt.
        failed=original[first:first+4]+['[X4LAN] ATTACH_EXEC applied operation=1 outcome=rejected',
                                     '[X4LAN] ATTACH_CONTROLLER execution-ack operation=1 outcome=rejected']
        tail=[re.sub(r'operation=(\d+)',lambda m:'operation='+str(int(m[1])+1),x) for x in original[first:]]
        lines=original[:first]+failed+tail
        result=validate('\n'.join(lines),SEED)
        self.assertEqual((result['native_prepare_attempts'],result['native_rejected_consumptions'],result['operations']),(10,1,9))
        action='[X4LAN] ATTACH_EXEC action operation=1 action=ATTACH state=STORED dock=2 shipIndex=1 code=ABC-123'
        with self.assertRaises(ValueError):validate('\n'.join(original[:first]+failed[:5]+[action]+failed[5:]+tail),SEED)


if __name__=='__main__':unittest.main()
