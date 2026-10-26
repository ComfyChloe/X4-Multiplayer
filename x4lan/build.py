"""Station build intents (BUILDREQ). Original code; mirrors ui/build_protocol.lua.

A guest's station-planner confirmation arrives as a request ID, a header and a chunked
construction plan in X4's own plan XML. The relay authenticates the sender, deduplicates
by request ID, bounds size, validates the plan's strict canonical form and forwards it to
the authority (host X4 today, the dedicated server later). Only the authority decides and
mutates; the relay never infers success. Losing the authority after forwarding yields
UNCERTAIN and the request is never replayed into another authority.

Optionally the relay writes the validated plan into the authority's construction plan
folder (``plan_inbox``), from which authority X4 imports it by ID; X4 offers no way to
create a player-designed construction sequence from script data without a file.
"""
from __future__ import annotations

import math
import re
import zlib
from pathlib import Path

from .host import ATOM, HEX, MAX_PACKET, sequence

MAX_ENTRIES = 512
MAX_BYTES = 131072
PART_SIZE = 560
MAX_PARTS = 256
MAX_GROUPS = 64
LIMIT = 1024
PART_BUDGET = 32
GUEST_MISSING = 16
SILENT_AFTER = 20.0
GIVE_UP_AFTER = 300.0
KINDS = {'EXPAND', 'REMOVEPLOT', 'RECYCLE'}
FINAL = {'ACCEPTED', 'REFUSED', 'UNCERTAIN', 'STALE', 'BUSY'}
REASON = re.compile(r'[a-z0-9_:.-]{1,64}\Z')
PATH = re.compile(r'[A-Za-z0-9_./\\-]{1,128}\Z')
NUMBER = re.compile(r'-?\d+\.\d+\Z')


def _atom(value, macro=False):
    return bool(ATOM.fullmatch(value)) and (not macro or value.endswith('_macro'))


def _num(value, limit, digits):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > limit:
        raise ValueError('plan number')
    text = f'{value:.{digits}f}'
    return text[1:] if text == '-0.' + '0' * digits else text


def encode_plan(plan_id, entries):
    """Canonical X4 plan XML; the only spelling either side accepts."""
    if not _atom(plan_id) or len(entries) > MAX_ENTRIES:
        raise ValueError('plan size')
    out = [f'<plan id="{plan_id}" name="{plan_id}">']
    for i, e in enumerate(entries, 1):
        if e['index'] != i or not _atom(e['macro'], True):
            raise ValueError('plan entry')
        conn = e.get('connection', '')
        if conn and not _atom(conn):
            raise ValueError('plan connection')
        out.append(f'<entry index="{i}" macro="{e["macro"]}"' + (f' connection="{conn}"' if conn else '') + '>')
        pred = e.get('pred', 0)
        if pred:
            if not 1 <= pred <= len(entries) or pred == i or not conn or not _atom(e.get('predconnection', '')):
                raise ValueError('plan predecessor')
            out.append(f'<predecessor index="{pred}" connection="{e["predconnection"]}"/>')
        x, y, z = (_num(v, 1_000_000, 3) for v in e['pos'])
        yaw, pitch, roll = (_num(v, 360, 4) for v in e['rot'])
        out.append(f'<offset><position x="{x}" y="{y}" z="{z}"/><rotation yaw="{yaw}" pitch="{pitch}" roll="{roll}"/></offset>')
        groups = e.get('groups', [])
        if len(groups) > MAX_GROUPS:
            raise ValueError('plan loadout size')
        if groups:
            out.append('<upgrades><groups>')
            for g in groups:
                if (g['kind'] not in ('shields', 'turrets') or not _atom(g['macro'], True) or not PATH.fullmatch(g['path'])
                        or not _atom(g['group']) or not isinstance(g['exact'], int) or not 1 <= g['exact'] <= 1000):
                    raise ValueError('plan loadout')
                out.append(f'<{g["kind"]} macro="{g["macro"]}" path="{g["path"]}" group="{g["group"]}" exact="{g["exact"]}"/>')
            out.append('</groups></upgrades>')
        out.append('</entry>')
    out.append('</plan>')
    xml = ''.join(out)
    if len(xml) > MAX_BYTES:
        raise ValueError('plan size')
    return xml


ENTRY = re.compile(r'<entry index="(\d+)" macro="([^"]*)"(?: connection="([^"]*)")?>(.*?)</entry>')
PRED = re.compile(r'<predecessor index="(\d+)" connection="([^"]*)"/>')
OFFSET = re.compile(r'<offset><position x="([^"]*)" y="([^"]*)" z="([^"]*)"/>'
                    r'<rotation yaw="([^"]*)" pitch="([^"]*)" roll="([^"]*)"/></offset>')
GROUP = re.compile(r'<(\w+) macro="([^"]*)" path="([^"]*)" group="([^"]*)" exact="(\d+)"/>')


def parse_plan(xml):
    """Strict parse; re-encoding must reproduce the input byte for byte."""
    if not xml or len(xml) > MAX_BYTES or not xml.isascii() or '|' in xml or any(c < ' ' or c == '\x7f' for c in xml):
        raise ValueError('plan encoding')
    m = re.fullmatch(r'<plan id="([^"]*)" name="[^"]*">(.*)</plan>', xml)
    if not m or not _atom(m[1]):
        raise ValueError('plan header')
    entries = []
    for em in ENTRY.finditer(m[2]):
        body = em[4]
        e = dict(index=int(em[1]), macro=em[2], connection=em[3] or '', pred=0, predconnection='', groups=[])
        p = PRED.match(body)
        if p:
            e['pred'], e['predconnection'] = int(p[1]), p[2]
            body = body[p.end():]
        o = OFFSET.match(body)
        if not o:
            raise ValueError('plan offset')
        values = []
        for text in o.groups():
            if not NUMBER.fullmatch(text) or len(text) > 24:
                raise ValueError('plan number')
            values.append(float(text))
        e['pos'], e['rot'] = values[:3], values[3:]
        tail = body[o.end():]
        if tail:
            g = re.fullmatch(r'<upgrades><groups>(.*)</groups></upgrades>', tail)
            if not g:
                raise ValueError('plan loadout')
            e['groups'] = [dict(kind=k, macro=mac, path=pa, group=gr, exact=int(ex)) for k, mac, pa, gr, ex in GROUP.findall(g[1])]
        entries.append(e)
        if len(entries) > MAX_ENTRIES:
            raise ValueError('plan size')
    if encode_plan(m[1], entries) != xml:
        raise ValueError('plan not canonical')
    return m[1], entries


def parse_plot(text):
    if text == '-':
        return None
    f = text.split(',')
    if len(f) != 6 or any(not re.fullmatch(r'-?\d{1,7}', v) for v in f):
        raise ValueError('plot')
    values = [int(v) for v in f]
    if any(v < 0 for v in values[:3]):
        raise ValueError('plot size')
    return values


def parse_header(f):
    """kind|code|macro|sector|plot|option|entries|bytes|parts|digest (10 fields)."""
    kind, code, macro, sector, plot, option = f[:6]
    entries, size, parts, digest = (sequence(v) for v in f[6:10])
    if kind not in KINDS or not _atom(code) or not _atom(macro, True) or not _atom(sector, True):
        raise ValueError('build identity')
    parse_plot(plot)
    if digest > 0xffffffff or entries > MAX_ENTRIES or size > MAX_BYTES or parts > MAX_PARTS:
        raise ValueError('build size limit')
    if kind == 'EXPAND':
        if not entries or not size or parts != math.ceil(size / PART_SIZE):
            raise ValueError('build plan size')
        if option != '-':
            sequence(option)
    else:
        if entries or size or parts or digest != 1 or plot != '-':
            raise ValueError('build plan for non-expand')
        if (kind == 'REMOVEPLOT' and option not in ('0', '1')) or (kind == 'RECYCLE' and option != '-'):
            raise ValueError('build option')
    return tuple(f[:10])


class BuildIntents:
    def __init__(self, session, plan_inbox=None, logger=None):
        self.session = session
        self.plan_inbox = Path(plan_inbox) if plan_inbox else None
        self.logger = logger
        self.requests = {}

    def log(self, text):
        if self.logger:
            self.logger('BUILDREQ ' + text)

    def reply(self, lease, request, status, reason='ok', missing='-'):
        return f'BUILDREPLY|1|{lease}|{request}|{status}|{reason}|{missing}'.encode('ascii')

    def guest_view(self, r):
        state = r['state']
        if state == 'RECEIVING':
            need = [str(i) for i in range(1, int(r['header'][8]) + 1) if i not in r['parts']]
            return 'RECEIVING', 'ok', ','.join(need[:GUEST_MISSING]) or '-'
        if state in ('READY', 'PENDING'):
            return 'PENDING', 'ok', '-'
        return state, r['reason'], '-'

    def finish(self, request, status, reason):
        r = self.requests[request]
        if r['state'] in FINAL:
            return
        r['state'], r['reason'] = status, reason
        r['parts'] = {}
        self.log(f'{status.lower()} request={request} slot={r["slot"]} kind={r["header"][0]} station={r["header"][1]} reason={reason}')
        if r.get('file'):
            try:
                r['file'].unlink(missing_ok=True)
            except OSError:
                pass

    def receive(self, raw, address, now):
        s = self.session
        s.expire(now)
        if len(raw) > MAX_PACKET:
            return []
        try:
            f = raw.decode('ascii').split('|')
            if len(f) < 5 or f[1] != '1':
                return []
            slot, peer = s._peer(f[2], address)
            if not peer or not HEX.fullmatch(f[3]):
                return []
            request = f[3]
            r = self.requests.get(request)
            if f[0] == 'BUILDDONE' and len(f) == 6 and slot == 'p1':
                if (r and r['host'] == peer.lease and r['state'] == 'PENDING' and REASON.fullmatch(f[5])):
                    if f[4] == 'RECEIVED':
                        r['received'] = True
                    elif f[4] in ('ACCEPTED', 'REFUSED', 'UNCERTAIN'):
                        self.finish(request, f[4], f[5])
                return []
            if f[0] == 'BUILDPART' and len(f) == 6:
                if not r or r['slot'] != slot or r['state'] != 'RECEIVING':
                    return []
                index = sequence(f[4])
                chunk = f[5]
                if not 1 <= index <= int(r['header'][8]) or not 1 <= len(chunk) <= PART_SIZE:
                    return []
                old = r['parts'].get(index)
                if old is not None and old != chunk:
                    self.finish(request, 'REFUSED', 'part_conflict')
                    return []
                r['parts'][index] = chunk
                if len(r['parts']) == int(r['header'][8]):
                    self.assemble(request, r)
                return []
            if f[0] != 'BUILDREQ' or len(f) != 15:
                return []
            if not HEX.fullmatch(f[4]):
                return []
            header = parse_header(f[5:15])
            signature = (slot, f[4], header)
            if r:
                if r['signature'] != signature:
                    return [self.reply(peer.lease, request, 'REFUSED', 'request_conflict')]
                return [self.reply(peer.lease, request, *self.guest_view(r))]
            if slot == 'p1':
                return [self.reply(peer.lease, request, 'REFUSED', 'authority_builds_locally')]
            authority = s.peers.get('p1')
            if not authority or authority.lease != f[4]:
                return [self.reply(peer.lease, request, 'STALE', 'no_authority')]
            if any(x['slot'] == slot and x['state'] not in FINAL for x in self.requests.values()):
                return [self.reply(peer.lease, request, 'BUSY', 'busy')]
            # Never evict a retained ID into reuse; refuse new work at the cap instead.
            if len(self.requests) >= LIMIT:
                return [self.reply(peer.lease, request, 'BUSY', 'relay_full')]
            r = dict(signature=signature, slot=slot, host=f[4], header=header, parts={}, state='RECEIVING',
                     reason='ok', created=now, received=False, next_send=now, cursor=0, xml='')
            self.requests[request] = r
            self.log(f'received request={request} slot={slot} kind={header[0]} station={header[1]} '
                     f'entries={header[6]} bytes={header[7]} parts={header[8]}')
            if header[0] != 'EXPAND':
                r['state'] = 'READY'
            return [self.reply(peer.lease, request, *self.guest_view(r))]
        except (ValueError, UnicodeError, OverflowError):
            return []

    def assemble(self, request, r):
        xml = ''.join(r['parts'][i] for i in range(1, int(r['header'][8]) + 1))
        if len(xml) != int(r['header'][7]) or zlib.adler32(xml.encode('ascii')) != int(r['header'][9]):
            return self.finish(request, 'REFUSED', 'digest')
        try:
            plan_id, entries = parse_plan(xml)
        except ValueError:
            return self.finish(request, 'REFUSED', 'plan_invalid')
        if plan_id != 'x4lan_' + request or len(entries) != int(r['header'][6]):
            return self.finish(request, 'REFUSED', 'plan_identity')
        r['xml'] = xml
        r['state'] = 'READY'

    def write_inbox(self, request, r):
        if not self.plan_inbox:
            return '0'
        try:
            self.plan_inbox.mkdir(parents=True, exist_ok=True)
            path = self.plan_inbox / f'x4lan_{request}.xml'
            temporary = path.with_suffix('.tmp')
            temporary.write_text('<?xml version="1.0" encoding="utf-8"?>\n<plans>' + r['xml'] + '</plans>\n', encoding='ascii')
            temporary.replace(path)
            r['file'] = path
            return '1'
        except OSError as exc:
            self.log(f'inbox_failed request={request} error={type(exc).__name__}')
            return '0'

    def snapshots(self, now):
        host = self.session.peers.get('p1')
        out = []
        for request, r in self.requests.items():
            if r['state'] not in ('READY', 'PENDING'):
                continue
            if not host or host.lease != r['host']:
                # Never forwarded: nothing can have been built. Forwarded: unknown.
                if r['state'] == 'READY':
                    self.finish(request, 'STALE', 'no_authority')
                else:
                    self.finish(request, 'UNCERTAIN', 'authority_lost')
                continue
            if r['state'] == 'READY':
                r['inbox'] = self.write_inbox(request, r) if r['header'][0] == 'EXPAND' else '0'
                r['state'], r['forwarded'] = 'PENDING', now
                self.log(f'forwarded request={request} inbox={r["inbox"]}')
            if not r['received'] and now - r['forwarded'] > SILENT_AFTER:
                self.finish(request, 'UNCERTAIN', 'authority_silent')
                continue
            if now - r['forwarded'] > GIVE_UP_AFTER:
                self.finish(request, 'UNCERTAIN', 'authority_timeout')
                continue
            if now < r['next_send']:
                continue
            r['next_send'] = now + (1.0 if r['received'] else .25)
            h = r['header']
            out.append((host.address, ('|'.join(('BUILDEXEC', '1', host.lease, request, r['slot']) + h + (r['inbox'],))).encode('ascii')))
            if not r['received'] and r['xml']:
                total = int(h[8])
                for k in range(min(PART_BUDGET, total)):
                    i = (r['cursor'] + k) % total
                    chunk = r['xml'][i * PART_SIZE:(i + 1) * PART_SIZE]
                    out.append((host.address, f'BUILDXPART|1|{host.lease}|{request}|{i + 1}|{chunk}'.encode('ascii')))
                r['cursor'] = (r['cursor'] + PART_BUDGET) % total
        return out
