"""Continuous playtest recorder for one X4 Multiplayer package (host or guest, Windows or Linux).

Start it separately before X4 and leave it running for the whole playtest; Ctrl+C ends the run.
It only READS the package and the profile. It never changes the test profile, saves, Workshop
or the running server, and it never calls the launcher's diagnostics (they rewrite log files).

Every poll it copies what is new in the engine log (<profile>/x4lan-debug.txt), the relay JSONL
streams (logs/server-*.jsonl) and the launcher's capture copies into a run folder of its own:

  recordings/<UTC start>-<role>-<pid>/
    events.jsonl            one JSON object per event: utc, mono, seq, kind, ...
    sources/<name>.<n>.log  copied text; n starts a new segment after a truncation/replacement
    snapshots/...           copies of launcher capture files (world/capture, logs/before-*)
    manifest.json           written at the end: run info and SHA-256 of every output file

Only complete lines are copied. The session key (session.json token and bubble) never reaches
disk. Other 32-hex values (leases, nonces) become stable pseudonyms keyed by the session key, so
a host and a guest recording of the same session use the same pseudonyms. World seeds stay
readable. Recording stops copying content (events continue) at --max-mb.

'export' zips one run and also masks IPv4 addresses and home-folder user names, for sharing.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
import zipfile

VERSION = 1
HEX32 = re.compile(rb'(?<![0-9A-Fa-f])(?<!seed=)[0-9A-Fa-f]{32}(?![0-9A-Fa-f])')
IPV4 = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
HOME = re.compile(r'(?i)((?:[A-Z]:)?[\\/]+(?:Users|home)[\\/]+)[^\\/\s"\']+')
HEADER = 256            # bytes compared to detect a file replaced without shrinking
HEARTBEAT = 10.0        # seconds between progress events
STALE = 5.0             # server-status.json older than this while running is reported once


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def read_json(path):
    try: return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError): return None


class Redactor:
    def __init__(self, secrets):
        self.secrets = sorted({s.encode() for s in secrets if s}, key=len, reverse=True)
        self.key = b'|'.join(self.secrets) or b'x4lan-no-session'

    def __call__(self, data):
        for secret in self.secrets:
            data = data.replace(secret, b'[SESSION KEY]')
        return HEX32.sub(lambda m: b'<id:' + hashlib.sha256(self.key + m.group(0).lower()).hexdigest()[:10].encode() + b'>', data)


class Tail:
    """Append-only copy of one growing text file; survives truncation, replacement and absence."""
    def __init__(self, recorder, name, path):
        self.r, self.name, self.path = recorder, name, Path(path)
        self.offset, self.segment, self.head, self.pending, self.present, self.bytes = 0, 0, b'', b'', None, 0

    def poll(self):
        try: st = self.path.stat()
        except OSError: st = None
        if st is None:
            if self.present is not False: self.r.event('missing', source=self.name, path=str(self.path))
            self.present = False
            return
        if self.present is not True: self.r.event('present', source=self.name, path=str(self.path), size=st.st_size)
        self.present = True
        try:
            with open(self.path, 'rb') as f:              # opened per poll; no handle is kept
                head = f.read(HEADER)
                if self.segment and (st.st_size < self.offset or head[:len(self.head)] != self.head[:len(head)]):
                    self.r.event('truncated' if st.st_size < self.offset else 'replaced', source=self.name,
                                 old_size=self.offset, size=st.st_size)
                    self.flush(); self.offset = 0; self.segment += 1
                if not self.segment: self.segment = 1
                self.head = head
                f.seek(self.offset)
                data = f.read(max(0, st.st_size - self.offset))
        except OSError as exc:
            self.r.event('read_error', source=self.name, error=type(exc).__name__)
            return
        self.offset += len(data)
        data = self.pending + data
        cut = data.rfind(b'\n') + 1
        self.pending = data[cut:]
        self.write(data[:cut])

    def write(self, data):
        if data and self.r.allow(len(data)):
            with open(self.r.out / 'sources' / f'{self.name}.{self.segment}.log', 'ab') as f:
                f.write(self.r.redact(data))
            self.bytes += len(data)

    def flush(self):
        self.write(self.pending); self.pending = b''


class Recorder:
    def __init__(self, package, profile=None, out=None, max_mb=1024, clock=time.monotonic):
        self.package, self.clock = Path(package).resolve(), clock
        meta = read_json(self.package / 'package.json') or {}
        session = read_json(self.package / 'session.json') or {}
        install = read_json(self.package / 'installation.json') or {}
        self.role = {'p1': 'host', 'p2': 'guest'}.get(meta.get('slot'), 'unconfigured')
        self.redact = Redactor([session.get('token'), session.get('bubble')])
        self.profile = Path(profile) if profile else Path(install['profile']) if install.get('profile') else None
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        root = Path(out) if out else self.package / 'recordings'
        self.out = root / f'{stamp}-{self.role}-{os.getpid()}'
        self.out.mkdir(parents=True)                      # a fresh folder; never reused
        (self.out / 'sources').mkdir()
        self.limit, self.used, self.limited = max_mb << 20, 0, False
        self.seq, self.tails, self.snaps, self.status = 0, {}, {}, None
        self.started, self.beat = utc_now(), self.clock()
        self.info = dict(recorder=VERSION, role=self.role, slot=meta.get('slot'), version=meta.get('version'),
                         steam_build=meta.get('steam_build'), game_version=meta.get('game_version'),
                         platform=platform.platform(), python=sys.version.split()[0],
                         package=str(self.package), profile=str(self.profile) if self.profile else None)
        self.events = open(self.out / 'events.jsonl', 'a', encoding='utf-8', buffering=1)
        self.event('start', **self.info)
        if self.profile is None: self.event('no_profile', note='installation.json has no profile; engine log not followed')

    def event(self, kind, **fields):
        self.seq += 1
        row = dict(utc=utc_now(), mono=round(self.clock(), 3), seq=self.seq, kind=kind, **fields)
        self.events.write(json.dumps(row, ensure_ascii=True, sort_keys=False) + '\n')

    def allow(self, size):
        if self.used + size > self.limit:
            if not self.limited: self.event('storage_limit', limit_mb=self.limit >> 20)
            self.limited = True
            return False
        self.used += size
        return True

    def sources(self):
        found = {}
        if self.profile: found['engine'] = self.profile / 'x4lan-debug.txt'
        for path in sorted((self.package / 'logs').glob('server-*.jsonl')):
            found['relay-' + path.stem[len('server-'):]] = path
        return found

    def snapshot_paths(self):
        for pattern in ('world/capture/*.log', 'world/failed-capture-*/*.log', 'logs/before-world-start-*.txt'):
            yield from sorted(self.package.glob(pattern))

    def poll(self):
        for name, path in self.sources().items():
            if name not in self.tails: self.tails[name] = Tail(self, name, path)
        for tail in self.tails.values(): tail.poll()
        for path in self.snapshot_paths():
            try: st = path.stat()
            except OSError: continue
            key = (st.st_size, st.st_mtime_ns)
            if self.snaps.get(path) == key: continue
            self.snaps[path] = key
            rel = path.relative_to(self.package)
            try: data = path.read_bytes()
            except OSError: continue
            self.event('snapshot', path=rel.as_posix(), size=st.st_size, sha256=hashlib.sha256(data).hexdigest())
            if self.allow(len(data)):
                target = self.out / 'snapshots' / rel.parent / f'{st.st_mtime_ns}-{rel.name}'
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.redact(data))
        self.poll_status()
        if self.clock() - self.beat >= HEARTBEAT:
            self.beat = self.clock()
            self.event('heartbeat', used_bytes=self.used, sources={n: dict(present=t.present, offset=t.offset,
                       segment=t.segment) for n, t in self.tails.items()})

    def poll_status(self):
        path = self.package / 'logs/server-status.json'
        state = read_json(path) if path.is_file() else None
        running = bool(state and state.get('running'))
        try: age = time.time() - path.stat().st_mtime if state else None
        except OSError: age = None
        now = (running, bool(running and age is not None and age > STALE))
        if now != self.status:
            self.event('server_status', running=now[0], stale=now[1], age=None if age is None else round(age, 1))
            self.status = now

    def close(self, reason='stopped'):
        for tail in self.tails.values(): tail.flush()
        self.event('stop', reason=reason, used_bytes=self.used)
        self.events.close()
        files = {p.relative_to(self.out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(self.out.rglob('*')) if p.is_file()}
        manifest = dict(self.info, started=self.started, stopped=utc_now(), reason=reason, files=files)
        (self.out / 'manifest.json').write_text(json.dumps(manifest, indent=1) + '\n', encoding='utf-8')
        return self.out


def mask(text):
    return HOME.sub(r'\1<user>', IPV4.sub(lambda m: m.group(0) if m.group(0).startswith('127.') else '<ipv4>', text))


def export(run, out=None, keep_network=False):
    run = Path(run).resolve()
    if not (run / 'events.jsonl').is_file(): raise RuntimeError('Not a recorder run folder: ' + str(run))
    target = Path(out) if out else run.with_suffix('.zip')
    with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED) as z:
        for path in sorted(run.rglob('*')):
            if path.is_file():
                text = path.read_text(encoding='utf-8', errors='replace')
                z.writestr(run.name + '/' + path.relative_to(run).as_posix(), text if keep_network else mask(text))
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description='X4 Multiplayer continuous playtest recorder (read-only).')
    sub = parser.add_subparsers(dest='action', required=True)
    rec = sub.add_parser('record', help='record until Ctrl+C or --duration')
    rec.add_argument('--package', default=str(Path(__file__).resolve().parent))
    rec.add_argument('--profile', help='override the profile folder from installation.json')
    rec.add_argument('--out', help='parent folder for run folders (default: <package>/recordings)')
    rec.add_argument('--interval', type=float, default=.5)
    rec.add_argument('--max-mb', type=int, default=1024)
    rec.add_argument('--duration', type=float, help='stop after this many seconds')
    exp = sub.add_parser('export', help='zip one run for sharing (masks IPv4 and user names)')
    exp.add_argument('run'); exp.add_argument('--out'); exp.add_argument('--keep-network', action='store_true')
    args = parser.parse_args(argv)
    if args.action == 'export':
        print('Exported:', export(args.run, args.out, args.keep_network)); return 0
    if not 0.05 <= args.interval <= 10: parser.error('--interval must be 0.05..10 seconds')
    recorder = Recorder(args.package, args.profile, args.out, args.max_mb)
    print('Recording to', recorder.out, '- keep this window open; Ctrl+C stops the recording.', flush=True)
    end = None if args.duration is None else time.monotonic() + args.duration
    reason = 'duration'
    try:
        while end is None or time.monotonic() < end:
            recorder.poll()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        reason = 'ctrl-c'
    recorder.poll()
    print('Recording saved:', recorder.close(reason))
    return 0


if __name__ == '__main__':
    sys.exit(main())
