"""Validate retained native role evidence, rejecting a stale opposite-role log."""
import re
from x4lan import seed


def validate_role(text, seed_id, slot):
    if not seed._hex(seed_id, seed.HEX32) or slot not in ('p1', 'p2'):
        raise ValueError('invalid expected native world/role')
    markers = ['SEED_NATIVE loaded count=1 marked=1',
               'SEED_GATE READY seed=' + seed_id + ' slot=' + slot,
               'SEED_PLAY_STATE value=' + seed_id + '|1|1 paused=false',
               'SEED_PLAY_READY seed=' + seed_id + ' slot=' + slot +
               ' controlled=true ownAlive=true proxyAlive=true']
    offsets = []
    for marker in markers:
        if text.count(marker) != 1:
            raise ValueError('missing/duplicate expected native role or unpaused readback')
        offsets.append(text.index(marker))
    if offsets != sorted(offsets):
        raise ValueError('native role/readiness ordering differs')
    match = re.findall(r'SEED_PLAY_NATIVE seed=(\w+) slot=([12]) host=([01]) tradeHost=([01]) '
                       r'cargoHost=([01]) questHost=([01]) shuttles=(\d+) markets=(\d+) '
                       r'player=(0x[0-9a-f]+) p1=(0x[0-9a-f]+) p2=(0x[0-9a-f]+)(?=\s|$)', text)
    if len(match) != 1:
        raise ValueError('missing/duplicate native MD role readback')
    row = match[0]; host = '1' if slot == 'p1' else '0'
    if (row[:8] != (seed_id, slot[-1], host, host, host, host, '2', '2')
            or row[8] != row[9 if slot == 'p1' else 10] or row[9] == row[10]
            or any(int(value,16)==0 for value in row[8:]) or text.index('SEED_PLAY_NATIVE seed=') < offsets[-1]):
        raise ValueError('native role, controlled object or completion ordering differs')
    if any(s in text for s in ('SEED_GATE STOPPED', 'ADAPTER STOPPED', 'Lua error', 'Error in MD cue md.X4LAN')):
        raise ValueError('native adapter error retained')
    return dict(seed_id=seed_id, slot=slot, native_role_matches=True,
                own_ship_controlled=True, load_unpaused=True, shared_shuttles=2, markets=2,
                two_native_playtest=False)
