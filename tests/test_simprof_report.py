"""Offline simprof symbolizer tests (synthetic labels and debug-log lines)."""
from pathlib import Path
import contextlib
import io
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dev'))
import simprof_report  # noqa: E402

LOG = '''\
[=ERROR=] 1.00 [X4LAN] FRAMEPROBE armed in MoveWorker slot 5 mode=profile
[=ERROR=] 1.00 [X4LAN] FEATURE simprof ENABLED intervalUs=1000 depth=32 reportS=10
[=ERROR=] 11.00 [X4LAN] SIMPROF window seconds=10.00 samples=100 rateHz=1000 depthMean=12.0 suspendUsMean=20.0 suspendUsMax=90.0 walkUsMean=2.0 overheadPct=2.00 truncated=1 scanned=0 lost=10 loading=0 failures=0 skipped=0 droppedFunctions=0 droppedStacks=0 foldPending=5
[=ERROR=] 11.00 [X4LAN] SIMPROF phases slot3=60.0%(w0.0) idle=30.0%(w30.0) lost=10.0%(w0.0)
[=ERROR=] 11.00 [X4LAN] SIMPROF self 9fdef0=40.0%
[=ERROR=] 11.01 [X4LAN] SIMPROF fold slot3;ef4400;122b360;9fdef0 40
[=ERROR=] 11.01 [X4LAN] SIMPROF fold slot3;ef4400;122b360;9fdef0;abc0 20
[=ERROR=] 11.02 [X4LAN] SIMPROF fold idle;ef4400;kernelbase+1bdf0;ntdll!NtWaitForSingleObject 30
[=ERROR=] 11.02 [X4LAN] SIMPROF fold lost;[truncated];3e4f40;3e4f40;[ext] 6
[=ERROR=] 21.00 [X4LAN] SIMPROF window seconds=10.00 samples=4 rateHz=1000 depthMean=12.0 suspendUsMean=30.0 suspendUsMax=90.0 walkUsMean=2.0 overheadPct=4.00 truncated=0 scanned=0 lost=4 loading=0 failures=0 skipped=0 droppedFunctions=0 droppedStacks=2 foldPending=1
[=ERROR=] 21.01 [X4LAN] SIMPROF fold lost;[truncated];3e4f40;3e4f40;[ext] 4
[=ERROR=] 22.00 [X4LAN] FEATURE simprof DISABLED reason=SuspendThread failed err=5
'''


class SimprofReportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        labels = root / 'x4re' / 'labels'
        labels.mkdir(parents=True)
        (labels / 'merged.tsv').write_text(
            'ef4400\tU::MoveWorker::Run\tfunction\tmanual\thigh\t\t\ttime\n'
            '9fdef0\tU::UniverseFrameUpdate\tfunction\tmanual\tmedium\t\t\ttime\n'
            '6cf1570\tg_VbiTable\tdata\tmanual\thigh\t\t\ttime\n')
        (labels / 'time.tsv').write_text(
            '# comment\n9fdef0\tOld::Name\tfunction\tmanual\thigh\t\t\ttime\n'
            '3e4f40\tU::Component::Move\tfunction\tmanual\thigh\t\t\ttime\n')
        (labels / 'linux-xfer.tsv').write_text('3e4f40\tlinux;name\tfunction\tlinux-xfer\tlow\t\t\t\n')
        (labels / 'conflicts.tsv').write_text('abc0\tConflict::Name\tfunction\tx\tx\t\t\t\n')
        (root / 'x4re' / 'symbols.csv').write_text(
            'rva,va,name,kind,source,confidence,evidence\n'
            '0x122b360,0x14122b360,X4::GameClient::Move,function,rtti,high,x\n'
            '0xef4400,0x140ef4400,Wrong::Name,function,rtti,high,x\n')
        self.x4re = root / 'x4re'
        self.log = root / 'x4lan-debug.txt'
        self.log.write_text(LOG)
        self.root = root

    def tearDown(self):
        self.dir.cleanup()

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = simprof_report.main([str(self.log), '--x4re', str(self.x4re), *args])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_label_priority_and_kinds(self):
        names = simprof_report.load_labels(self.x4re)
        self.assertEqual(names[0xef4400], 'U::MoveWorker::Run')          # merged beats symbols.csv
        self.assertEqual(names[0x9fdef0], 'U::UniverseFrameUpdate')      # merged beats other TSVs
        self.assertEqual(names[0x3e4f40], 'U::Component::Move')          # TSV beats linux-xfer
        self.assertEqual(names[0x122b360], 'X4::GameClient::Move')       # symbols.csv fallback
        self.assertNotIn(0x6cf1570, names)                               # data labels ignored
        self.assertNotIn(0xabc0, names)                                  # conflicts.tsv ignored

    def test_folded_output_is_symbolized_and_aggregated(self):
        folded = self.root / 'out.folded'
        self.run_main('--folded', str(folded))
        lines = folded.read_text().splitlines()
        self.assertIn('slot3;U::MoveWorker::Run;X4::GameClient::Move;U::UniverseFrameUpdate 40', lines)
        self.assertIn('slot3;U::MoveWorker::Run;X4::GameClient::Move;U::UniverseFrameUpdate;fn_abc0 20', lines)
        self.assertIn('idle;U::MoveWorker::Run;kernelbase+1bdf0;ntdll!NtWaitForSingleObject 30', lines)
        self.assertIn('lost;[truncated];U::Component::Move;U::Component::Move;[ext] 10', lines)
        self.assertEqual(sum(int(line.rsplit(' ', 1)[1]) for line in lines), 100)
        for line in lines:  # FlameGraph format: frames joined by ';', count after the last space
            stack, count = line.rsplit(' ', 1)
            self.assertTrue(count.isdigit() and stack)

    def test_report_phases_top_lists_and_overhead(self):
        text = self.run_main('--top', '3')
        self.assertIn('simprof report: 100 samples', text)
        self.assertIn('windows=2 seconds=20.0 rateHz=1000 overheadPct(mean)=3.00 suspendUs(mean)=25.0', text)
        self.assertIn('droppedStacks=2', text)
        self.assertIn('DISABLED: SuspendThread failed err=5', text)
        self.assertRegex(text, r'slot3\s+60\.0%\s+0\.0%')
        self.assertRegex(text, r'idle\s+30\.0%\s+30\.0%')
        self.assertRegex(text, r'lost\s+10\.0%')
        self_section = text.split('top 3 by self samples')[1].split('top 3 by inclusive')[0]
        self.assertRegex(self_section.splitlines()[1], r'40\.0%\s+40\s+U::UniverseFrameUpdate')
        incl = text.split('top 3 by inclusive samples')[1]
        self.assertRegex(incl.splitlines()[1], r'90\.0%\s+90\s+U::MoveWorker::Run')
        # recursion counts once per stack for inclusive time
        names = simprof_report.symbolizer(simprof_report.load_labels(self.x4re), False)
        stacks, _, _ = simprof_report.parse([self.log])
        self.assertEqual(simprof_report.analyse(stacks, names)[4]['U::Component::Move'], 10)
        self.assertIn('[slot3] top 5 self', text)

    def test_phase_filter_and_rva_suffix(self):
        text = self.run_main('--phase', 'slot3', '--with-rva', '--per-phase', '0')
        self.assertIn('simprof report: 60 samples in phase slot3', text)
        self.assertIn('U::UniverseFrameUpdate@9fdef0', text)
        self.assertNotIn('idle', text.split('phase      share')[1])

    def test_plain_folded_input_and_missing_labels(self):
        plain = self.root / 'plain.folded'
        plain.write_text('slot6;ef4400;[ext] 7\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(simprof_report.main([str(plain), '--x4re', str(self.root / 'none')]), 0)
        self.assertIn('fn_ef4400', out.getvalue())

    def test_no_fold_lines_is_an_error(self):
        empty = self.root / 'empty.txt'
        empty.write_text('[=ERROR=] 1.00 [X4LAN] FRAMEPROBE frames=1\n')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(simprof_report.main([str(empty), '--x4re', str(self.x4re)]), 1)


if __name__ == '__main__':
    unittest.main()
