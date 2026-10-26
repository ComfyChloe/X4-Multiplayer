"""Role-keyed RNG stream seeding: signature sync, Lua logging and draw-count comparison (offline only)."""
import contextlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from dev import compare_divergence as cd  # noqa: E402

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    LuaRuntime = None

SOURCE = ROOT / "extension/x4_lan_lab/ui/engine_hooks.lua"
RUST = ROOT / "native/hooks/src/rng.rs"
MANIFEST = ROOT / "native/hooks/signatures.json"


class SignatureSyncTests(unittest.TestCase):
    def test_every_rng_check_is_pinned_in_the_manifest(self):
        text = RUST.read_text(encoding="utf-8")
        block = text.split("const CHECKS: &[(usize, usize, &str)] = &[", 1)[1].split("];", 1)[0]
        checks = {(int(rva, 16), int(length), digest)
                  for rva, length, digest in re.findall(r'\(0x([0-9a-f]+), (\d+), "([0-9a-f]{64})"\)', block)}
        self.assertEqual(len(checks), 29)
        sites = {(site["rva"], site["length"], site["sha256"]) for site in json.loads(MANIFEST.read_text())["sites"]}
        self.assertLessEqual(checks, sites)

    def test_rng_module_never_patches_code_bytes(self):
        text = RUST.read_text(encoding="utf-8")
        self.assertNotIn("VirtualProtect", text)
        self.assertNotIn("WriteProcessMemory", text)
        self.assertEqual(text.count("ptr::write_volatile(state_slot"), 1)


class LcgTests(unittest.TestCase):
    def test_python_lcg_matches_engine_and_stage1_evidence(self):
        seed = cd.role_seed(123456789, "MoveWorker")
        self.assertEqual(seed, 0xF3DA54E488294596)
        # Stage 1 native runs C and D both logged MoveWorker rng 28acd4a7fac17879 first.
        self.assertEqual(cd.lcg_steps_between(seed, 0x28ACD4A7FAC17879, 1000), 150)
        state = seed
        for _ in range(77):
            state = cd.lcg_step(state)
        self.assertEqual(cd.lcg_steps_between(seed, state, 100), 77)
        self.assertIsNone(cd.lcg_steps_between(seed, state, 76))


def advance(state, count):
    for _ in range(count):
        state = cd.lcg_step(state)
    return state


def rng_line(frame, roles, phases=(0, 0, 0, 0, 0), pool=2, ticks=None, tick0=None, markers=1,
             seed="00000000075bcd15", run="r1", session="s1"):
    parts = [f"[X4LAN] RNGSTREAMS run={run} session={session} observer={frame} v=1 frame={frame} seed={seed}"]
    session_seed = int(seed, 16)
    names = {"mw": "MoveWorker", "path": "PathingManager", "mt": "MassTraffic", "rm": "RegionManager",
             "grav": "Gravidar"}
    names.update({f"j{index:02d}": f"JobThread/{index:02d}" for index in range(32)})

    def token(key, value):
        steps, flag, seeded = value if isinstance(value, tuple) else (value, "q", 1)
        state = advance(cd.role_seed(session_seed, names[key]), steps)
        return f"{key}={steps}:{state:016x}:0:{flag}:{seeded}"

    parts.append(token("mw", roles.get("mw", 0)))
    parts += [f"mw_{p}={v}" for p, v in zip("abcox", phases)]
    parts.append(f"phases={markers}")
    if ticks:
        parts.append(f"mw_tick0={tick0} mw_ticks={','.join(map(str, ticks))}")
    parts += [token(key, roles.get(key, 0)) for key in ("path", "mt", "rm", "grav")]
    jobs = [roles.get(f"j0{i}", 0) for i in range(pool)]
    parts.append(f"pool={pool} pool_steps={sum(j if isinstance(j, int) else j[0] for j in jobs)}")
    parts += [token(f"j0{i}", roles.get(f"j0{i}", 0)) for i in range(pool)]
    return " ".join(parts) + "\n"


class ComparatorTests(unittest.TestCase):
    def write(self, folder, name, lines):
        path = Path(folder) / name
        path.write_text("".join(lines), encoding="utf-8")
        return path

    def test_identical_streams_and_role_divergence(self):
        host = [rng_line(30, {"mw": 100, "rm": 5}, (10, 80, 10, 0, 0), ticks=list(range(1, 31)), tick0=1),
                rng_line(60, {"mw": 200, "rm": 9}, (20, 160, 20, 0, 0), ticks=[3] * 30, tick0=31)]
        guest = [rng_line(30, {"mw": 100, "rm": 5}, (10, 80, 10, 0, 0), ticks=list(range(1, 31)), tick0=1),
                 rng_line(60, {"mw": 200, "rm": 12}, (20, 160, 20, 0, 0), ticks=[3] * 30, tick0=31)]
        with tempfile.TemporaryDirectory() as folder:
            same = cd.compare_rngstreams(cd.read_rngstreams(self.write(folder, "a.log", host)),
                                         cd.read_rngstreams(self.write(folder, "b.log", host)))
            self.assertIsNone(same["first"])
            self.assertIn("No draw-count divergence", cd.rng_report(same))
            result = cd.compare_rngstreams(cd.read_rngstreams(self.write(folder, "a.log", host)),
                                           cd.read_rngstreams(self.write(folder, "c.log", guest)))
        self.assertEqual((result["first"]["frame"], result["first"]["role"]), (60, "rm"))
        self.assertEqual(result["stats"]["rm"]["first"]["window"], (4, 7))
        report = cd.rng_report(result)
        self.assertIn("FIRST DRAW DIVERGENCE frame=60 role=rm (RegionManager)", report)
        self.assertIn("window_draws=4/7", report)

    def test_moveworker_phase_and_exact_tick(self):
        host = [rng_line(30, {"mw": 90}, (10, 70, 10, 0, 0), ticks=[3] * 30, tick0=1),
                rng_line(60, {"mw": 180}, (20, 140, 20, 0, 0), ticks=[3] * 30, tick0=31)]
        guest_ticks = [3] * 30
        guest_ticks[11] = 5
        guest = [rng_line(30, {"mw": 90}, (10, 70, 10, 0, 0), ticks=[3] * 30, tick0=1),
                 rng_line(60, {"mw": 182}, (20, 142, 20, 0, 0), ticks=guest_ticks, tick0=31)]
        with tempfile.TemporaryDirectory() as folder:
            result = cd.compare_rngstreams(cd.read_rngstreams(self.write(folder, "a.log", host)),
                                           cd.read_rngstreams(self.write(folder, "b.log", guest)))
        self.assertEqual(result["first"]["role"], "mw")
        self.assertEqual(result["phase_first"]["phases"], ["b"])
        self.assertEqual(result["tick"], dict(frame=42, host=3, guest=5))
        report = cd.rng_report(result)
        self.assertIn("PHASE mw_b first=60 host=140 guest=142", report)
        self.assertIn("EXACT MoveWorker tick=42 host_draws=3 guest_draws=5", report)

    def test_busy_lost_pool_and_state_warnings(self):
        host = [rng_line(30, {"mw": 10, "grav": (4, "b", 2), "path": (7, "ql", 1)}, pool=2)]
        guest = [rng_line(30, {"mw": 10, "grav": (4, "q", 3), "path": (9, "q", 1)}, pool=3)]
        broken = host[0].replace("mw=10:", "mw=10:").split(" ")
        for index, part in enumerate(broken):
            if part.startswith("rm="):
                steps, state, rest = part[3:].split(":", 2)
                broken[index] = "rm=%s:%016x:%s" % (steps, int(state, 16) ^ 1, rest)
        with tempfile.TemporaryDirectory() as folder:
            result = cd.compare_rngstreams(cd.read_rngstreams(self.write(folder, "a.log", host)),
                                           cd.read_rngstreams(self.write(folder, "b.log", guest)))
            mismatch = cd.compare_rngstreams(cd.read_rngstreams(self.write(folder, "c.log", [" ".join(broken)])),
                                             cd.read_rngstreams(self.write(folder, "d.log", host)))
        report = cd.rng_report(result)
        self.assertIsNone(result["first"], "a lost count is never compared")
        self.assertTrue(result["stats"]["path"]["lost"])
        self.assertEqual(result["stats"]["grav"]["busy"], 1)
        self.assertIn("pool sizes differ host=2 guest=3", report)
        self.assertIn("grav=2/3", report)
        self.assertEqual(mismatch["stats"]["rm"]["state_mismatch"], 30)

    def test_split_pool_lines_are_merged_by_frame(self):
        def split(line):
            head, pool = line.rstrip("\n").split(" pool=", 1)
            frame = re.search(r" frame=(\d+)", head).group(1)
            prefix = head.split(" v=1 ", 1)[0].replace("RNGSTREAMS", "RNGPOOL")
            return [head + "\n", f"{prefix} frame={frame} pool={pool}\n"]

        host = split(rng_line(30, {"mw": 4, "j01": 6})) + split(rng_line(60, {"mw": 8, "j01": 9}))
        guest = split(rng_line(30, {"mw": 4, "j01": 6})) + split(rng_line(60, {"mw": 8, "j01": 11}))
        with tempfile.TemporaryDirectory() as folder:
            log = cd.read_rngstreams(self.write(folder, "a.log", host))
            self.assertEqual((log["records"][1]["pool"], log["records"][1]["pool_steps"]), (2, 9))
            self.assertEqual(log["records"][1]["roles"]["j01"]["steps"], 9)
            result = cd.compare_rngstreams(log, cd.read_rngstreams(self.write(folder, "b.log", guest)))
            self.assertEqual((result["first"]["frame"], result["first"]["role"]), (60, "j01"))
            self.assertIn("role=j01 (JobThread/01)", cd.rng_report(result))
            with self.assertRaises(cd.ProbeLogError):
                cd.read_rngstreams(self.write(folder, "c.log", host + [host[1]]))

    def test_unavailable_and_malformed_records(self):
        unavailable = "[X4LAN] RNGSTREAMS run=r1 session=s1 observer=30 unavailable=1 reason=thread\n"
        with tempfile.TemporaryDirectory() as folder:
            log = cd.read_rngstreams(self.write(folder, "a.log", [unavailable, rng_line(60, {"mw": 1})]))
            self.assertEqual((log["unavailable"], len(log["records"])), (1, 1))
            for bad in (rng_line(30, {}).replace(":q:", ":z:"), rng_line(30, {}).replace("mw_a=0", "mw_a=x"),
                        rng_line(30, {}).replace("v=1", "v=2")):
                with self.subTest(bad=bad[:60]), self.assertRaises(cd.ProbeLogError):
                    cd.read_rngstreams(self.write(folder, "b.log", [bad]))
            with self.assertRaises(cd.ProbeLogError):
                cd.read_rngstreams(self.write(folder, "c.log", [rng_line(60, {}), rng_line(30, {})]))

    def test_derived_moveworker_steps_from_statehash(self):
        seed = 123456789
        start = cd.role_seed(seed, "MoveWorker")

        def statehash(frame, steps):
            rng = advance(start, steps)
            return (f"[X4LAN] STATEHASH run=r session=s frame={frame} h={rng ^ 1:016x} identity={'2' * 16} "
                    f"pose={'3' * 16} health={'4' * 16} rng={rng:016x} time={'5' * 16} seed={seed:016x} "
                    "ships=1 weak=0 fresh=1 complete=1 gt=1.0 dt=0.1\n")

        with tempfile.TemporaryDirectory() as folder:
            host = cd.read_statehash(self.write(folder, "a.log", [statehash(30, 150), statehash(60, 400)]))
            guest = cd.read_statehash(self.write(folder, "b.log", [statehash(30, 150), statehash(60, 410)]))
            rows = cd.derive_moveworker_steps(host, 10000)
            self.assertEqual(rows, [(30, 150), (60, 400)])
            report = cd.derived_report(rows, cd.derive_moveworker_steps(guest, 10000))
            self.assertIn("FIRST DERIVED DIVERGENCE frame=60 host=400 guest=410", report)
            self.assertEqual(cd.derive_moveworker_steps(guest, 100)[0], (30, None))
            text_path = Path(folder) / "report.txt"
            with contextlib.redirect_stdout(io.StringIO()):
                code = cd.main([str(Path(folder) / "a.log"), str(Path(folder) / "b.log"), "--statehash",
                                "--derive-rng-steps", "--walk-cap", "10000", "--report", str(text_path)])
            self.assertEqual(code, 0)
            self.assertIn("FIRST DERIVED DIVERGENCE", text_path.read_text())


@unittest.skipUnless(LuaRuntime, "LuaJIT unavailable")
class LuaRngLoggingTests(unittest.TestCase):
    def runtime(self, setup=""):
        from test_engine_hooks import MOCK
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute(MOCK)
        lua.execute('''phaseCalls=0
         native.rng_streams=function() return rngText,rngReason end
         native.rng_phases=function() phaseCalls=phaseCalls+1; return phaseResult,phaseReason end
         rngText='v=1 frame=30 seed=00000000075bcd15 mw=5:0123456789abcdef:0:q:0'
         phaseResult=true''')
        lua.execute(setup)
        lua.execute(SOURCE.read_text(encoding="utf-8"))
        return lua

    def logs(self, lua):
        return [lua.eval("logs")[i] for i in range(1, len(lua.eval("logs")) + 1)]

    def test_streams_logged_with_each_sample_and_phases_installed(self):
        lua = self.runtime()
        lua.eval("events['X4LAN.Loaded']")()
        self.assertEqual(lua.eval("phaseCalls"), 1)
        self.assertIn("[X4LAN] RNGPHASES installed=1 slots=2,5", self.logs(lua))
        lua.eval("callback")(30, 100.0, 0.1, "0123456789abcdef", "00000000075bcd15")
        lines = [line for line in self.logs(lua) if "RNGSTREAMS" in line]
        self.assertEqual(lines, ["[X4LAN] RNGSTREAMS run=default session=test-build observer=30 "
                                 "v=1 frame=30 seed=00000000075bcd15 mw=5:0123456789abcdef:0:q:0"])
        lua.execute("X4LAN_CONFIG.engine_hash_budget_ms=nil")
        lua.execute("wallDt=10")
        lua.eval("callback")(60, 100.0, 0.1, "0123456789abcdef", "00000000075bcd15")
        self.assertEqual(sum("RNGSTREAMS" in line for line in self.logs(lua)), 2, "logged for incomplete captures too")

    def test_pool_tokens_are_logged_on_their_own_line(self):
        lua = self.runtime("rngText=rngText..' pool=2 pool_steps=3 j00=1:0000000000000001:0:q:2 j01=2:0000000000000002:0:q:2'")
        lua.eval("events['X4LAN.Loaded']")()
        lua.eval("callback")(30, 100.0, 0.1, "0123456789abcdef", "00000000075bcd15")
        logs = self.logs(lua)
        stream = [line for line in logs if "RNGSTREAMS" in line]
        pool = [line for line in logs if "RNGPOOL" in line]
        self.assertEqual(len(stream), 1)
        self.assertNotIn("pool=", stream[0])
        self.assertEqual(pool, ["[X4LAN] RNGPOOL run=default session=test-build observer=30 frame=30 pool=2 "
                                "pool_steps=3 j00=1:0000000000000001:0:q:2 j01=2:0000000000000002:0:q:2"])

    def test_unavailable_streams_and_refused_phases_do_not_disable(self):
        lua = self.runtime("rngText=false;rngReason='RNG streams unavailable';phaseResult=false;phaseReason='VBI phase slot occupied'")
        lua.eval("events['X4LAN.Loaded']")()
        lua.eval("callback")(30, 100.0, 0.1, "0123456789abcdef", "00000000075bcd15")
        logs = "\n".join(self.logs(lua))
        self.assertIn("RNGPHASES installed=0 reason=VBI%20phase%20slot%20occupied", logs)
        self.assertIn("observer=30 unavailable=1 reason=RNG%20streams%20unavailable", logs)
        self.assertNotIn("HOOKS_DISABLED", logs)
        self.assertFalse(lua.eval("X4LAN_ENGINE_HOOKS.failed"))

    def test_phase_markers_can_be_disabled_by_config(self):
        lua = self.runtime("X4LAN_CONFIG.engine_rng_phases=false")
        lua.eval("events['X4LAN.Loaded']")()
        self.assertEqual(lua.eval("phaseCalls"), 0)
        self.assertNotIn("RNGPHASES", "\n".join(self.logs(lua)))


if __name__ == "__main__":
    unittest.main()
