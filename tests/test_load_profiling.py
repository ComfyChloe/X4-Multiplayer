"""Load profiling tools: progressbar.xml deltas (layer 1) and the perf symbolizer (layer 3).

The agent side (layer 2, /proc parsers, inertness) is tested by `cargo test -p x4mp-agent`.
The real-binary FDE check runs only where the Linux X4 executable is present
(`X4_LINUX_BIN`, default ../x4-re/bin-linux/X4); it is not defined otherwise.
"""
import collections
import contextlib
import io
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dev'))

import perf_symbolize as ps  # noqa: E402
import progressbar_delta as pb  # noqa: E402

BEFORE = """<?xml version="1.0" encoding="UTF-8"?>
<progressbar>
  <type id="newgame" loaded="2"><stage id="init" time="2.0"/><stage id="map" time="10.0"/></type>
  <type id="savedgame" loaded="3">
    <stage id="init" time="1.0"/><stage id="map" time="6.0"/><stage id="jobs" time="10.0"/>
  </type>
</progressbar>"""


class ProgressbarDelta(unittest.TestCase):
    def test_parse(self):
        t = pb.parse(BEFORE)
        self.assertEqual([x.name for x in t], ['newgame', 'savedgame'])
        self.assertEqual(t[1].loaded, 3)
        self.assertEqual(t[1].stages, [('init', 1.0), ('map', 6.0), ('jobs', 10.0)])
        self.assertEqual(pb.parse('<p><savedgame loaded="5" init="1.5" map="2"/></p>')[0].stages,
                         [('init', 1.5), ('map', 2.0)])
        self.assertEqual(pb.parse("<p><n loaded='1'><init>0.5</init><map ms='3'/></n></p>")[0].stages,
                         [('init', 0.5), ('map', 3.0)])

    def test_cumulative_rule_recovers_one_load(self):
        after = pb.parse(BEFORE.replace('loaded="3"', 'loaded="4"')
                         .replace('<stage id="init" time="1.0"/>', '<stage id="init" time="1.25"/>')
                         .replace('<stage id="jobs" time="10.0"/>', '<stage id="jobs" time="10.5"/>'))
        deltas = pb.diff(pb.parse(BEFORE), after)
        self.assertEqual(len(deltas), 1)
        d = deltas[0]
        self.assertEqual((d.name, d.before_n, d.after_n, d.loads), ('savedgame', 3, 4, 1))
        self.assertEqual(d.cumulative(), [('init', 2.0), ('map', 6.0), ('jobs', 12.0)])
        self.assertAlmostEqual(d.implied_weight(20.0), 0.25)
        text = pb.report(d, 20.0)
        self.assertIn('rule check: agrees: cumulative mean', text)
        self.assertIn('sum (cumulative rule): 20.000', text)

    def test_ema_detected_with_measured_total(self):
        before = [pb.LoadType('savedgame', 9, [('a', 1.0), ('b', 6.0), ('c', 10.0)])]
        after = [pb.LoadType('savedgame', 10, [('a', 2.0), ('b', 6.0), ('c', 12.0)])]
        d = pb.diff(before, after)[0]
        self.assertAlmostEqual(d.implied_weight(23.0), 0.5)
        self.assertEqual(d.with_weight(0.5), [('a', 3.0), ('b', 6.0), ('c', 14.0)])
        self.assertIn('DISAGREES', pb.report(d, 23.0))

    def test_first_load_and_no_change(self):
        after = pb.parse('<p><type id="savedgame" loaded="1"><s id="init" time="4"/></type></p>')
        self.assertEqual(pb.diff([], after)[0].cumulative(), [('init', 4.0)])
        self.assertEqual(pb.diff(after, after), [])
        same_n = [pb.LoadType('x', 10, [('s', 2.0)])]
        d = pb.diff([pb.LoadType('x', 10, [('s', 1.0)])], same_n)[0]
        self.assertIsNone(d.cumulative())
        self.assertIn('cannot be derived', pb.report(d, None))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            b, a = Path(tmp, 'b.xml'), Path(tmp, 'a.xml')
            b.write_text(BEFORE)
            a.write_text(BEFORE.replace('loaded="3"', 'loaded="4"'))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(pb.main([str(b), str(a), '--measured', '17']), 0)
                self.assertEqual(pb.main([str(b), str(b)]), 1)
            self.assertIn('savedgame: loaded 3 -> 4', out.getvalue())


def uleb(v):
    out = bytearray()
    while True:
        byte = v & 0x7f
        v >>= 7
        out.append(byte | (0x80 if v else 0))
        if not v:
            return bytes(out)


def synthetic_eh_frame(section_va, functions):
    """A .eh_frame with one zR CIE (pcrel|sdata4) and one FDE per (start, size)."""
    cie_body = struct.pack('<I', 0) + bytes([1]) + b'zR\0' + uleb(1) + bytes([0x78, 16]) + uleb(1) + bytes([0x1b])
    cie_body += b'\0' * (-(len(cie_body) + 4) % 8)
    buf = bytearray(struct.pack('<I', len(cie_body)) + cie_body)
    for start, size in functions:
        fde_at = len(buf)
        field_at = fde_at + 8  # after length and CIE pointer
        body = struct.pack('<I', fde_at + 4)  # CIE pointer: distance back to offset 0
        body += struct.pack('<i', start - (section_va + field_at)) + struct.pack('<I', size) + uleb(0)
        body += b'\0' * (-(len(body) + 4) % 8)
        buf += struct.pack('<I', len(body)) + body
    buf += struct.pack('<I', 0)
    return bytes(buf)


PERF = """\
# ========
# captured on: test
X4 1000/1001 [002] 5000.100000:     250000 cpu-clock:
\t         1212495 [unknown] (/home/x4server/x4/game/X4)
\t         2025000 X4::whatever+0x10 (/home/x4server/x4/game/X4)
\t    7f0000001000 start_thread+0x7f (/usr/lib/x86_64-linux-gnu/libc.so.6)

JobThread 3 1000/1005 [001] 5000.600000:     250000 cpu-clock:
\t         1300004 [unknown] (/home/x4server/x4/game/X4)
\t         1212500 [unknown] (/home/x4server/x4/game/X4)

JobThread 12 1000/1006 [003] 5001.200000:     250000 cpu-clock:
\tffffffff81000000 do_syscall_64+0x5 ([kernel.kallsyms])
\t         1300010 [unknown] (/home/x4server/x4/game/X4)
\t         9999999 [unknown] (/home/x4server/x4/game/X4)

other 2000/2000 [000] 5001.300000:     250000 cpu-clock:
\t            4000 [unknown] (/usr/bin/other)
"""


def table():
    return ps.SymbolTable([0x1212490, 0x1300000, 0x2024fc0], [0x1212d7a, 0x1300010, 0x2025470],
                          ['U::MoveWorker::Run', 'fn_1300000', 'X4::FrameTick'], ['label', 'eh_frame', 'label'])


class Symbolizer(unittest.TestCase):
    def test_eh_frame_parser(self):
        va = 0x2a00000
        funcs = [(0x401000, 0x40), (0x401040, 0x100), (0x1212490, 0x8ea), (0x401000, 0x40), (0x500000, 0)]
        ranges = ps.parse_eh_frame(synthetic_eh_frame(va, funcs), va)
        self.assertEqual(ranges, [(0x401000, 0x401040), (0x401040, 0x401140), (0x1212490, 0x1212d7a)])

    def test_lookup_and_return_addresses(self):
        t = table()
        self.assertEqual(t.lookup(0x1212490), 'U::MoveWorker::Run')
        self.assertEqual(t.lookup(0x1212d79), 'U::MoveWorker::Run')
        self.assertIsNone(t.lookup(0x1212d7a))
        # A caller frame whose return address is the next function's first byte.
        f = ps.Frame(0x1300010, '[unknown]', '/x/X4')
        self.assertEqual(ps.frame_name(f, t, 'X4', caller=True), 'fn_1300000')
        self.assertEqual(ps.frame_name(f, t, 'X4', caller=False), 'X4!0x1300010')
        self.assertEqual(ps.frame_name(ps.Frame(1, 'memcpy+0x1f', '/lib/libc.so.6'), t, 'X4', True), 'memcpy')
        self.assertEqual(ps.frame_name(ps.Frame(1, '[unknown]', '/lib/libfoo.so'), t, 'X4', True), '[libfoo.so]')

    def test_perf_script_parse(self):
        samples = ps.parse_perf_script(PERF.splitlines(True))
        self.assertEqual(len(samples), 4)
        s = samples[1]
        self.assertEqual((s.comm, s.pid, s.tid, s.time, s.weight), ('JobThread 3', 1000, 1005, 5000.6, 250000))
        self.assertEqual([hex(f.ip) for f in s.frames], ['0x1300004', '0x1212500'])
        self.assertEqual(samples[0].frames[2].dso, '/usr/lib/x86_64-linux-gnu/libc.so.6')

    def test_perf_script_without_callchains(self):
        text = ('              X4  4321 12.500000:     250000 cpu-clock:ppp:           1212495 [unknown] (/g/X4)\n'
                '      JobThread 3  4325 12.750000:     250000 cpu-clock:ppp:  7f00 memcpy+0x3 (/lib/libc.so.6)\n')
        samples = ps.parse_perf_script(text.splitlines(True))
        self.assertEqual([(s.comm, s.tid, s.time) for s in samples], [('X4', 4321, 12.5), ('JobThread 3', 4325, 12.75)])
        self.assertEqual([f.ip for f in samples[0].frames], [0x1212495])
        self.assertEqual(samples[1].frames[0].sym, 'memcpy+0x3')

    def test_fold_and_top(self):
        samples = ps.parse_perf_script(PERF.splitlines(True))
        folded = ps.fold(samples, table(), 'X4', True, None, pid=1000)['all']
        self.assertEqual(folded, collections.Counter({
            'X4;start_thread;X4::FrameTick;U::MoveWorker::Run': 1,
            'JobThread;U::MoveWorker::Run;fn_1300000': 1,
            'JobThread;X4!0x9999999;fn_1300000;do_syscall_64': 1,
        }))
        text = ps.top_table(folded, 5)
        self.assertIn('== JobThread: 2 samples (66.7% of all)', text)
        self.assertIn('fn_1300000', text)

    def test_phases_from_agent_log(self):
        log = '\n'.join([
            '1.0 X4MP_AGENT LOADPROF t=1.0 mono=5000.000 dt=0.25 load=0 cpu=1 top=-',
            '1.0 X4MP_AGENT LOADPROF t=1.5 mono=5000.500 dt=0.25 load=1 cpu=1 top=-',
            '1.0 X4MP_AGENT LOADPROF t=2.0 mono=5001.000 dt=0.25 load=1 cpu=1 top=-',
            '1.0 X4MP_AGENT LOADPROF t=2.5 mono=5001.500 dt=0.25 load=0 cpu=1 top=-',
            '1.0 X4MP_AGENT LOADSTAGES type=savedgame loaded=3->4 rule=cumulative loads=1 sum=1 negative=0 stages=init:0.300,map:0.700',
            '1.0 X4MP_AGENT LOADPROF SUMMARY end=complete load_start_mono=5000.500 load_end_mono=5001.500',
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'agent.log')
            path.write_text(log)
            phases = ps.read_phases(path)
        self.assertEqual(phases.phase(4999.0), 'pre')
        self.assertEqual(phases.phase(5000.6), 'loading:init')
        self.assertEqual(phases.phase(5000.9), 'loading:map')
        self.assertEqual(phases.phase(5001.2), 'post')
        self.assertEqual(phases.phase(5009.0), 'after')
        samples = ps.parse_perf_script(PERF.splitlines(True))
        folded = ps.fold(samples, table(), 'X4', True, phases, pid=1000)
        self.assertEqual(sorted(folded), ['loading', 'loading:init', 'post'])

    def test_fold_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, 'perf.txt')
            script.write_text(PERF)
            syms = Path(tmp, 'syms.tsv')
            table().write(syms)
            out = Path(tmp, 'out', 'load')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ps.main(['fold', '--symbols', str(syms), str(script), '--out', str(out)]), 0)
                self.assertEqual(ps.main(['fold', '--symbols', str(syms), str(script), '--out', str(out) + 'p', '--by-period']), 0)
            self.assertIn('other;[other] 250000', Path(tmp, 'out', 'loadp.folded').read_text().splitlines())
            lines = Path(tmp, 'out', 'load.folded').read_text().splitlines()
            self.assertIn('other;[other] 1', lines)
            self.assertEqual(len(lines), 4)
            self.assertTrue(all(line.rsplit(' ', 1)[1].isdigit() for line in lines))
            self.assertTrue(Path(tmp, 'out', 'load.top.txt').is_file())

    def test_thread_groups(self):
        self.assertEqual([ps.thread_group(x) for x in ['JobThread 12', 'X4', 'Worker12', 'gl3', 'Movement worker']],
                         ['JobThread', 'X4', 'Worker', 'gl3', 'Movement worker'])


REAL_BIN = Path(os.environ.get('X4_LINUX_BIN', ROOT.parent / 'x4-re' / 'bin-linux' / 'X4'))

if REAL_BIN.is_file():
    class RealBinary(unittest.TestCase):
        """Only defined where the pinned Linux X4 executable is available."""

        @classmethod
        def setUpClass(cls):
            cls.elf = ps.Elf(REAL_BIN.read_bytes())
            cls.table = ps.build_table(cls.elf, ps.default_re_dir())

        def test_fde_count_matches_eh_frame_hdr(self):
            hdr = self.elf.bytes_of(self.elf.section('.eh_frame_hdr'))
            self.assertEqual(self.table.stats['fdes'], struct.unpack_from('<I', hdr, 8)[0])
            self.assertGreater(self.table.stats['fdes'], 90000)

        def test_known_functions_start_an_fde(self):
            for addr, name in ps.SPOT_CHECKS:
                i = self.table.starts.index(addr)
                self.assertGreater(self.table.ends[i], addr)
                if ps.default_re_dir() is not None:
                    self.assertEqual(self.table.names[i].rstrip('?'), name)


if __name__ == '__main__':
    unittest.main()
