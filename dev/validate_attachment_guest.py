"""Full native guest replay evidence, with bounded late sustained convergence.

All cycles are retained. Earlier mismatches remain reported; the final three
complete cycles must match, without filtering away ships or negative readbacks.
This gate makes no live two-engine, dynamic-ship or whole-universe claim.
"""
from dataclasses import replace
import math,re
from x4lan.attachment import decode,encode,HEX
from dev.validate_attachment_world import require


def fields(value):
    parts=value.split();out={}
    for item in parts:
        key,sep,val=item.partition('=')
        require(sep and key and val and key not in out,'malformed evidence fields');out[key]=val
    return out


def validate(text,replay,dock_count,history,stats):
    require(stats.get('complete') is True and stats.get('native_guest') is True
        and stats.get('native_host') is False and stats.get('replayed_native_capture') is True
        and stats.get('send_errors')==0,'incomplete guest transport evidence')
    peers=stats.get('peers',{});host=peers.get('p1',{}).get('lease');guest=peers.get('p2',{}).get('lease')
    require(isinstance(host,str) and HEX.fullmatch(host) and isinstance(guest,str) and HEX.fullmatch(guest)
        and host!=guest,'missing or aliased guest replay leases')
    seed=replay['seed_id'];expected={int(w.split(',')[0]):w.split(',') for w in replay['rows']}
    goals={int(k):v for k,v in replay['attachments'].items()};identities={int(k):v for k,v in replay['identities'].items()}
    require(expected and expected.keys()==goals.keys()==identities.keys(),'incomplete host replay subset')
    baselines={};published={};host_ids={};last_seq=0
    for e in history:
        if e['event']=='baseline':
            require(e['epoch']==host and e['rows']==replay['rows'] and e['sector']==replay['sector']
                and e['revision'] not in baselines,'replay baseline differs from qualified capture')
            baselines[e['revision']]=e
        elif e['event']=='identity':
            f=e['packet'].split('|');require(len(f)==11 and f[:4]==['IDENT','1',host,host] and f[5]=='N','wrong replay identity')
            ship=int(f[6]);require(ship in expected and dict(zip(('code','name','known','contact'),f[7:]))==identities[ship],
                'replayed native identity differs');host_ids[ship]=True
        elif e['event']=='attachment':
            row=decode(e['packet']);require(row.recipient==host and row.epoch==host and row.revision in baselines
                and row.sequence>last_seq and row.ship in goals and row.state==goals[row.ship]['state']
                and row.dock==goals[row.ship]['dock'],'replayed attachment differs')
            last_seq=row.sequence;published[row.sequence]=row
        else:raise ValueError('unknown guest peer evidence')
    require(expected.keys()==host_ids.keys() and len(baselines)>=3,'incomplete replay publication')
    admitted=unpaused=fixture_lua=fixture_md=complete=False
    native_baselines={};received={};cycles={};samples={};md={};acks=set();handles={};operations={};execution_acks=0
    summary=None;current_cycle=0;native_ready=False;received_sequences={}
    for line in text.splitlines():
        require('Error in MD cue md.X4LAN' not in line and 'Lua error' not in line,'native guest engine error')
        if '[X4LAN]' in line:require('STOPPED' not in line and 'ATTACH_GUEST failed' not in line,'native guest adapter failed')
        if '[X4LAN] SEED_PLAY_READY ' in line:
            require(not fixture_lua and line.split('[X4LAN] ',1)[1]==
                f'SEED_PLAY_READY seed={seed} slot=p2 controlled=true ownAlive=true proxyAlive=true','guest player fixture differs');fixture_lua=True
        if '[X4LAN] SEED_PLAY_NATIVE ' in line:
            m=re.fullmatch(r'SEED_PLAY_NATIVE seed=(\w+) slot=2 host=(0|false) tradeHost=(0|false) cargoHost=(0|false) questHost=(0|false) shuttles=2 markets=2 player=(0x[0-9a-fA-F]+) p1=(0x[0-9a-fA-F]+) p2=(0x[0-9a-fA-F]+)',line.split('[X4LAN] ',1)[1])
            require(not fixture_md and m and m[1]==seed and int(m[6],16)==int(m[8],16)>0 and int(m[7],16)>0
                and m[7]!=m[8],'guest native authority/player differs');fixture_md=True
        if '[X4LAN] ATTACH_WORLD READY ' in line:
            require(native_baselines and not native_ready and line.split('[X4LAN] ',1)[1]=='ATTACH_WORLD READY epoch='+host,
                    'native execution readiness differs');native_ready=True
        if '[X4LAN] ATTACH_EXEC ' in line:
            require(not complete,'native execution after completion')
            value=line.split('[X4LAN] ATTACH_EXEC ',1)[1];action,raw=value.split(' ',1);f=fields(raw);op=int(f['operation'])
            require(op in operations,'native execution without local preparation');item=operations[op]
            if action=='prepared':
                require('native_prepared' not in item and not item['issued'] and f['accepted'] in ('true','false','1','0'),
                        'native preparation order differs');item['native_prepared']=f['accepted'] in ('true','1')
            elif action=='applied':
                require(item['issued'] and item['window'] and 'native_outcome' not in item and f['outcome'] in ('applied','rejected'),
                        'native apply before dispatch/window or duplicate');item['native_outcome']=f['outcome']
            elif action=='action':
                command=item['command'];ship=int(command['ship'])
                require(item.get('native_outcome')=='applied' and not item.get('native_action')
                    and all(f[k]==command[k] for k in ('action','state','dock'))
                    and int(f['shipIndex'])==ship and f['code']==identities[ship]['code'],'native action descriptor differs')
                item['native_action']=True
            else:raise ValueError('unknown native execution evidence')
        if '[X4LAN] ATTACH_GUEST' not in line:continue
        require(not complete,'guest evidence after completion');v=line.split('[X4LAN] ',1)[1]
        kind,rest=v.split(' ',1);action,_,raw=rest.partition(' ');f=fields(raw)
        if kind=='ATTACH_GUEST_NATIVE':
            require(action=='sample','unknown guest native witness');seq=int(f['sequence'])
            require(seq in samples and seq not in md,'native sample before Lua request or duplicated')
            lua=samples[seq];ship=int(f['id'])
            require(int(f['saved'])==ship and all(f[k]==lua[k] for k in ('cycle','revision','id','code','macro','owner')),
                'native saved identity differs from Lua request')
            if replay.get('geometry_evidence'):
                require(f.get('sector')==lua.get('sector') and f.get('sector'),'native/Lua sector readback differs')
            md[seq]=f
        elif action=='admitted':
            require(not admitted and f==dict(seed=seed,docks=str(dock_count),paused='true'),'guest paused admission differs');admitted=True
        elif action=='diagnostic-unpause':
            require(admitted and not unpaused and f==dict(seed=seed),'guest unpause order differs');unpaused=True
        elif action=='baseline':
            rev=int(f['revision']);require(unpaused and f['epoch']==host and rev in baselines and rev not in native_baselines
                and int(f['count'])==len(expected),'native baseline differs from host replay');native_baselines[rev]=True
        elif action=='wire':
            row=decode(f['value']);at=float(f['at']);require(math.isfinite(at) and row.sequence in published
                and encode(row)==encode(replace(published[row.sequence],recipient=guest)),'native wire has no exact host provenance')
            if f['result']=='applied':
                previous=received.get(row.ship)
                require(row.revision in native_baselines and row.sequence not in received_sequences
                    and (not previous or row.sequence>previous[0].sequence),'native attachment before baseline or per-ship replay accepted')
                received[row.ship]=(row,at);received_sequences[row.sequence]=(row,at)
        elif action=='cycle':
            cycle=int(f['index']);require(cycle==current_cycle+1 and (not current_cycle or cycles[current_cycle].get('ended'))
                and int(f['revision']) in native_baselines and math.isfinite(float(f['at']))
                and math.isfinite(float(f['elapsed'])) and float(f['elapsed'])>=3
                and (not current_cycle or float(f['elapsed'])-float(cycles[current_cycle]['meta']['elapsed'])>=1),
                'guest cycle order/time differs')
            current_cycle=cycle;cycles[cycle]=dict(meta=f,rows={},missing=set())
        elif action=='missing':
            ship=int(f['id']);require(int(f['cycle'])==current_cycle and ship in expected,'unexpected missing ship')
            cycles[current_cycle]['missing'].add(ship)
        elif action=='sample':
            seq=int(f['sequence']);cycle=int(f['cycle']);ship=int(f['id']);handle=int(f['handle']);at=float(f['at'])
            require(seq==len(samples)+1 and cycle==current_cycle and ship in expected and handle>0
                and ship not in cycles[cycle]['rows'] and f['revision']==cycles[cycle]['meta']['revision']
                and f['at']==cycles[cycle]['meta']['at'],'guest sample order/coverage differs')
            require(f['original']=='true' and f['sameHandle']=='true' and (ship not in handles or handles[ship]==handle)
                and f['macro']==expected[ship][2] and f['owner']==expected[ship][3] and f['code']==identities[ship]['code'],
                'guest binding is not the stable original saved ship')
            require(math.isfinite(at) and math.isfinite(float(f['poseError'])) and float(f['poseError'])>=0,'invalid guest sample time/pose')
            if replay.get('geometry_evidence'):
                require(f.get('sector') and f.get('parent','').isdigit(),'missing guest native parent/sector')
                vectors=[list(map(float,f.get(k,'').split(','))) for k in ('pose','target')]
                require(all(len(v)==3 and all(math.isfinite(x) for x in v) for v in vectors),'missing/nonfinite native pose geometry')
                error=max(abs(a-b) for a,b in zip(*vectors))
                rounding=max(.001,max(abs(x) for v in vectors for x in v)*1.1e-8)
                require(abs(error-float(f['poseError']))<=rounding,'reported native pose error differs from coordinates')
            handles[ship]=handle;samples[seq]=f;cycles[cycle]['rows'][ship]=seq
            receipt=received.get(ship)
            f['fresh']=bool(receipt and 0<=at-receipt[1]<=2 and receipt[0].state==goals[ship]['state']
                            and receipt[0].dock==goals[ship]['dock'])
        elif action=='cycle-end':
            cycle=int(f['index']);require(cycle==current_cycle and not cycles[cycle].get('ended')
                and int(f['count'])==len(expected) and set(cycles[cycle]['rows'])|cycles[cycle]['missing']==set(expected),
                'guest cycle omitted or duplicated saved ships');cycles[cycle]['ended']=True
        elif action=='sample-complete':
            seq=int(f['sequence']);require(seq in md and seq not in acks,'native completion before witness or duplicated');acks.add(seq)
        elif action=='prepare':
            op=int(f['operation']);ship=int(f['ship']);seq=int(f['sequence']);at=float(f['at'])
            require(native_ready and op not in operations and ship in expected and seq in received_sequences,'duplicate/unbound/unadmitted operation')
            observation,received_at=received_sequences[seq]
            require(observation.ship==ship and 0<=at-received_at<=.75001,'native preparation lacks fresh original intent')
            require((f['action']=='UNDOCK' and f['state']=='FREE' and f['dock']=='0') or
                (f['action']=='ATTACH' and f['state']==observation.state and int(f['dock'])==observation.dock),
                'native preparation differs from intent')
            operations[op]=dict(pending=True,issued=False,window=False,command=f,received_at=received_at)
        elif action=='dispatch':
            op=int(f['operation']);at=float(f['at']);item=operations.get(op,{})
            require(item.get('pending') and not item['issued'] and item.get('native_prepared') is True
                and 0<=at-float(item['command']['at'])<=.50001 and 0<=at-item['received_at']<=.75001,
                'dispatch without timely accepted native preparation');item['issued']=True
        elif action=='window':
            op=int(f['operation']);require(op in operations and not operations[op]['window']
                and 'native_prepared' in operations[op],'unmatched native window')
            operations[op]['window']=True
            require((f['result']=='issued')==operations[op]['issued'],'native window/dispatch outcome differs')
            if f['result']!='issued':operations[op]['pending']=False
        elif action=='execution-ack':
            op=int(f['operation']);require(op in operations and operations[op]['pending'] and operations[op]['issued']
                and operations[op]['window'] and f['result'] in ('accepted','rejected') and f['outcome'] in ('applied','rejected'),
                'unmatched native execution acknowledgment')
            require(operations[op].get('native_outcome')==f['outcome']
                and bool(operations[op].get('native_action'))==(f['outcome']=='applied'),'native acknowledgment lacks actual outcome/action')
            operations[op]['pending']=False;execution_acks+=1
        elif action=='complete':
            require(fixture_lua and fixture_md and native_ready and f['slot']=='p2' and f['live_native_host']=='false'
                and int(f['cycles'])==len(cycles)>=10 and int(f['samples'])==len(samples)==len(md)==len(acks)
                and int(f['executionAcks'])==execution_acks and 30<=float(f['elapsed'])<=45
                and all(c.get('ended') for c in cycles.values())
                and all(not op['pending'] and op['window'] for op in operations.values()),'incomplete guest native coverage/outcomes')
            complete=True;summary=f
        else:raise ValueError('unknown guest native evidence')
    require(complete and cycles,'native guest did not complete')
    reports=[]
    for cycle,row in cycles.items():
        failures=[dict(id=ship,reason='missing') for ship in sorted(row['missing'])];free_errors=[];attached_errors=[]
        for ship,seq in row['rows'].items():
            lua=samples[seq];native=md[seq];goal=goals[ship];free=goal['state']=='FREE';err=float(lua['poseError'])
            (free_errors if free else attached_errors).append(err)
            if not (lua['fresh'] and lua['state']==native['state']==goal['state'] and int(lua['dock'])==int(native['dock'])==goal['dock']
                and lua['allowPose']==str(free).lower() and (not free or err<=.1)
                and (not replay.get('geometry_evidence') or lua['sector']==replay['sector'])):
                failures.append(dict(id=ship,lua_state=lua['state'],native_state=native['state'],wanted=goal,
                    lua_dock=int(lua['dock']),native_dock=int(native['dock']),fresh=lua['fresh'],allow_pose=lua['allowPose'],pose_error=err,
                    sector=lua.get('sector'),parent=lua.get('parent')))
        reports.append(dict(cycle=cycle,elapsed=float(row['meta']['elapsed']),failures=failures,
            max_free_pose_error=max(free_errors,default=0),max_attached_pose_difference=max(attached_errors,default=0)))
    require(len(reports)>=10 and reports[-1]['elapsed']>=27 and all(not r['failures'] for r in reports[-3:]),
        'guest final three cycles did not converge: '+str(reports[-3:]))
    return dict(passed=True,native_guest=True,live_native_host=False,replayed_native_host_capture=True,
        full_universe_adapter=True,fixture_authority=True,saved_ships=len(expected),cycles=reports,
        independent_native_samples=len(md),execution_acknowledgments=execution_acks,
        attached_pose_alignment_accepted=False,omitted_non_saved_entities=replay['omitted_rows'],
        whole_universe_acceptance=False,two_native_acceptance=False)
