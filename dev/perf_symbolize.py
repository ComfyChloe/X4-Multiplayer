#!/usr/bin/env python3
"""Symbolise `perf script` output for the Linux X4 build and fold it for flame graphs.

The Linux X4 executable (9.00, Steam build 23660954, ET_EXEC at 0x400000) has no engine
symbols: `.dynsym` only names the Lua FFI exports and statically linked libraries. This
tool names addresses inside the X4 image from, in order of preference:

  1. `.dynsym` FUNC symbols (exact; FFI exports and library code);
  2. hand labels that give a Linux address (`linux=0x...` in label comments and the
     `# linux` lines at the end of some label files);
  3. `labels/linux-xfer.tsv` (`Linux 0x...` in the comment);
  4. Windows labels (`labels/*.tsv`, then `symbols.csv`) carried to Linux through the
     function pairs in `data/xfer/pairs.tsv`, and `data/xfer/win-labels-on-linux.tsv`;
  5. otherwise `fn_<start>`, using the function boundaries of the `.eh_frame` FDEs.

A name from a low-confidence label or pair ends in `?`. Addresses outside the X4 image
keep perf's own symbol (libraries, kernel) or become `[<dso>]`.

Commands (no third-party modules needed; runs on the VPS and on Windows):

  table     build the merged symbol table (TSV: start end name source)
  validate  parse the real binary: FDE count, label spot checks, coverage, frame pointers
  fold      read `perf script` text and write folded stacks plus a per-thread top-N table

See reports/LOAD-PROFILING.md for the capture procedure.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_NAME = 'X4'

# ---------------------------------------------------------------------------------
# ELF


@dataclass
class Section:
    name: str
    type: int
    addr: int
    offset: int
    size: int
    link: int
    entsize: int


class Elf:
    """Minimal little-endian ELF64 reader: section headers and section bytes."""

    def __init__(self, data: bytes):
        if data[:4] != b'\x7fELF' or data[4] != 2 or data[5] != 1:
            raise ValueError('not a little-endian ELF64 file')
        self.data = data
        self.type = struct.unpack_from('<H', data, 0x10)[0]
        shoff = struct.unpack_from('<Q', data, 0x28)[0]
        shentsize, shnum, shstrndx = struct.unpack_from('<HHH', data, 0x3a)
        raw = []
        for i in range(shnum):
            name, typ, _flags, addr, offset, size, link, _info, _align, entsize = struct.unpack_from(
                '<IIQQQQIIQQ', data, shoff + i * shentsize)
            raw.append((name, typ, addr, offset, size, link, entsize))
        strtab = raw[shstrndx]
        names = data[strtab[3]:strtab[3] + strtab[4]]
        self.sections = []
        for name, typ, addr, offset, size, link, entsize in raw:
            end = names.index(b'\0', name)
            self.sections.append(Section(names[name:end].decode('ascii', 'replace'), typ, addr, offset, size, link, entsize))

    def section(self, name: str) -> Section | None:
        return next((s for s in self.sections if s.name == name), None)

    def bytes_of(self, s: Section) -> bytes:
        return self.data[s.offset:s.offset + s.size]

    def read_va(self, va: int, length: int) -> bytes:
        for s in self.sections:
            if s.type != 8 and s.addr and s.addr <= va and va + length <= s.addr + s.size:  # 8 = NOBITS
                off = s.offset + va - s.addr
                return self.data[off:off + length]
        return b''

    def dynsym_functions(self) -> dict[int, tuple[str, int]]:
        """Defined FUNC symbols in `.dynsym`: {address: (name, size)}."""
        sym = self.section('.dynsym')
        if sym is None:
            return {}
        strs = self.bytes_of(self.sections[sym.link])
        out: dict[int, tuple[str, int]] = {}
        body = self.bytes_of(sym)
        for off in range(0, len(body), 24):
            name, info, _other, shndx, value, size = struct.unpack_from('<IBBHQQ', body, off)
            if (info & 0xf) != 2 or shndx == 0 or value == 0:  # STT_FUNC, defined
                continue
            text = strs[name:strs.index(b'\0', name)].decode('utf-8', 'replace')
            if value not in out or len(text) < len(out[value][0]):
                out[value] = (text, size)
        return out


# ---------------------------------------------------------------------------------
# .eh_frame (FDE function boundaries)


def _uleb(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7f) << shift
        shift += 7
        if byte < 0x80:
            return result, pos


def _sleb(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7f) << shift
        shift += 7
        if byte < 0x80:
            if byte & 0x40:
                result -= 1 << shift
            return result, pos


_FIXED = {0x02: '<H', 0x03: '<I', 0x04: '<Q', 0x0a: '<h', 0x0b: '<i', 0x0c: '<q', 0x00: '<Q'}


def _encoded(buf: bytes, pos: int, enc: int, field_va: int, apply: bool = True) -> tuple[int, int]:
    """Read a DW_EH_PE-encoded value. `apply=False` ignores the application bits
    (used for the FDE address range, which is a plain length)."""
    fmt = enc & 0x0f
    if fmt == 0x01:
        value, end = _uleb(buf, pos)
    elif fmt == 0x09:
        value, end = _sleb(buf, pos)
    elif fmt in _FIXED:
        code = _FIXED[fmt]
        value = struct.unpack_from(code, buf, pos)[0]
        end = pos + struct.calcsize(code)
    else:
        raise ValueError(f'unsupported pointer encoding 0x{enc:02x}')
    if apply:
        app = enc & 0x70
        if app == 0x10:  # pcrel
            value += field_va
        elif app not in (0x00,):
            raise ValueError(f'unsupported pointer application 0x{enc:02x}')
        value &= (1 << 64) - 1
    return value, end


def _cie_fde_encoding(buf: bytes, pos: int, section_va: int) -> int:
    """Parse a CIE body (after the CIE id) and return its FDE pointer encoding."""
    version = buf[pos]
    pos += 1
    end = buf.index(b'\0', pos)
    aug = buf[pos:end].decode('ascii', 'replace')
    pos = end + 1
    if 'eh' in aug:
        pos += 8
    _, pos = _uleb(buf, pos)  # code alignment
    _, pos = _sleb(buf, pos)  # data alignment
    if version == 1:
        pos += 1
    else:
        _, pos = _uleb(buf, pos)
    enc = 0x00
    if aug.startswith('z'):
        _, pos = _uleb(buf, pos)
        for ch in aug[1:]:
            if ch == 'R':
                enc = buf[pos]
                pos += 1
            elif ch == 'P':
                penc = buf[pos]
                pos += 1
                _, pos = _encoded(buf, pos, penc & 0x7f, section_va + pos, apply=False)
            elif ch == 'L':
                pos += 1
            elif ch in 'SB':
                pass
            else:
                break  # unknown augmentation: the length lets us stop safely
    return enc


def parse_eh_frame(buf: bytes, section_va: int) -> list[tuple[int, int]]:
    """Return sorted, de-duplicated (start, end) ranges of every FDE with a non-zero range."""
    encodings: dict[int, int] = {}
    ranges = set()
    pos = 0
    while pos + 4 <= len(buf):
        length = struct.unpack_from('<I', buf, pos)[0]
        if length == 0:
            break  # terminator
        hdr = 4
        if length == 0xffffffff:
            length = struct.unpack_from('<Q', buf, pos + 4)[0]
            hdr = 12
        start = pos + hdr
        end = start + length
        cie_id = struct.unpack_from('<I', buf, start)[0]
        if cie_id == 0:
            encodings[start - hdr] = _cie_fde_encoding(buf, start + 4, section_va)
        else:
            cie_at = start - cie_id
            if cie_at not in encodings:
                # CIE not seen yet (unusual ordering): parse it in place.
                clen = struct.unpack_from('<I', buf, cie_at)[0]
                chdr = 12 if clen == 0xffffffff else 4
                encodings[cie_at] = _cie_fde_encoding(buf, cie_at + chdr + 4, section_va)
            enc = encodings[cie_at]
            p = start + 4
            begin, p = _encoded(buf, p, enc, section_va + p)
            size, p = _encoded(buf, p, enc, section_va + p, apply=False)
            if size:
                ranges.add((begin, begin + size))
        pos = end
    return sorted(ranges)


# ---------------------------------------------------------------------------------
# Labels


@dataclass
class Name:
    name: str
    source: str
    rank: int  # lower wins


LINUX_EQ = re.compile(r'linux=0x([0-9a-fA-F]+)')
LINUX_XFER = re.compile(r'Linux 0x([0-9a-fA-F]+)\b')
HASH_LINUX = re.compile(r'^#\s*linux[= ]0x([0-9a-fA-F]+)\t([^\t]+)(?:\t([^\t]*))?')
LOW = {'low'}


def _clean(name: str) -> str:
    return name.strip().replace(';', ':').replace('\t', ' ')


def read_label_files(labels_dir: Path) -> tuple[list[tuple[int, str, str, str]], list[tuple[int, str, str, str]], list[tuple[int, str, str, str]]]:
    """Return (linux_hand, linux_xfer, windows) rows as (address, name, confidence, file)."""
    linux_hand, linux_xfer, windows = [], [], []
    for path in sorted(labels_dir.glob('*.tsv')):
        if path.name == 'conflicts.tsv':
            continue
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            if not line.strip():
                continue
            if line.startswith('#'):
                m = HASH_LINUX.match(line)
                if m:
                    kind = (m.group(3) or 'function').strip()
                    if kind == 'data':
                        continue
                    linux_hand.append((int(m.group(1), 16), _clean(m.group(2)), 'medium', path.name))
                continue
            cols = line.split('\t')
            if len(cols) < 3:
                continue
            try:
                rva = int(cols[0], 16)
            except ValueError:
                continue
            name, kind = _clean(cols[1]), cols[2].strip()
            conf = cols[4].strip() if len(cols) > 4 else 'medium'
            comment = cols[6] if len(cols) > 6 else ''
            if kind != 'function':
                continue
            if path.name == 'linux-xfer.tsv':
                m = LINUX_XFER.search(comment)
                if m:
                    linux_xfer.append((int(m.group(1), 16), name, conf, path.name))
                continue
            windows.append((rva, name, conf, path.name))
            m = LINUX_EQ.search(comment)
            if m:
                linux_hand.append((int(m.group(1), 16), name, conf, path.name))
    return linux_hand, linux_xfer, windows


def read_symbols_csv(path: Path) -> list[tuple[int, str, str, str]]:
    rows = []
    with path.open(encoding='utf-8', errors='replace', newline='') as f:
        for row in csv.DictReader(f):
            if row.get('kind') != 'function':
                continue
            try:
                rows.append((int(row['rva'], 16), _clean(row['name']), row.get('confidence', 'medium'), path.name))
            except (KeyError, ValueError):
                continue
    return rows


def read_pairs(path: Path) -> dict[int, tuple[int, str]]:
    """Windows RVA -> (Linux VA, pair confidence)."""
    out = {}
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        if line.startswith('#') or not line.strip():
            continue
        cols = line.split('\t')
        try:
            out[int(cols[0], 16)] = (int(cols[1], 16), cols[3].strip())
        except (IndexError, ValueError):
            continue
    return out


def read_win_on_linux(path: Path) -> list[tuple[int, str, str]]:
    rows = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        if line.startswith('#') or not line.strip():
            continue
        cols = line.split('\t')
        try:
            conf = 'low' if 'low' in (cols[4].strip(), cols[6].strip()) else 'medium'
            rows.append((int(cols[0], 16), _clean(cols[2]), conf))
        except (IndexError, ValueError):
            continue
    return rows


@dataclass
class SymbolTable:
    starts: list[int]
    ends: list[int]
    names: list[str]
    sources: list[str]
    stats: dict = field(default_factory=dict)

    def lookup(self, addr: int) -> str | None:
        """Name of the FDE function that contains `addr`, or None outside every FDE."""
        i = bisect.bisect_right(self.starts, addr) - 1
        if i >= 0 and addr < self.ends[i]:
            return self.names[i]
        return None

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8', newline='\n') as f:
            f.write('#start\tend\tname\tsource\n')
            for row in zip(self.starts, self.ends, self.names, self.sources):
                f.write(f'{row[0]:x}\t{row[1]:x}\t{row[2]}\t{row[3]}\n')

    @classmethod
    def read(cls, path: Path) -> 'SymbolTable':
        starts, ends, names, sources = [], [], [], []
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.startswith('#') or not line:
                continue
            a, b, n, s = line.split('\t')
            starts.append(int(a, 16))
            ends.append(int(b, 16))
            names.append(n)
            sources.append(s)
        return cls(starts, ends, names, sources)


def _nonoverlapping(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Keep the first of overlapping ranges (FDEs never overlap in practice)."""
    out: list[tuple[int, int]] = []
    for a, b in ranges:
        if out and a < out[-1][1]:
            continue
        out.append((a, b))
    return out


def build_table(elf: Elf, re_dir: Path | None) -> SymbolTable:
    eh = elf.section('.eh_frame')
    if eh is None:
        raise SystemExit('binary has no .eh_frame')
    ranges = _nonoverlapping(parse_eh_frame(elf.bytes_of(eh), eh.addr))
    starts = [a for a, _ in ranges]
    start_set = set(starts)
    best: dict[int, Name] = {}
    stats = collections.Counter()

    def offer(addr: int, name: str, source: str, rank: int, conf: str) -> None:
        if addr not in start_set:
            stats[f'{source}:not_fde_start'] += 1
            return
        if conf in LOW:
            name += '?'
            rank += 1
        cur = best.get(addr)
        if cur is None or rank < cur.rank:
            best[addr] = Name(name, source, rank)

    for addr, (name, _size) in elf.dynsym_functions().items():
        offer(addr, _clean(name), 'dynsym', 0, 'high')
    if re_dir is not None:
        labels = re_dir / 'labels'
        hand, xfer, windows = read_label_files(labels) if labels.is_dir() else ([], [], [])
        for addr, name, conf, fname in hand:
            offer(addr, name, f'label:{fname}', 10, conf)
        for addr, name, conf, _ in xfer:
            offer(addr, name, 'linux-xfer', 20, conf)
        pairs_path = re_dir / 'data/xfer/pairs.tsv'
        pairs = read_pairs(pairs_path) if pairs_path.is_file() else {}
        for rva, name, conf, fname in windows:
            if rva in pairs:
                va, pconf = pairs[rva]
                offer(va, name, f'pair:{fname}', 30, 'low' if 'low' in (conf, pconf) else conf)
        wol = re_dir / 'data/xfer/win-labels-on-linux.tsv'
        if wol.is_file():
            for va, name, conf in read_win_on_linux(wol):
                offer(va, name, 'win-labels-on-linux', 40, conf)
        sym_csv = re_dir / 'symbols.csv'
        if sym_csv.is_file():
            for rva, name, conf, fname in read_symbols_csv(sym_csv):
                if rva in pairs:
                    va, pconf = pairs[rva]
                    offer(va, name, 'pair:symbols.csv', 50, 'low' if 'low' in (conf, pconf) else conf)
    names, sources = [], []
    for a in starts:
        n = best.get(a)
        names.append(n.name if n else f'fn_{a:x}')
        sources.append(n.source if n else 'eh_frame')
    table = SymbolTable(starts, [b for _, b in ranges], names, sources)
    by_source = collections.Counter(s.split(':')[0] for s in sources)
    table.stats = {'fdes': len(ranges), 'named': sum(1 for s in sources if s != 'eh_frame'), 'by_source': dict(by_source),
                   'skipped': dict(stats)}
    return table


def default_re_dir() -> Path | None:
    env = os.environ.get('X4_RE_DIR')
    if env:
        return Path(env)
    guess = Path(__file__).resolve().parents[2] / 'x4-re'
    return guess if guess.is_dir() else None


def load_table(args) -> SymbolTable:
    if getattr(args, 'symbols', None):
        return SymbolTable.read(Path(args.symbols))
    if not args.binary:
        raise SystemExit('give --binary (the Linux X4 executable) or --symbols (a table from the table command)')
    elf = Elf(Path(args.binary).read_bytes())
    re_dir = Path(args.re_dir) if args.re_dir else default_re_dir()
    return build_table(elf, re_dir)


# ---------------------------------------------------------------------------------
# perf script parsing


@dataclass
class Frame:
    ip: int
    sym: str
    dso: str


@dataclass
class Sample:
    comm: str
    pid: int
    tid: int
    time: float
    weight: int
    frames: list[Frame]  # leaf first, as perf prints them


HEADER = re.compile(
    r'^(?P<comm>\S.*?)\s+(?:(?P<pid>-?\d+)/)?(?P<tid>-?\d+)\s+(?:\[(?P<cpu>\d+)\]\s+)?(?P<time>\d+\.\d+):'
    r'(?:\s+(?P<period>\d+))?(?:\s+(?P<event>[^\s:]+(?::[^\s:]+)*):)?')
FRAME = re.compile(r'^\s+(?P<ip>[0-9a-fA-F]+)\s+(?P<sym>.*?)\s*\((?P<dso>[^()]*)\)\s*$')
SYMOFF = re.compile(r'\+0x[0-9a-fA-F]+$')


def parse_perf_script(lines) -> list[Sample]:
    """Parse `perf script` text (default fields, or `-F comm,pid,tid,time,period,event,ip,sym,dso`)."""
    samples: list[Sample] = []
    cur: Sample | None = None
    for raw in lines:
        line = raw.rstrip('\n')
        if not line.strip():
            if cur is not None:
                samples.append(cur)
                cur = None
            continue
        if line[0] == '\t' or (cur is not None and line[0] == ' ' and FRAME.match(line)):
            # Callchain lines are tab-indented. Header lines may start with spaces:
            # without callchains perf right-aligns comm to 16 columns.
            if cur is None:
                continue
            m = FRAME.match(line)
            if m:
                cur.frames.append(Frame(int(m.group('ip'), 16), m.group('sym').strip(), m.group('dso').strip()))
            continue
        line = line.strip()
        if line.startswith('#'):
            continue
        m = HEADER.match(line)
        if not m:
            continue
        if cur is not None:
            samples.append(cur)
        tid = int(m.group('tid'))
        pid = int(m.group('pid')) if m.group('pid') else tid
        cur = Sample(m.group('comm').strip(), pid, tid, float(m.group('time')), int(m.group('period') or 1), [])
        inline = FRAME.match(' ' + line[m.end():].strip())  # no -g: the ip is on the header line
        if inline:
            cur.frames.append(Frame(int(inline.group('ip'), 16), inline.group('sym').strip(), inline.group('dso').strip()))
    if cur is not None:
        samples.append(cur)
    return samples


THREAD_NUMBER = re.compile(r'^(?P<prefix>.*?)(?P<sep>[\s#_:-]*)\d+$')


def thread_group(comm: str) -> str:
    """`JobThread 12` -> `JobThread` (same rule as the agent's LOADPROF GROUP lines): a
    trailing number goes, unless it is glued to a prefix shorter than 3 (`X4`)."""
    m = THREAD_NUMBER.match(comm.strip())
    if not m:
        return comm.strip()
    group = m.group('prefix').strip()
    if not group or (not m.group('sep') and len(group) < 3):
        return comm.strip()
    return group


def is_image(dso: str, image: str) -> bool:
    return dso == image or dso.endswith('/' + image)


def frame_name(frame: Frame, table: SymbolTable, image: str, caller: bool) -> str:
    if is_image(frame.dso, image):
        # A caller frame's ip is a return address: look up ip-1 so a call that ends a
        # function is attributed to that function, not to the next one.
        addr = frame.ip - 1 if caller and frame.ip else frame.ip
        name = table.lookup(addr)
        return name if name else f'X4!0x{frame.ip:x}'
    sym = SYMOFF.sub('', frame.sym)
    if sym and sym != '[unknown]':
        return _clean(sym)
    base = frame.dso.rsplit('/', 1)[-1] or 'unknown'
    return f'[{base}]'


# ---------------------------------------------------------------------------------
# Load phases from the agent log


@dataclass
class Phases:
    """Load-flag intervals from LOADPROF rows: [(t_end, state)] on CLOCK_MONOTONIC."""
    rows: list[tuple[float, str]]
    stages: list[tuple[str, float, float]]  # (name, start, end), approximate

    def phase(self, t: float) -> str:
        if not self.rows:
            return 'all'
        i = self._index(t)
        if i >= len(self.rows):
            return 'after'
        state = self.rows[i][1]
        if state == '1':
            for name, a, b in self.stages:
                if a <= t < b:
                    return f'loading:{name}'
            return 'loading'
        seen_load = any(s == '1' for _, s in self.rows[:i])
        return 'post' if seen_load else 'pre'

    def _index(self, t: float) -> int:
        if not hasattr(self, '_ends'):
            self._ends = [r[0] for r in self.rows]
        return bisect.bisect_left(self._ends, t)


KV = re.compile(r'(\w+)=([^\s]+)')


def read_phases(path: Path) -> Phases:
    rows: list[tuple[float, str]] = []
    stage_line = None
    load_start = None
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        if ' LOADPROF ' in line and ' SUMMARY' not in line and ' GROUP' not in line:
            kv = dict(KV.findall(line))
            if 'mono' in kv and 'load' in kv:
                rows.append((float(kv['mono']), kv['load']))
        elif ' LOADPROF SUMMARY' in line:
            kv = dict(KV.findall(line))
            if kv.get('load_start_mono', 'none') != 'none':
                load_start = float(kv['load_start_mono'])
        elif ' LOADSTAGES ' in line and 'type=' in line and stage_line is None:
            stage_line = line
    stages = []
    if stage_line and load_start is not None:
        t = load_start
        body = stage_line.split(' stages=', 1)[-1].split()[0] if ' stages=' in stage_line else ''
        for item in body.split(','):
            if ':' not in item:
                continue
            name, value = item.split(':', 1)
            try:
                d = float(value)
            except ValueError:
                continue
            stages.append((name, t, t + max(d, 0.0)))
            t += max(d, 0.0)
    rows.sort()
    return Phases(rows, stages)


# ---------------------------------------------------------------------------------
# Folding and tables


def fold(samples: list[Sample], table: SymbolTable, image: str, group_threads: bool, phases: Phases | None,
         pid: int | None = None, by_period: bool = False) -> dict[str, collections.Counter]:
    """Return {phase: Counter(folded_stack -> weight)}; phase is 'all' without an agent log.
    The weight is the sample count, or the sample period (e.g. ns of cpu-clock) with `by_period`."""
    out: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for s in samples:
        if pid is not None and s.pid != pid:
            continue
        names = [frame_name(f, table, image, caller=i > 0) for i, f in enumerate(s.frames)]
        names.reverse()  # root first
        thread = thread_group(s.comm) if group_threads else s.comm
        stack = ';'.join([_clean(thread)] + names) if names else _clean(thread)
        phase = phases.phase(s.time) if phases else 'all'
        out[phase][stack] += s.weight if by_period else 1
    return out


def top_table(folded: collections.Counter, top: int) -> str:
    """Per-thread self and inclusive top-N table from folded stacks."""
    per_thread_total = collections.Counter()
    self_w: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    incl_w: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for stack, w in folded.items():
        parts = stack.split(';')
        thread, frames = parts[0], parts[1:]
        per_thread_total[thread] += w
        if frames:
            self_w[thread][frames[-1]] += w
            for name in set(frames):
                incl_w[thread][name] += w
    total = sum(per_thread_total.values()) or 1
    lines = []
    for thread, tw in per_thread_total.most_common():
        lines.append(f'== {thread}: {tw} samples ({100.0 * tw / total:.1f}% of all)')
        lines.append(f'   {"self%":>6} {"incl%":>6}  function')
        for name, w in self_w[thread].most_common(top):
            lines.append(f'   {100.0 * w / tw:6.1f} {100.0 * incl_w[thread][name] / tw:6.1f}  {name}')
        shown = {n for n, _ in self_w[thread].most_common(top)}
        extra = [(n, w) for n, w in incl_w[thread].most_common(top * 2) if n not in shown][:top]
        if extra:
            lines.append('   top inclusive (not in the self list):')
            for name, w in extra:
                lines.append(f'   {"":6} {100.0 * w / tw:6.1f}  {name}')
    return '\n'.join(lines) + '\n'


def write_folded(path: Path, folded: collections.Counter) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as f:
        for stack, w in sorted(folded.items()):
            f.write(f'{stack} {w}\n')


# ---------------------------------------------------------------------------------
# Validation on the real binary

SPOT_CHECKS = [
    (0x1212490, 'U::MoveWorker::Run'),
    (0x2024fc0, 'X4::FrameTick'),
    (0x1a3b9c0, 'XLib::VbiTable::Unregister'),
    (0xfcdbd0, 'X4::GameClient::Init'),
    (0x1106ee0, 'AI::Gravidar::Run'),
    (0x13e35e0, 'U::Path::PathingManager::Run'),
]


def frame_pointer_share(elf: Elf, table: SymbolTable, sample: int = 4000) -> tuple[int, int]:
    """How many sampled functions set up rbp as a frame pointer in their prologue."""
    step = max(1, len(table.starts) // sample)
    hits = checked = 0
    for a, b in list(zip(table.starts, table.ends))[::step]:
        code = elf.read_va(a, min(48, b - a))
        if not code:
            continue
        checked += 1
        if b'\x48\x89\xe5' in code or b'\x48\x8b\xec' in code:  # mov rbp, rsp
            hits += 1
    return hits, checked


def validate(args) -> int:
    data = Path(args.binary).read_bytes()
    elf = Elf(data)
    re_dir = Path(args.re_dir) if args.re_dir else default_re_dir()
    table = build_table(elf, re_dir)
    print(f'binary: {args.binary} ({len(data)} bytes, ELF type {elf.type})')
    hdr = elf.section('.eh_frame_hdr')
    if hdr is not None:
        body = elf.bytes_of(hdr)
        # version, eh_frame_ptr_enc, fde_count_enc, table_enc; then eh_frame_ptr, fde_count
        if body[0] == 1 and body[1] & 0x0f == 0x0b and body[2] == 0x03:
            print(f'.eh_frame_hdr search table entries: {struct.unpack_from("<I", body, 8)[0]}')
    print(f'FDE functions: {table.stats["fdes"]}')
    print(f'named: {table.stats["named"]} ({100.0 * table.stats["named"] / max(1, table.stats["fdes"]):.1f}%)')
    for source, n in sorted(table.stats['by_source'].items(), key=lambda kv: -kv[1]):
        print(f'  {source}: {n}')
    for key, n in sorted(table.stats['skipped'].items()):
        print(f'  skipped ({key}): {n}')
    low = sum(1 for n in table.names if n.endswith('?'))
    print(f'  of which low confidence (?): {low}')
    ok = True
    for addr, expected in SPOT_CHECKS:
        i = bisect.bisect_right(table.starts, addr) - 1
        inside = i >= 0 and addr < table.ends[i]
        exact = inside and table.starts[i] == addr
        got = table.names[i] if inside else None
        good = exact and got is not None and got.rstrip('?') == expected
        ok &= inside and exact
        print(f'spot 0x{addr:x} {expected}: in FDE={inside} FDE start=0x{table.starts[i]:x} size=0x{table.ends[i] - table.starts[i]:x} name={got}'
              f' {"OK" if good else "CHECK"}')
    hits, checked = frame_pointer_share(elf, table)
    print(f'frame pointer prologue (mov rbp,rsp in first 48 bytes): {hits}/{checked} sampled functions ({100.0 * hits / max(1, checked):.1f}%)')
    if args.write:
        table.write(Path(args.write))
        print(f'wrote {args.write}')
    return 0 if ok else 1


# ---------------------------------------------------------------------------------
# CLI


def cmd_table(args) -> int:
    table = load_table(args)
    table.write(Path(args.out))
    print(f'{table.stats.get("fdes", len(table.starts))} functions, {table.stats.get("named", "?")} named -> {args.out}')
    return 0


def cmd_fold(args) -> int:
    table = load_table(args)
    src = sys.stdin if args.perf_script == '-' else open(args.perf_script, encoding='utf-8', errors='replace')
    with src:
        samples = parse_perf_script(src)
    phases = read_phases(Path(args.agent_log)) if args.agent_log else None
    folded = fold(samples, table, args.image, not args.no_group, phases, args.pid, args.by_period)
    out = Path(args.out)
    total = collections.Counter()
    for phase, counter in folded.items():
        total.update(counter)
    folded_path, top_path = Path(f'{out}.folded'), Path(f'{out}.top.txt')
    write_folded(folded_path, total)
    report = [f'# {len(samples)} samples; image {args.image}; phases: {", ".join(sorted(folded))}', '']
    report.append('## all phases')
    report.append(top_table(total, args.top))
    if phases is not None:
        for phase in sorted(folded):
            write_folded(out.with_name(f'{out.name}.{phase.replace(":", "-")}.folded'), folded[phase])
            report.append(f'## phase {phase}')
            report.append(top_table(folded[phase], args.top))
    text = '\n'.join(report)
    top_path.write_text(text, encoding='utf-8')
    print(text if args.print else f'wrote {folded_path}, {top_path} and per-phase folded files')
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    def common(sp, need_binary=True):
        sp.add_argument('--binary', help='the Linux X4 executable (ELF)', required=False)
        sp.add_argument('--re-dir', help='x4-re directory with labels/ and data/xfer/ (default: ../x4-re or $X4_RE_DIR)')

    t = sub.add_parser('table', help='write the merged symbol table')
    common(t)
    t.add_argument('--symbols', help=argparse.SUPPRESS)
    t.add_argument('--out', default=str(Path(__file__).resolve().parents[1] / '.local/x4-linux-symbols.tsv'))
    t.set_defaults(func=cmd_table)

    v = sub.add_parser('validate', help='parse the real binary and spot-check labels')
    common(v)
    v.add_argument('--write', help='also write the symbol table here')
    v.set_defaults(func=validate)

    f = sub.add_parser('fold', help='fold perf script output')
    common(f)
    f.add_argument('--symbols', help='symbol table from the table command (faster than --binary)')
    f.add_argument('perf_script', help='perf script text file, or - for stdin')
    f.add_argument('--out', default='x4-load', help='output prefix: <out>.folded, <out>.top.txt, <out>.<phase>.folded')
    f.add_argument('--agent-log', help='agent log with LOADPROF rows (split by load phase; needs perf record -k CLOCK_MONOTONIC)')
    f.add_argument('--image', default=IMAGE_NAME, help='DSO name of the X4 image in perf output (default X4)')
    f.add_argument('--pid', type=int, help='only samples of this process')
    f.add_argument('--top', type=int, default=15)
    f.add_argument('--no-group', action='store_true', help='keep JobThread N etc. as separate threads')
    f.add_argument('--print', action='store_true', help='print the top-N table')
    f.add_argument('--by-period', action='store_true', help='weight by sample period instead of sample count')
    f.set_defaults(func=cmd_fold)

    args = p.parse_args(argv)
    if args.cmd == 'validate' and not args.binary:
        p.error('validate needs --binary')
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
