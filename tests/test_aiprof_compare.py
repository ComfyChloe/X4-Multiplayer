"""Regression coverage for native A/B report edge cases (synthetic log fixtures)."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'dev/aiprof_compare.py'


class AiProfCompareTests(unittest.TestCase):
    def test_new_random_draws_cannot_look_unchanged_against_zero_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'alternate.txt'
            for normal_draws in (0, 1):
                with self.subTest(normal_draws=normal_draws):
                    log.write_text(
                        '[X4LAN] AIPROF mode=census frames=60 runs=1 rngRuns=0\n'
                        f'[X4LAN] AIPROF mode=normal frames=60 runs=1 rngRuns={normal_draws}\n',
                        encoding='utf-8',
                    )
                    result = subprocess.run(
                        [sys.executable, str(SCRIPT), str(log), '--modes', 'census,normal'],
                        capture_output=True, text=True, check=True,
                    )
                    line = next(line for line in result.stdout.splitlines() if 'random-drawing runs' in line)
                    self.assertEqual('<-- differs' in line, normal_draws != 0, line)
                    if normal_draws:
                        self.assertIn('+inf%', line)
                    else:
                        self.assertIn('+0.0%', line)


if __name__ == '__main__':
    unittest.main()
