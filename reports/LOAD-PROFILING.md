# Load profiling: what X4 does while it loads a game

Goal: find where the client's load time goes, so hot-join can be made shorter. Three
layers, from cheap to detailed:

| layer | what | where | output |
|---|---|---|---|
| 1 | X4's own stage times (`progressbar.xml`) | agent (`rust/x4mp-agent/src/loadstages.rs`), offline `dev/progressbar_delta.py` | `LOADSTAGES` line |
| 2 | per-thread CPU, I/O, RSS, faults, loading flag, every 250 ms | agent (`loadprof.rs`, `procstat.rs`) | `LOADPROF` rows, `LOADPROF SUMMARY`, `LOADPROF GROUP` |
| 3 | symbolised sampling profile | `perf record` on the VPS + `dev/perf_symbolize.py` | folded stacks (FlameGraph), top-N per thread, split by load phase |

Status: built and tested offline (unit tests, synthetic `perf script`, the real binary's
`.eh_frame`). **No native run yet.** All example lines below are illustrative of the
format, not measurements.

## Layer 1: stage times from progressbar.xml

X4 keeps one running average per load type in `<profile>/progressbar.xml` (`newgame`:
init/map/cutscenes/scripts/stations/jobs/starting; `savedgame`: init/map/scripts/init2/
map2/economylog/scripts2/patch/scripts3/jobs) with a count `loaded=N`. Only averages are
stored. One load's stage times follow from the file before and after that load if the
update rule is known:

- cumulative mean (expected): `avg' = (N*avg + x)/(N+1)`, so `x = (N+1)*avg' - N*avg`, exact;
- any running average `avg' = avg + w*(x - avg)`: `x = avg + (avg' - avg)/w`.

The rule cannot be read from the file alone. The agent measures the load's wall time `T`
itself (loading flag set -> cleared), and the stage sums `S` (before) and `S'` (after) give
the weight actually used: `w = (S' - S)/(T - S)`. A cumulative mean predicts `w = 1/N'`.
The `LOADSTAGES` line prints both, so **the first native run settles the rule**. If they
disagree by more than 25 %, the line adds `stages_w=` (stages under the implied weight);
use that instead of `stages=`. `negative=1` (a negative cumulative stage) also means the
rule is not a cumulative mean. If `loaded` did not increase (a cap), only `stages_w=` is
meaningful.

The XML layout is not documented and no copy was inspected for this work (the user's
`Documents\Egosoft` is off limits). The parser accepts: a load-type element carrying a
`loaded` attribute (named by `id`/`name`/`type`, else by its tag), with stages as child
elements (value from `time`/`duration`/`value`/`avg`/..., the only numeric attribute, or
the text) or as numeric attributes of the type element. Values are kept in the file's unit
(believed seconds: compare `sum=` with `measured_load_s=`; a ratio near 1000 means ms).
The run script keeps `progressbar.before.xml` / `progressbar.after.xml`, so a wrong layout
guess is fixable offline: `python dev/progressbar_delta.py --dump progressbar.after.xml`.

Agent lines (`X4MP_LOADSTAGES=0` disables; `X4MP_LOADSTAGES_FILE=/abs/path` overrides the
file, default `$XDG_CONFIG_HOME` or `$HOME/.config` + `/EgoSoft/X4/<-personalfolderid>/progressbar.xml`):

```
X4MP_AGENT LOADSTAGES before file=/home/x4server/.config/EgoSoft/X4/45842/progressbar.xml exists=1 types=newgame:3,savedgame:0
X4MP_AGENT LOADSTAGES type=newgame loaded=3->4 rule=cumulative loads=1 sum=36.912 negative=0 stages=init:1.900,map:8.400,cutscenes:0.300,scripts:6.100,stations:4.200,jobs:11.200,starting:4.812 measured_load_s=37.250 weight_implied=0.2480 weight_cumulative=0.2500 averages=init:2.000,...
```

The "before" copy is read right after the build check (under a second after `exec`); the
"after" copy from arming on, once it changed and reads the same twice (1 s apart), for up
to 120 s (`LOADSTAGES unchanged_after_s=120` otherwise).

Offline (any OS, e.g. Windows): copy the file before and after one load, then
`python dev/progressbar_delta.py before.xml after.xml --measured <stopwatch seconds>`.

## Layer 2: per-thread activity (agent)

On by default whenever the agent is enabled (`X4MP_AGENT=1`) and has verified the pinned
build; `X4MP_LOADPROF=0` disables it. Other knobs: `X4MP_LOADPROF_TAIL_S` (default 10),
`X4MP_LOADPROF_MAX_S` (900, hard stop), `X4MP_LOADPROF_TOP` (6 threads per row). Invalid
values refuse the agent, like its other settings.

The worker thread samples every 250 ms from start-up until the loading flag (VBI table
+0x130) has been clear for `TAIL_S` seconds after a load, or `MAX_S`. It reads only
`/proc/self/task/*/stat` (per-thread utime+stime, `comm`), `/proc/self/stat` (process CPU,
minor/major faults), `/proc/self/io`, `/proc/self/status` (VmRSS, VmHWM) and the loading
flag/game time that `observe()` already reads. Nothing in the engine is written or called;
no extra thread exists. The first row covers process start to the first sample; CPU, I/O
and faults are cumulative counters, so nothing before the first sample is lost.

```
X4MP_AGENT LOADPROF start process_age_s=0.412 mono=88213.517 interval_ms=250 clk_tck=100
X4MP_AGENT LOADPROF t=14.262 mono=88227.367 dt=0.250 load=1 cpu=3.60 rchar_mb=48.0 rd_mb=12.3 wr_mb=0.0 rss_mb=5210 minflt=8123 majflt=4 thr=97 top=JobThread_3/41301:0.96,X4/41234:0.92,JobThread_7/41305:0.88,... cost_us=410
X4MP_AGENT LOADPROF SUMMARY end=complete wall_s=52.10 first_sample_s=0.41 table_s=1.20 menu_s=none load_start_s=3.40 load_end_s=40.65 load_s=37.25 first_sim_s=41.15 load_start_mono=88216.505 load_end_mono=88253.755 first_sim_mono=88254.255 cpu_s=120.30 parallelism=2.31 load_cpu_s=104.10 load_parallelism=2.79 rchar_mb=4100.2 read_mb=2950.7 write_mb=3.1 peak_rss_mb=6120 minflt=1820345 majflt=812 threads_seen=97 samples=207
X4MP_AGENT LOADPROF GROUP name=JobThread threads=16 cpu_s=60.20 load_cpu_s=55.10 share=0.500 busiest_thread_cpu_s=4.10
X4MP_AGENT LOADPROF GROUP name=X4 threads=1 cpu_s=30.10 load_cpu_s=27.00 share=0.250 busiest_thread_cpu_s=30.10
```

Row fields: `t` process age (s), `mono` CLOCK_MONOTONIC (s; the clock of
`perf record -k CLOCK_MONOTONIC`), `load` = `n` engine not ready / `0` idle / `1` loading,
`cpu` cores busy in the interval, I/O in MiB per interval (`rchar` = all read() bytes
including page cache, `rd` = from storage), faults per interval, `top` = busiest threads
as `name/tid:cores`, `cost_us` = the sample's own cost.

Summary milestones (process age, s; 250 ms resolution):

- `table_s`: VBI table first readable (engine core up);
- `menu_s`: first idle sample with the GameClient present, before any load (heuristic for
  "at the main menu"; `none` when a load started first, e.g. `-module custom_creative`);
- `load_start_s` / `load_end_s` / `load_s`: loading flag first set / last cleared (a later
  load restarts the end); `load_s` is the `T` for layer 1;
- `first_sim_s`: first sample after the load end whose game time has advanced (first
  simulation frame, within 250 ms).

`parallelism` = process CPU / wall since process start; `load_parallelism` = CPU / wall
inside the loading window. `GROUP` lines fold `JobThread 12` etc. into one group
(a trailing number is dropped unless glued to a prefix shorter than 3 characters, so `X4`
stays). Process `cpu_s` includes threads that exited; group totals cover the threads seen.

**Overhead**: one sample with ~100 threads costs about 0.4 ms (release build, WSL2:
`cargo test --release -p x4mp-agent -- --ignored --nocapture loadprof_sample_cost` ->
"102 threads, read 397 us, read+row 436 us, at 4 Hz = 0.174% of one core"). Every row
logs `cost_us`, so the native cost is measured too. About 250 log lines (~60 KB) per load.

## Layer 3: symbolised sampling profile

### Unwinding: dwarf, not frame pointers

The Linux X4 build has no frame pointers: only 79 of 4,146 sampled functions (1.9 %) set up
`rbp` (`dev/perf_symbolize.py validate`). `perf record -g` (frame-pointer unwinding) would
give one- or two-frame stacks. Use `--call-graph dwarf` (stack copy unwound later with
`.eh_frame`, which covers every function). Cost: a stack copy per sample; at 99 Hz with
`dwarf,8192` about 8 KB per sample, roughly 3-7 MB/s with 4-8 busy threads (a 40 s load
gives a few hundred MB of `perf.data`, deleted after `perf script` unless `KEEP=1`). If
many stacks end in `[unknown]` roots, raise `STACK=16384`.

### Symbol sources (validated on `x4-re/bin-linux/X4`)

`python dev/perf_symbolize.py validate --binary ../x4-re/bin-linux/X4`:

```
.eh_frame_hdr search table entries: 99494
FDE functions: 99494
named: 29104 (29.3%)
  eh_frame: 70390          (unnamed: fn_<start>)
  dynsym: 23874            (FFI exports + statically linked libraries; exact)
  pair: 4268               (Windows labels via x4-re/data/xfer/pairs.tsv)
  win-labels-on-linux: 917
  label: 45                (hand labels with linux=0x... / '# linux' lines)
  of which low confidence (?): 501
spot 0x1212490 U::MoveWorker::Run: in FDE=True FDE start=0x1212490 size=0x8ea OK
spot 0x2024fc0 X4::FrameTick: in FDE=True FDE start=0x2024fc0 size=0x4af OK
(also XLib::VbiTable::Unregister, X4::GameClient::Init, AI::Gravidar::Run, PathingManager::Run: OK)
frame pointer prologue: 79/4146 sampled functions (1.9%)
```

The FDE parser is a ~70-line pure-Python reader (no pyelftools needed, so it also runs on
the VPS). Its count equals the `.eh_frame_hdr` table exactly, and its first 3,000 FDEs
agree with pyelftools. About 12,600 of the FDEs are GCC `.cold` fragments; they stay
`fn_<addr>`. Engine-named functions: about 5,200 (the rest of the names are library code).

Priority when several sources name one address: `.dynsym` > hand labels with a Linux
address > `linux-xfer.tsv` > Windows labels (`labels/*.tsv`, then `symbols.csv`) mapped
through `data/xfer/pairs.tsv` > `win-labels-on-linux.tsv` > `fn_<start>`. A low-confidence
label or pair gets a `?` suffix. Labels are read from `x4-re` read-only; the merged table
can be cached with `python dev/perf_symbolize.py table --binary ... --out .local/evidence-268-withheld`
(about 0.5 s to build, so `--binary` directly is fine too).

Caller frames are looked up at `ip - 1` (return address), so a call that ends a function is
not attributed to the next one. Addresses outside every FDE show as `X4!0x<ip>`; other DSOs
keep perf's symbol or become `[libname.so]`.

### Output

```
python dev/perf_symbolize.py fold --binary ../x4-re/bin-linux/X4 perf.txt \
    --agent-log agent.log --out .local/evidence-111-withheld --print
```

- `run1.folded`: FlameGraph text (`thread;root;...;leaf count`), threads grouped
  (`--no-group` keeps `JobThread 3` separate); `flamegraph.pl run1.folded > run1.svg`, or
  load it into speedscope.app;
- `run1.<phase>.folded` per phase: `pre`, `loading`, `loading:<stage>`, `post`, `after`.
  Phases come from the agent's `LOADPROF` rows (`mono=` matches perf's CLOCK_MONOTONIC
  time). The `loading:<stage>` windows are **approximate**: stage durations from
  `LOADSTAGES` laid end to end from `load_start_mono`, in file order;
- `run1.top.txt`: per thread group, top-N self functions with self% and inclusive%, plus
  top inclusive functions. Weight = sample count (`--by-period` for sample period).

## Run procedure on the VPS (native, scheduled separately)

Safety first:

- The VPS also runs the dedicated server. Use only the separate profile **45842**, whose
  `content.xml` disables `x4_lan_lab`. **Never touch profile 45841, and never the
  `x4mp-server` / `x4mp-steam` units.** The run reuses the logged-in Steam client of
  `x4mp-steam` (X4 refuses to start without one), as the fork probe did.
- X4 runs at `nice -n 19` (optionally `CPUS=2-5` to pin it), `perf record` at nice 5,
  `perf script` at nice 19. The script refuses to start if an X4 with profile 45842 runs
  already, and stops only the X4 process it started.
- A second X4 next to the server's X4 may hit X4's single-instance lock; then
  `x4-out.log` shows it and the run ends early. Nothing else is affected.

Steps:

1. On the PC (WSL Ubuntu-24.04), build and copy the agent:
   ```
   cd /mnt/c/Users/X4 Contributor 3/Documents/X4Mods/X4-Multiplayer-load-prof/rust
   CARGO_TARGET_DIR=/root/load-prof-target cargo build --release -p x4mp-agent
   # copy /root/load-prof-target/release/libx4mp_agent.so to the VPS as
   #   /home/x4server/x4/agent/libx4mp_agent-loadprof.so
   # and dev/linux_loadprof_run.sh to e.g. /root/linux_loadprof_run.sh
   ```
2. On the VPS, as root: check `perf --version` (install `linux-tools-$(uname -r)` or
   `linux-perf` if missing; `PERF=0` runs the agent layers only).
3. As root: `bash /root/linux_loadprof_run.sh` (defaults: `G=/home/x4server/x4/game`,
   `OUT=/home/x4server/x4/loadprof/<timestamp>`, `FREQ=99`, `STACK=8192`, `SECS=600`).
   It writes profile 45842's `content.xml` (and a minimal `config.xml` if absent), copies
   `progressbar.xml`, starts
   ```
   runuser -u x4server -- env HOME=/home/x4server DISPLAY=:10 SteamAppId=392160 SteamGameId=392160 \
     LD_LIBRARY_PATH=$G/lib X4MP_AGENT=1 X4MP_AGENT_LOG=$OUT/agent.log LD_PRELOAD=$AGENT \
     timeout -s KILL 600 nice -n 19 ./X4 -personalfolderid 45842 -module custom_creative -skipintro \
     -logfile loadprof-debug.txt -nocputhrottle -noworkshopsync
   ```
   then, as soon as the X4 PID exists (polled every 50 ms),
   `perf record -F 99 --call-graph dwarf,8192 -k CLOCK_MONOTONIC -p <pid> -o $OUT/perf.data`.
   It stops perf when the agent logs `LOADPROF SUMMARY` (load end + 10 s), waits up to
   130 s for `LOADSTAGES`, copies `progressbar.xml` again, stops its X4, and runs
   `perf script -F comm,pid,tid,time,period,event,ip,sym,dso --no-inline > perf.txt`.
4. Results in `$OUT`: `agent.log` (layers 1 and 2), `perf.txt` (layer 3 input),
   `progressbar.before.xml` / `progressbar.after.xml`, `x4-out.log`,
   `loadprof-debug.txt` (X4's own log), `perf-record.log` (check for "lost samples").
5. Copy `agent.log`, `perf.txt` and the two progressbar copies to the PC (under `.local/`,
   never committed: they are logs) and run `dev/perf_symbolize.py fold` as above.

What this measures: `-module custom_creative` starts a **new game** (progressbar type
`newgame`). A hot-joining client loads a **saved game** (`savedgame`). The tooling is
type-agnostic, but a headless saved-game load needs a trigger: X4's Lua `LoadGame(<slot>)`
after the first start, as `extension/x4_lan_lab/ui/seed_game.lua` does for `save_010`. A
small probe extension for profile 45842 (with a guard against reloading after the load,
since UI Lua restarts with the new game) is the next step. Until then compare the
`newgame` stages with the Windows `savedgame` averages (about 37 s, `jobs` 11.2 s).

### Reading the results

- `LOADSTAGES`: which stage dominates (`jobs`, `map`, `scripts`...). Check the rule line
  first (`weight_implied` against `weight_cumulative`).
- `LOADPROF SUMMARY`: `load_s` against `first_sim_s - load_end_s` (time to the first
  frame), `load_parallelism` (near 1 = single-threaded load, near the core count = CPU
  bound and parallel), `read_mb` against `rchar_mb` (storage against page cache), `majflt`.
- `LOADPROF` rows over time: phases where `cpu` drops to ~1 with one thread on top are
  serial sections; low `cpu` plus high `rd_mb` is I/O bound; `rss_mb` growth shows
  allocation phases.
- `GROUP` lines: how much of the work runs on the JobThread pool against the main thread.
- `*.top.txt` per phase / stage, and the flame graphs: which functions burn the serial
  sections. Names with `?` are low-confidence; `fn_<addr>` can be looked up in Ghidra
  (`x4-re/ghidra`), and the pair table maps it to the Windows RVA.

## Windows: the same profile with WPR/WPA (no tooling)

`wpr.exe` (Windows Performance Recorder) ships with Windows 10/11. WPA (Windows
Performance Analyzer) is a separate install (Microsoft Store or the Windows ADK, which also
brings `xperf`). Run from an elevated prompt, with X4 closed and only test profiles:

1. `wpr -start CPU -filemode` (CPU sampling with stacks, about 1 kHz; add `-start DiskIO
   -start FileIO` for I/O). Start X4 and load the save. Stopwatch the load for layer 1.
2. When the game runs: `wpr -stop C:\temp\x4load.etl`.
3. Open the `.etl` in WPA: Graph Explorer -> Computation -> **CPU Usage (Sampled)**,
   filter Process `X4.exe`, group by Thread Name / Thread ID, then Stack. The
   **CPU Usage (Precise)** table gives per-thread running time and waits (context
   switches), the equivalent of the `LOADPROF` rows; Storage -> Disk Usage / File I/O for
   reads. Select the loading interval on the timeline to restrict all tables to it.
4. X4.exe has no PDB, so its frames show as `X4.exe!<unknown>` (or `X4.exe+0x...`). Map
   them to names with RVAs: `RVA = address - X4.exe image base` (the base is in the trace's
   Image load events; WPA's "Images" table, or
   `xperf -i x4load.etl -a dumper` -> `I-DCStart`/`Image` lines with BaseAddress).
   `xperf -i x4load.etl -o samples.txt -a dumper -stackwalk` dumps raw sample addresses
   with their stacks. Look each RVA up in `x4-re/labels/merged.tsv` (Windows RVAs,
   function starts) or `x4-re/symbols.csv`; function boundaries come from X4.exe's
   `.pdata` (read by `x4-re/scripts/xfer/extract.py`), so an address maps to the entry with the
   largest start <= RVA inside its `.pdata` range. ASLR moves the base each run; RVAs
   stay fixed for build 23660954.
5. Layer 1 on Windows: copy `Documents\Egosoft\X4\<id>\progressbar.xml` before and after
   the load (read-only copies) and run `dev/progressbar_delta.py` with `--measured`.

## Files

- `rust/x4mp-agent/src/procstat.rs`: `/proc` parsers (fixtures in its tests);
- `rust/x4mp-agent/src/loadprof.rs`: layer 2 core (rows, milestones, summary, groups);
- `rust/x4mp-agent/src/loadstages.rs`: progressbar.xml parser, delta and rule check;
- `rust/x4mp-agent/src/agent.rs`: `/proc` reading and the worker integration;
  `config.rs`: `X4MP_LOADPROF*`, `X4MP_LOADSTAGES*`;
- `rust/x4mp-agent/tests/inert.rs`: preload without `X4MP_AGENT=1` starts no thread and
  writes nothing; enabled in a foreign process it only logs START and REFUSED;
- `dev/progressbar_delta.py`, `dev/perf_symbolize.py`, `dev/linux_loadprof_run.sh`;
- `tests/test_load_profiling.py` (synthetic progressbar, `.eh_frame` and `perf script`
  input; the real-binary FDE checks run only where `X4_LINUX_BIN` or
  `../x4-re/bin-linux/X4` exists).

Tests: `cargo build -p x4mp-agent && cargo test -p x4mp-agent && cargo clippy -p x4mp-agent --all-targets`
(WSL; `cargo test` does not rebuild the cdylib the inertness test preloads, so build
first), and `python -m unittest discover -s tests -p test_load_profiling.py`.
