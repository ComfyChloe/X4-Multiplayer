"""Strict native host publication/fixture evidence; no guest gameplay acceptance."""
from dataclasses import replace
import math,re
from x4lan.attachment import decode,encode,HEX,MAX_SHIP
from x4lan.universe import Baseline,record


def require(ok,why):
    if not ok:raise ValueError(why)


def validate(text,seed,dock_count,history,stats):
    require(stats.get('complete') is True and stats.get('native_host') is True
            and stats.get('native_guest') is False and stats.get('send_errors')==0,'peer incomplete or wrong native roles')
    peers=stats.get('peers',{});host=peers.get('p1',{}).get('lease');guest=peers.get('p2',{}).get('lease')
    require(isinstance(host,str) and HEX.fullmatch(host) and isinstance(guest,str) and HEX.fullmatch(guest)
            and host!=guest,'missing or aliased peer leases')
    admitted=False;unpaused=False;complete=False;fixture_lua=False;fixture_md=False
    publications={};requests={};samples={};acks=set();bindings={};last_sequence=0;last_time=-1;summary=None
    for line in text.splitlines():
        require('Error in MD cue md.X4LAN' not in line and 'Lua error' not in line,'native engine error')
        if '[X4LAN]' in line:require('STOPPED' not in line and 'ATTACH_FULL failed' not in line,'native adapter failed')
        if '[X4LAN] SEED_PLAY_READY ' in line:
            require(not fixture_lua and line.split('[X4LAN] ',1)[1]==
                    f'SEED_PLAY_READY seed={seed} slot=p1 controlled=true ownAlive=true proxyAlive=true','native player fixture differs')
            fixture_lua=True
        if '[X4LAN] SEED_PLAY_NATIVE ' in line:
            m=re.fullmatch(r'SEED_PLAY_NATIVE seed=(\w+) slot=1 host=(1|true) tradeHost=(1|true) cargoHost=(1|true) questHost=(1|true) shuttles=2 markets=2 player=(0x[0-9a-fA-F]+) p1=(0x[0-9a-fA-F]+) p2=(0x[0-9a-fA-F]+)',line.split('[X4LAN] ',1)[1])
            require(not fixture_md and m and m[1]==seed and int(m[6],16)==int(m[7],16)>0
                    and int(m[8],16)>0 and int(m[8],16)!=int(m[7],16),'native fixture authority differs');fixture_md=True
        if '[X4LAN] ATTACH_FULL' not in line:continue
        require(not complete,'evidence after native completion');value=line.split('[X4LAN] ',1)[1]
        if value.startswith('ATTACH_FULL admitted '):
            require(not admitted and value==f'ATTACH_FULL admitted seed={seed} docks={dock_count} paused=true',
                    'native admission count/paused boundary differs');admitted=True
        elif value=='ATTACH_FULL diagnostic-unpause seed='+seed:
            require(admitted and not unpaused,'diagnostic unpause before admission or duplicated');unpaused=True
        elif value.startswith('ATTACH_FULL publish '):
            m=re.fullmatch(r'ATTACH_FULL publish at=(\d+\.\d+) handle=(\d+) code=(\S+) macro=(\S+) owner=(\S+) value=(.*)',value)
            require(m and unpaused,'publication before admission or malformed');row=decode(m[6]);at=float(m[1])
            require(math.isfinite(at) and at>=last_time and int(m[2])>0 and row.recipient==host and row.epoch==host
                    and row.sequence>last_sequence,'native publication identity/time differs')
            binding=(int(m[2]),m[3],m[4],m[5]);require(row.ship not in bindings or bindings[row.ship]==binding,'native saved binding changed')
            bindings[row.ship]=binding;publications[row.sequence]=(row,binding,at);last_sequence=row.sequence;last_time=at
        elif value.startswith('ATTACH_FULL sample-request '):
            m=re.fullmatch(r'ATTACH_FULL sample-request sequence=(\d+) revision=(\d+) id=(\d+)',value)
            require(m,'invalid native sample request');seq,rev,ship=map(int,m.groups())
            require(seq in publications and seq not in requests and publications[seq][0].revision==rev
                    and publications[seq][0].ship==ship,'sample request differs from published state');requests[seq]=(rev,ship)
        elif value.startswith('ATTACH_FULL_NATIVE sample '):
            m=re.fullmatch(r'ATTACH_FULL_NATIVE sample sequence=(\d+) revision=(\d+) id=(\d+) saved=(\d+) code=(\S+) macro=(\S+) owner=(\S+) state=(FREE|EXTERNAL|STORED|TRANSIT) dock=(\d+)',value)
            require(m,'invalid independent native sample');seq,rev,ship,saved=map(int,m.groups()[:4])
            require(seq in requests and seq not in samples and requests[seq]==(rev,ship) and saved==ship,
                    'native saved-reference sample lacks ordered request')
            row,binding,_=publications[seq]
            require(tuple(m.groups()[4:7])==binding[1:] and m[8]==row.state and int(m[9])==row.dock,
                    'independent MD attachment/descriptor differs from publication')
            samples[seq]=(rev,ship)
        elif value.startswith('ATTACH_FULL sample-complete '):
            m=re.fullmatch(r'ATTACH_FULL sample-complete sequence=(\d+)',value);require(m,'invalid native sample completion');seq=int(m[1])
            require(seq in samples and seq not in acks,'native sample completion before readback or duplicated');acks.add(seq)
        elif value.startswith('ATTACH_FULL complete '):
            m=re.fullmatch(r'ATTACH_FULL complete slot=p1 revisions=(\d+) published=(\d+) samples=(\d+) native_guest=false',value)
            require(m and fixture_lua and fixture_md,'native completion before ordinary fixtures')
            summary=tuple(map(int,m.groups()));require(summary[0]>=3 and summary[1]==len(publications)
                and summary[2]==len(samples)==len(requests)==len(acks) and summary[2]>0,'native completion coverage differs')
            complete=True
        else:raise ValueError('unknown full-host evidence')
    require(complete and publications,'native host did not complete')
    revisions={row.revision for row,_,_ in publications.values()}
    require(summary[0]==len(revisions),'native published revision count differs')
    first={}
    for seq,(row,_,_) in publications.items():first.setdefault((row.revision,row.ship),seq)
    require(set(first.values())==set(requests),'missing first-per-revision independent native sample')
    baselines={};delivered={};identities={};omitted=[]
    for event in history:
        if event.get('event')=='baseline':
            require(event['epoch']==host and event['revision'] not in baselines,'peer baseline epoch/duplicate differs')
            b=Baseline.create(host,event['revision'],event['sector'],event['rows']);baselines[b.revision]=b
        elif event.get('event')=='identity':
            f=event['packet'].split('|');require(len(f)==11 and f[:4]==['IDENT','1',guest,host] and f[5]=='N','peer identity differs')
            identities.setdefault(int(f[6]),set()).add(f[7])
        elif event.get('event')=='attachment':
            row=decode(event['packet']);require(row.sequence in publications,'peer attachment has no native publication')
            native,_,_=publications[row.sequence]
            require(row.recipient==guest and row.epoch==host and encode(row)==encode(replace(native,recipient=guest))
                    and row.revision in baselines,'peer packet differs or preceded committed baseline')
            delivered.setdefault(row.revision,set()).add(row.ship)
        else:raise ValueError('unknown peer evidence')
    require(len(baselines)>=3 and revisions<=baselines.keys(),'native revisions absent from real recipient')
    for rev,b in baselines.items():
        expected=set()
        for wire in b.rows:
            fields=wire.split(',');ship=record(wire)
            if fields[1]=='ship' and ship<=MAX_SHIP:
                expected.add(ship);require(ship in bindings and tuple(fields[2:4])==bindings[ship][2:]
                    and bindings[ship][1] in identities.get(ship,set()),'native baseline/IDENT does not match saved binding')
            else:omitted.append(dict(revision=rev,id=ship,kind=fields[1]))
        require(expected and expected<=delivered.get(rev,set()),'recipient lacks attachment coverage for saved baseline ships')
    return dict(passed=True,native_host=True,native_guest=False,full_universe_adapter=True,
        fixture_authority=True,baselines=len(baselines),saved_ships=len(bindings),publications=len(publications),
        independent_native_samples=len(samples),omitted_non_saved_entities=omitted,
        whole_universe_acceptance=False,two_native_acceptance=False)
