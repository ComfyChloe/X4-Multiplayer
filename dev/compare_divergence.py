"""Compare two DIV1 divergence-probe logs (host/guest) offline. Never launches X4.

Usage:
  python dev/compare_divergence.py HOST.log GUEST.log [--csv ticks.csv]
         [--summary-csv summary.csv] [--report report.txt] [options]
  python dev/compare_divergence.py HOST.log GUEST.log --statehash [--report report.txt]

DIV1 logs come from extension/x4_lan_lab/ui/divergence_probe.lua and align by
SCHEDULED saved-game time. With --statehash, observer frames from engine_hooks.lua
are aligned instead and the first differing bucket is reported.

Ship matching, per aligned sample and only over sectors completely sampled in both logs:
 1. id:   equal native component ID AND equal macro AND (equal idcode when both have one).
          Equal ID with different macro/code is an id_conflict and is NOT matched.
 2. code: unique (macro, owner, idcode) among the still-unmatched ships of the sample.
          idcodes come from the save for preloaded ships; ships spawned after load
          normally get unrelated codes in each game and fall through to step 3.
 3. pose: same sampled sector and same (macro, owner); greedy nearest pairs within
          --pose-tolerance metres. This is a guess, not identity: pose matches are
          reported separately and excluded from the identity-backed error figures.
Within one log a component ID whose macro or idcode changes is treated as a NEW
object (generation), so engine ID reuse never joins two different ships.
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from pathlib import Path
from urllib.parse import unquote

MARKER = '[X4LAN] DIV1 '
MAX_LINE = 4096
TOKEN = re.compile(r'([a-z_]+)=(\S*)')
BUCKETS = ((0, 60, '0-1min'), (60, 300, '1-5min'), (300, 900, '5-15min'), (900, 3600, '15-60min'),
           (3600, math.inf, '60min+'))
SHIP_NUMBERS = ('x', 'y', 'z', 'yaw', 'pitch', 'roll', 'hull', 'shield')
SHIP_REQUIRED = ('n', 's', 'id', 'macro', 'owner') + SHIP_NUMBERS


class ProbeLogError(ValueError):
    pass


def _number(value):
    if value in (None, ''):
        return None
    number = float(value)
    if not math.isfinite(number) or abs(number) > 1e12:
        raise ValueError('non-finite or out-of-range number')
    return number


def parse_line(line):
    """Return (kind, fields) for one DIV1 line, None for other lines; raise ValueError if malformed."""
    at = line.find(MARKER)
    if at < 0:
        return None
    if len(line) > MAX_LINE:
        raise ValueError('line exceeds limit')
    body = line[at + len(MARKER):].strip()
    kind, _, rest = body.partition(' ')
    if not re.fullmatch(r'[A-Z_]{2,16}', kind):
        raise ValueError('invalid record type')
    fields = {}
    for key, value in TOKEN.findall(rest):
        if key in fields:
            raise ValueError('duplicate field ' + key)
        fields[key] = unquote(value, errors='replace')
    if kind == 'SHIP':
        missing = [k for k in SHIP_REQUIRED if k not in fields]
        if missing or not fields['n'].isdigit() or not fields['id'].isdigit():
            raise ValueError('incomplete ship record')
        for k in SHIP_NUMBERS:
            fields[k] = _number(fields[k])
        if fields['x'] is None or fields['y'] is None or fields['z'] is None:
            raise ValueError('ship without position')
        fields['n'] = int(fields['n'])
    elif kind == 'TICK':
        if not fields.get('n', '').isdigit() or _number(fields.get('gt')) is None:
            raise ValueError('incomplete tick record')
        fields['n'] = int(fields['n'])
        fields['gt'] = _number(fields['gt'])
        fields['age'] = _number(fields.get('age'))
        fields['rt'] = _number(fields.get('rt'))
    elif kind in ('SECTOR', 'END'):
        if not fields.get('n', '').isdigit():
            raise ValueError('incomplete %s record' % kind.lower())
        fields['n'] = int(fields['n'])
        for k in ('found', 'requested', 'written', 'gone', 'dropped', 'errors', 'ships'):
            if k in fields:
                if not fields[k].isdigit():
                    raise ValueError('invalid count ' + k)
                fields[k] = int(fields[k])
    return kind, fields


class Sample:
    """One recorder tick: scheduled game time plus complete/incomplete sector samples."""
    def __init__(self, tick):
        self.n = tick['n']; self.gt = tick['gt']; self.age = tick['age']; self.rt = tick['rt']
        self.psector = tick.get('psector', ''); self.ships = {}; self.sectors = {}; self.ended = False

    def complete(self, sector):
        s = self.sectors.get(sector)
        return bool(s and s['found'] == s['requested'] and s['dropped'] == 0 and s['errors'] == 0
                    and s['written'] + s['gone'] == s['requested']
                    and len(self.ships.get(sector, ())) == s['written'])


class ProbeLog:
    def __init__(self):
        self.configs = []; self.runs = [[]]; self.malformed = 0; self.orphans = 0; self.duplicates = 0
        self.stopped = []; self.missing = set(); self.errors = 0


def parse_log(lines):
    """Parse an iterable of log lines. A backwards scheduled game time starts a new run."""
    log = ProbeLog(); current = None; by_n = {}
    for line in lines:
        try:
            parsed = parse_line(line)
        except ValueError:
            log.malformed += 1; continue
        if not parsed:
            continue
        kind, f = parsed
        if kind == 'CFG':
            log.configs.append(f)
        elif kind == 'TICK':
            run = log.runs[-1]
            if run and f['gt'] <= run[-1].gt:
                log.runs.append([]); run = log.runs[-1]; by_n = {}
            current = Sample(f); run.append(current); by_n[f['n']] = current
        elif kind == 'SHIP':
            sample = by_n.get(f['n'])
            if not sample or sample.ended:
                log.orphans += 1; continue
            rows = sample.ships.setdefault(f['s'], {})
            if f['id'] in rows:
                log.duplicates += 1; continue
            rows[f['id']] = f
        elif kind == 'SECTOR':
            sample = by_n.get(f['n'])
            if not sample or sample.ended or not all(k in f for k in ('sector', 'found', 'requested', 'written', 'gone', 'dropped', 'errors')):
                log.orphans += 1; continue
            sample.sectors[f['sector']] = f
        elif kind == 'END':
            sample = by_n.get(f['n'])
            if sample:
                sample.ended = True
        elif kind == 'STOPPED':
            log.stopped.append(f.get('reason', ''))
        elif kind == 'MISSING':
            log.missing.add(f.get('sector', ''))
        elif kind == 'ERROR':
            log.errors += 1
    log.runs = [r for r in log.runs if r]
    return log


def read_log(path):
    with open(path, encoding='utf-8', errors='replace') as stream:
        return parse_log(stream)


def assign_generations(run):
    """Per log: (id, generation) changes when an ID reappears with another macro/code."""
    last = {}
    for sample in run:
        for rows in sample.ships.values():
            for ship in rows.values():
                identity = (ship['macro'], ship.get('code', ''))
                previous = last.get(ship['id'])
                if previous is None:
                    generation = 0
                elif previous[0] != identity:
                    generation = previous[1] + 1
                else:
                    generation = previous[1]
                last[ship['id']] = (identity, generation)
                ship['gen'] = generation


def _distance(a, b):
    return math.dist((a['x'], a['y'], a['z']), (b['x'], b['y'], b['z']))


def match_sample(host, guest, sectors, pose_tolerance, include_player_owned=False):
    """Return (pairs, host_only, guest_only, conflicts) over the given sectors."""
    def ships(sample):
        out = []
        for sector in sectors:
            for ship in sample.ships.get(sector, {}).values():
                if include_player_owned or ship['owner'] != 'player':
                    out.append(ship)
        return out
    hs = ships(host); gs = ships(guest)
    pairs = []; conflicts = 0
    guest_by_id = {g['id']: g for g in gs}
    used_h = set(); used_g = set()
    for h in hs:
        g = guest_by_id.get(h['id'])
        if g is None:
            continue
        codes_differ = h.get('code') and g.get('code') and h['code'] != g['code']
        if h['macro'] != g['macro'] or codes_differ:
            conflicts += 1; continue
        pairs.append((h, g, 'id')); used_h.add(id(h)); used_g.add(id(g))

    def by_code(rows, used):
        index = {}
        for r in rows:
            if id(r) not in used and r.get('code'):
                index.setdefault((r['macro'], r['owner'], r['code']), []).append(r)
        return {k: v[0] for k, v in index.items() if len(v) == 1}
    hc = by_code(hs, used_h); gc = by_code(gs, used_g)
    for k in sorted(hc.keys() & gc.keys()):
        h, g = hc[k], gc[k]
        pairs.append((h, g, 'code')); used_h.add(id(h)); used_g.add(id(g))

    candidates = []
    rest_g = [g for g in gs if id(g) not in used_g]
    for h in hs:
        if id(h) in used_h:
            continue
        for g in rest_g:
            if g['s'] == h['s'] and g['macro'] == h['macro'] and g['owner'] == h['owner']:
                d = _distance(h, g)
                if d <= pose_tolerance:
                    candidates.append((d, h['id'], g['id'], h, g))
    candidates.sort(key=lambda c: c[:3])
    for _, _, _, h, g in candidates:
        if id(h) in used_h or id(g) in used_g:
            continue
        pairs.append((h, g, 'pose')); used_h.add(id(h)); used_g.add(id(g))
    host_only = [h for h in hs if id(h) not in used_h]
    guest_only = [g for g in gs if id(g) not in used_g]
    return pairs, host_only, guest_only, conflicts


def quantile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def bucket(elapsed):
    for low, high, name in BUCKETS:
        if low <= elapsed < high:
            return name
    return BUCKETS[0][2]


def _speeds(run):
    """Approximate speed per (id, gen) from consecutive samples in one log (m/s)."""
    previous = {}; speeds = {}
    for sample in run:
        for rows in sample.ships.values():
            for ship in rows.values():
                k = (ship['id'], ship['gen']); p = previous.get(k)
                if p and p[0] < sample.gt and p[1]['sector'] == ship['sector']:
                    speeds[(k, sample.n)] = _distance(p[1], ship) / (sample.gt - p[0])
                previous[k] = (sample.gt, ship)
    return speeds


def align(host_run, guest_run):
    guest_by_time = {round(s.gt, 1): s for s in guest_run}
    return [(h, guest_by_time[round(h.gt, 1)]) for h in host_run if round(h.gt, 1) in guest_by_time]


def compare(host_log, guest_log, pose_tolerance=1000.0, hull_tolerance=0.5, include_player_owned=False,
            run_index=-1):
    if not host_log.runs or not guest_log.runs:
        raise ProbeLogError('a log contains no DIV1 samples')
    host_run = host_log.runs[run_index]; guest_run = guest_log.runs[run_index]
    assign_generations(host_run); assign_generations(guest_run)
    aligned = align(host_run, guest_run)
    if not aligned:
        raise ProbeLogError('no common scheduled game times; check both probes use the same interval and save')
    host_speed = _speeds(host_run)
    start = aligned[0][0].gt
    rows = []; skipped = []
    for h, g in aligned:
        sectors = sorted(set(h.sectors) | set(g.sectors))
        skew = abs(h.age - g.age) if h.age is not None and g.age is not None else None
        for sector in sectors:
            if not (h.complete(sector) and g.complete(sector)):
                skipped.append((h.gt, sector)); continue
            hp = h.sectors[sector]['player'] == '1'; gp = g.sectors[sector]['player'] == '1'
            pairs, host_only, guest_only, conflicts = match_sample(h, g, [sector], pose_tolerance, include_player_owned)
            identity = [(a, b) for a, b, how in pairs if how != 'pose']
            errors = [_distance(a, b) for a, b in identity if a['sector'] == b['sector']]
            pose_errors = [_distance(a, b) for a, b, how in pairs if how == 'pose']
            floors = []
            if skew is not None:
                for a, _ in identity:
                    speed = host_speed.get(((a['id'], a['gen']), h.n))
                    if speed is not None:
                        floors.append(speed * skew)
            hull = [abs(a['hull'] - b['hull']) for a, b in identity if a['hull'] is not None and b['hull'] is not None]
            shield = [abs(a['shield'] - b['shield']) for a, b in identity if a['shield'] is not None and b['shield'] is not None]
            orders = [(a.get('order', ''), a.get('dorder', '')) != (b.get('order', ''), b.get('dorder', '')) for a, b in identity]
            docks = [a.get('dock', '') != b.get('dock', '') for a, b in identity]
            elapsed = h.gt - start
            rows.append(dict(
                gt=h.gt, elapsed_s=elapsed, bucket=bucket(elapsed), sector=sector,
                category='player' if hp or gp else 'none', player=('both' if hp and gp else 'host' if hp else 'guest' if gp else 'none'),
                host_ships=h.sectors[sector]['written'], guest_ships=g.sectors[sector]['written'],
                matched_id=sum(1 for p in pairs if p[2] == 'id'), matched_code=sum(1 for p in pairs if p[2] == 'code'),
                matched_pose=sum(1 for p in pairs if p[2] == 'pose'), host_only=len(host_only), guest_only=len(guest_only),
                id_conflicts=conflicts, skew_s=skew, errors=errors, pose_errors=pose_errors, floors=floors,
                hull_mismatch=sum(1 for v in hull if v > hull_tolerance), hull_max=max(hull, default=None),
                shield_mismatch=sum(1 for v in shield if v > hull_tolerance),
                order_mismatch=sum(orders), dock_mismatch=sum(docks), identity_pairs=len(identity)))
    return dict(rows=rows, skipped=skipped, aligned=len(aligned), host_samples=len(host_run), guest_samples=len(guest_run),
                host_runs=len(host_log.runs), guest_runs=len(guest_log.runs), host=host_log, guest=guest_log,
                pose_tolerance=pose_tolerance, hull_tolerance=hull_tolerance)


def aggregate(rows):
    keys = ('host_ships', 'guest_ships', 'matched_id', 'matched_code', 'matched_pose', 'host_only', 'guest_only',
            'id_conflicts', 'hull_mismatch', 'shield_mismatch', 'order_mismatch', 'dock_mismatch', 'identity_pairs')
    total = {k: sum(r[k] for r in rows) for k in keys}
    errors = [e for r in rows for e in r['errors']]; pose = [e for r in rows for e in r['pose_errors']]
    floors = [e for r in rows for e in r['floors']]; skews = [r['skew_s'] for r in rows if r['skew_s'] is not None]
    matched = total['matched_id'] + total['matched_code'] + total['matched_pose']
    union = matched + total['host_only'] + total['guest_only']
    identity_union = total['matched_id'] + total['matched_code'] + total['host_only'] + total['guest_only'] + total['matched_pose']
    total.update(samples=len(rows),
                 existence_match=matched / union if union else None,
                 identity_match=(total['matched_id'] + total['matched_code']) / identity_union if identity_union else None,
                 pos_median=quantile(errors, .5), pos_p90=quantile(errors, .9), pos_max=max(errors, default=None),
                 pose_match_median=quantile(pose, .5), skew_floor_median=quantile(floors, .5), skew_floor_p90=quantile(floors, .9),
                 skew_median=quantile(skews, .5), skew_max=max(skews, default=None),
                 hull_rate=total['hull_mismatch'] / total['identity_pairs'] if total['identity_pairs'] else None,
                 order_rate=total['order_mismatch'] / total['identity_pairs'] if total['identity_pairs'] else None)
    return total


def _fmt(value, pattern='%.1f'):
    if value is None:
        return '-'
    if isinstance(value, float):
        return pattern % value
    return str(value)


def _pct(value):
    return '-' if value is None else '%.1f%%' % (100 * value)


def summary_rows(result):
    groups = {}
    for r in result['rows']:
        for key in ((r['bucket'], r['category'], '*'), (r['bucket'], r['category'], r['sector']), ('all', r['category'], '*')):
            groups.setdefault(key, []).append(r)
    order = {name: i for i, (_, _, name) in enumerate(BUCKETS)}; order['all'] = len(order)
    out = []
    for key in sorted(groups, key=lambda k: (order[k[0]], k[1], k[2] != '*', k[2])):
        agg = aggregate(groups[key]); agg.update(bucket=key[0], category=key[1], sector=key[2]); out.append(agg)
    return out


def report(result):
    h, g = result['host'], result['guest']
    lines = ['X4 DIVERGENCE PROBE COMPARISON (offline; says nothing about runs not in these logs)',
             'Samples: host %d guest %d aligned %d; runs host %d guest %d (last run used)'
             % (result['host_samples'], result['guest_samples'], result['aligned'], result['host_runs'], result['guest_runs']),
             'Sector samples compared %d, skipped incomplete/one-sided %d' % (len(result['rows']), len(result['skipped'])),
             'Log hygiene: malformed host/guest %d/%d, orphan %d/%d, duplicate %d/%d, probe errors %d/%d'
             % (h.malformed, g.malformed, h.orphans, g.orphans, h.duplicates, g.duplicates, h.errors, g.errors),
             'Pose tolerance %.0f m (pose matches are guesses); hull/shield tolerance %.2f points'
             % (result['pose_tolerance'], result['hull_tolerance'])]
    for name, log in (('host', h), ('guest', g)):
        if log.stopped:
            lines.append('WARNING %s probe STOPPED: %s' % (name, '; '.join(log.stopped)))
        if log.missing:
            lines.append('WARNING %s sectors not found: %s' % (name, ', '.join(sorted(log.missing))))
        if any(c.get('world_mode') == 'replica' for c in log.configs):
            lines.append('WARNING %s ran world_mode=replica: its sector NPCs are host replicas, not an independent simulation' % name)
    lines += ['', 'Categories: player = a player was in the sector in either game; none = no player in either.',
              'identity = id+code matches only; pos error over identity matches in the same sector.',
              'skew floor = estimated speed x sampling-time skew (error expected from sampling alone).', '']
    header = ('%-8s %-6s %-28s %5s %7s %7s %6s %6s %6s %6s %8s %8s %8s %8s %8s %7s %7s'
              % ('bucket', 'cat', 'sector', 'samp', 'exist%', 'ident%', 'id', 'code', 'pose', 'h-only', 'g-only',
                 'pos_med', 'pos_p90', 'pos_max', 'floor50', 'hull%', 'order%'))
    lines.append(header); lines.append('-' * len(header))
    for r in summary_rows(result):
        lines.append('%-8s %-6s %-28s %5d %7s %7s %6d %6d %6d %6d %8d %8s %8s %8s %8s %7s %7s'
                     % (r['bucket'], r['category'], r['sector'][:28], r['samples'], _pct(r['existence_match']),
                        _pct(r['identity_match']), r['matched_id'], r['matched_code'], r['matched_pose'], r['host_only'],
                        r['guest_only'], _fmt(r['pos_median']), _fmt(r['pos_p90']), _fmt(r['pos_max']),
                        _fmt(r['skew_floor_median']), _pct(r['hull_rate']), _pct(r['order_rate'])))
    conflicts = sum(r['id_conflicts'] for r in result['rows'])
    lines += ['', 'ID conflicts (same component ID, different macro/code; not matched): %d' % conflicts,
              'Sampling skew (s): median %s max %s' % (_fmt(aggregate(result['rows'])['skew_median'], '%.3f') if result['rows'] else '-',
                                                     _fmt(aggregate(result['rows'])['skew_max'], '%.3f') if result['rows'] else '-')]
    return '\n'.join(lines) + '\n'


STATEHASH_MARKER = '[X4LAN] STATEHASH '
HASH_BUCKETS = ('identity', 'pose', 'health', 'rng', 'time')
HASH16 = re.compile(r'[0-9a-f]{16}\Z')


def parse_statehash_line(line):
    at = line.find(STATEHASH_MARKER)
    if at < 0:
        return None
    if len(line) > MAX_LINE:
        raise ProbeLogError('STATEHASH line exceeds limit')
    fields = {}
    for token in line[at + len(STATEHASH_MARKER):].strip().split():
        key, separator, value = token.partition('=')
        if not separator or not re.fullmatch(r'[a-z_]+', key) or key in fields:
            raise ProbeLogError('invalid STATEHASH field')
        fields[key] = value
    required = {'run', 'session', 'frame', 'complete', 'gt', 'dt'}
    if not required <= fields.keys() or not fields['frame'].isdigit() or int(fields['frame']) < 1:
        raise ProbeLogError('incomplete STATEHASH record')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', fields['run']) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', fields['session']):
        raise ProbeLogError('invalid STATEHASH run/session')
    if 'seed' in fields and not HASH16.fullmatch(fields['seed']):
        raise ProbeLogError('invalid STATEHASH session seed')
    for key in ('gt', 'dt'):
        fields[key] = _number(fields[key])
        if fields[key] is None:
            raise ProbeLogError('invalid STATEHASH time')
    fields['frame'] = int(fields['frame'])
    if fields['complete'] == '1':
        if not {'h', 'ships', 'weak', *HASH_BUCKETS} <= fields.keys() or 'reason' in fields:
            raise ProbeLogError('incomplete STATEHASH payload')
        if any(not HASH16.fullmatch(fields[key]) for key in ('h', *HASH_BUCKETS)):
            raise ProbeLogError('invalid STATEHASH digest')
        for key in ('ships', 'weak'):
            if not fields[key].isdigit():
                raise ProbeLogError('invalid STATEHASH count')
            fields[key] = int(fields[key])
    elif fields['complete'] == '0':
        if not fields.get('reason') or {'h', *HASH_BUCKETS} & fields.keys():
            raise ProbeLogError('invalid incomplete STATEHASH record')
    else:
        raise ProbeLogError('invalid STATEHASH completeness')
    return fields


def read_statehash(path):
    samples = []
    identity = None
    previous = 0
    with open(path, encoding='utf-8', errors='replace') as stream:
        for line in stream:
            if STATEHASH_MARKER not in line:
                continue
            sample = parse_statehash_line(line)
            current = (sample['run'], sample['session'], sample.get('seed', ''))
            if identity is None:
                identity = current
            elif current != identity:
                raise ProbeLogError('mixed run, session or seed in STATEHASH log')
            if sample['frame'] <= previous:
                raise ProbeLogError('duplicate or regressing STATEHASH frame')
            previous = sample['frame']
            samples.append(sample)
    if not samples:
        raise ProbeLogError('a log contains no STATEHASH samples')
    return samples


def compare_statehash(host, guest):
    if (host[0]['run'], host[0]['session'], host[0].get('seed', '')) != (guest[0]['run'], guest[0]['session'], guest[0].get('seed', '')):
        raise ProbeLogError('STATEHASH run, session or seed differs')
    host_by_frame = {sample['frame']: sample for sample in host}
    guest_by_frame = {sample['frame']: sample for sample in guest}
    events = []
    first = None
    compared = 0
    baseline = None
    bucket_stats = {key: dict(first=None, differences=0) for key in HASH_BUCKETS}
    for frame in sorted(host_by_frame.keys() | guest_by_frame.keys()):
        h, g = host_by_frame.get(frame), guest_by_frame.get(frame)
        if not h or not g:
            events.append((frame, 'missing', 'host' if not h else 'guest'))
            continue
        if h['complete'] != '1' or g['complete'] != '1':
            events.append((frame, 'incomplete', h.get('reason', '') if h['complete'] == '0' else g.get('reason', '')))
            continue
        compared += 1
        if baseline is None:
            baseline = h['gt']
        differing = [key for key in HASH_BUCKETS if h[key] != g[key]]
        for key in differing:
            bucket_stats[key]['differences'] += 1
            if bucket_stats[key]['first'] is None:
                bucket_stats[key]['first'] = dict(frame=frame, gt=h['gt'], elapsed_s=h['gt'] - baseline)
        if differing and first is None:
            first = dict(frame=frame, buckets=differing, gt=h['gt'], elapsed_s=h['gt'] - baseline,
                         host=h['h'], guest=g['h'])
        if bool(differing) != (h['h'] != g['h']):
            raise ProbeLogError(f'inconsistent combined STATEHASH at frame {frame}')
    return dict(first=first, events=events, compared=compared, frames=len(host_by_frame.keys() | guest_by_frame.keys()),
                missing=sum(event[1] == 'missing' for event in events),
                incomplete=sum(event[1] == 'incomplete' for event in events),
                weak=max((sample.get('weak', 0) for sample in host + guest), default=0),
                bucket_stats=bucket_stats, run=host[0]['run'], session=host[0]['session'])


def statehash_report(result):
    lines = ['X4 STATEHASH COMPARISON (offline; observer frames, not save-persistent engine frames)',
             f"Run {result['run']} session {result['session']}: {result['compared']} complete comparisons over {result['frames']} frames"]
    first = result['first']
    if first:
        lines.append('FIRST DIVERGENCE frame=%d game_time=%.9f elapsed=%.9f buckets=%s' %
                     (first['frame'], first['gt'], first['elapsed_s'], ','.join(first['buckets'])))
    else:
        lines.append('No hash divergence in complete shared frames')
    for key, stats in result['bucket_stats'].items():
        initial = stats['first']
        lines.append('BUCKET %s first=%s differences=%d/%d' %
                     (key, initial['frame'] if initial else 'none', stats['differences'], result['compared']))
    if result['missing'] or result['incomplete']:
        lines.append('WARNING missing frames=%d incomplete frames=%d; these are not equality evidence' %
                     (result['missing'], result['incomplete']))
    if result['weak']:
        lines.append('WARNING weak local identities present in at least one capture: max count=%d' % result['weak'])
    if result['events']:
        lines.append('First alignment events: ' + '; '.join('%d %s %s' % event for event in result['events'][:10]))
    return '\n'.join(lines) + '\n'


TICK_COLUMNS = ('gt', 'elapsed_s', 'bucket', 'sector', 'category', 'player', 'host_ships', 'guest_ships', 'matched_id',
                'matched_code', 'matched_pose', 'host_only', 'guest_only', 'id_conflicts', 'skew_s', 'pos_median', 'pos_p90',
                'pos_max', 'pose_match_median', 'skew_floor_median', 'hull_mismatch', 'hull_max', 'shield_mismatch',
                'order_mismatch', 'dock_mismatch', 'identity_pairs')
SUMMARY_COLUMNS = ('bucket', 'category', 'sector', 'samples', 'existence_match', 'identity_match', 'host_ships', 'guest_ships',
                   'matched_id', 'matched_code', 'matched_pose', 'host_only', 'guest_only', 'id_conflicts', 'pos_median',
                   'pos_p90', 'pos_max', 'pose_match_median', 'skew_floor_median', 'skew_floor_p90', 'skew_median', 'skew_max',
                   'hull_mismatch', 'hull_rate', 'shield_mismatch', 'order_mismatch', 'order_rate', 'dock_mismatch', 'identity_pairs')


def write_tick_csv(result, path):
    with open(path, 'w', newline='', encoding='utf-8') as stream:
        w = csv.DictWriter(stream, fieldnames=TICK_COLUMNS); w.writeheader()
        for r in result['rows']:
            row = {k: r.get(k) for k in TICK_COLUMNS}
            row.update(pos_median=quantile(r['errors'], .5), pos_p90=quantile(r['errors'], .9), pos_max=max(r['errors'], default=None),
                       pose_match_median=quantile(r['pose_errors'], .5), skew_floor_median=quantile(r['floors'], .5))
            w.writerow(row)


def write_summary_csv(result, path):
    with open(path, 'w', newline='', encoding='utf-8') as stream:
        w = csv.DictWriter(stream, fieldnames=SUMMARY_COLUMNS, extrasaction='ignore'); w.writeheader()
        for r in summary_rows(result):
            w.writerow(r)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('host'); parser.add_argument('guest')
    parser.add_argument('--csv', help='per sample/sector CSV output')
    parser.add_argument('--summary-csv', help='per bucket/category/sector CSV output')
    parser.add_argument('--report', help='also write the text report to this file')
    parser.add_argument('--pose-tolerance', type=float, default=1000.0, help='metres for fallback pose matching (default 1000)')
    parser.add_argument('--hull-tolerance', type=float, default=0.5, help='hull/shield percentage points (default 0.5)')
    parser.add_argument('--include-player-owned', action='store_true', help='also compare player-owned ships')
    parser.add_argument('--run', type=int, default=-1, help='run index when a log restarted its game clock (default last)')
    parser.add_argument('--statehash', action='store_true', help='compare STATEHASH frames/buckets instead of DIV1 samples')
    args = parser.parse_args(argv)
    if not (0 < args.pose_tolerance <= 100000) or not (0 <= args.hull_tolerance <= 100):
        parser.error('tolerance outside bounds')
    if args.statehash and (args.csv or args.summary_csv):
        parser.error('DIV1 CSV outputs are unavailable with --statehash')
    try:
        if args.statehash:
            result = compare_statehash(read_statehash(args.host), read_statehash(args.guest))
            text = statehash_report(result)
        else:
            result = compare(read_log(args.host), read_log(args.guest), args.pose_tolerance, args.hull_tolerance,
                             args.include_player_owned, args.run)
            text = report(result)
    except (ProbeLogError, IndexError) as error:
        print('compare_divergence: ' + str(error), file=sys.stderr); return 2
    sys.stdout.write(text)
    if args.report:
        Path(args.report).write_text(text, encoding='utf-8')
    if args.csv:
        write_tick_csv(result, args.csv)
    if args.summary_csv:
        write_summary_csv(result, args.summary_csv)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
