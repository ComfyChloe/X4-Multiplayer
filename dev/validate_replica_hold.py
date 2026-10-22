"""Evidence for the explicit constant-zero diagnostic, never production acceptance."""
from dev.validate_attachment_guest import fields
from dev.validate_attachment_world import require


def validate(text,replay,stats):
    require(replay.get('diagnostic_native_hold') is True,'native hold comparison was not explicitly selected')
    host=stats['peers']['p1']['lease'];pending={};held=set();native=set();ready=False;complete=False
    expected={int(k):v for k,v in replay['attachments'].items() if v['state']=='FREE'}
    identity={int(k):v for k,v in replay['identities'].items()};rows={int(w.split(',')[0]):w.split(',') for w in replay['rows']}
    for line in text.splitlines():
        if '[X4LAN] ATTACH_WORLD READY epoch='+host in line:ready=True
        if '[X4LAN] ATTACH_GUEST complete ' in line:
            require(not pending and held==expected.keys(),'hold comparison incomplete at guest completion');complete=True
        if '[X4LAN] REPLICA_HOLD' not in line:continue
        require(ready and not complete and 'STOPPED' not in line,'hold evidence outside active diagnostic boundary')
        kind,action,raw=line.split('[X4LAN] ',1)[1].split(' ',2);f=fields(raw);ship=int(f['id'])
        require(ship in expected,'diagnostic modified a ship outside the recorded FREE subset')
        if kind=='REPLICA_HOLD' and action=='request':
            require(ship not in pending and ship not in held and f['epoch']==host,'duplicate or foreign hold request')
            require(f['code']==identity[ship]['code'] and [f['macro'],f['owner']]==rows[ship][2:4],'hold request descriptor differs')
            pending[ship]=f
        elif kind=='REPLICA_HOLD_NATIVE' and action=='applied':
            require(ship in pending and ship not in native and f['mode']=='constant_zero'
                and all(f[k]==pending[ship][k] for k in ('code','macro','owner')),'native hold lacks matching request')
            native.add(ship)
        elif kind=='REPLICA_HOLD' and action=='ack':
            require(ship in pending and f['outcome'] in ('held','busy'),'unmatched hold acknowledgment')
            require((f['outcome']=='held')==(ship in native),'hold acknowledgment lacks native action')
            del pending[ship]
            if f['outcome']=='held':held.add(ship)
        else:raise ValueError('unknown native hold evidence')
    require(complete and held==expected.keys(),'native hold diagnostic incomplete')
    return dict(passed=True,diagnostic_native_hold=True,held_free_ships=len(held),
        production_acceptance=False,ai_order_suppression_accepted=False,ongoing_world_acceptance=False)
