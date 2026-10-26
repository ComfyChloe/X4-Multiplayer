"""Aggregate [X4LAN] AIPROF census lines from an X4 debug log (ai-driver stage 0).
usage: python aiprof_report.py <x4lan-debug.txt> [label] [--skip N]  (--skip drops the first N windows)"""
import re, sys, statistics as st
args = [a for a in sys.argv[1:] if not a.startswith('--')]
skip = 0
for i, a in enumerate(sys.argv):
    if a == '--skip': skip = int(sys.argv[i + 1])
path = args[0]; label = args[1] if len(args) > 1 else path
kv = re.compile(r'(\w+)=(-?\d+\.?\d*)')
sv = re.compile(r'(\S+?)=(\d+\.\d+)ms/(\d+\.?\d*)')
rows, scripts, sectors, script_rows, drive = [], [], [], [], []
for line in open(path, encoding='utf-8', errors='replace'):
    if 'AIDRIVE mode=' in line:
        d = {k: float(v) for k, v in kv.findall(line.split('AIDRIVE', 1)[1])}
        d['_mode'] = line.split('AIDRIVE mode=', 1)[1].split()[0]
        d['_idx'] = len(rows)   # the AIPROF window this AIDRIVE line follows
        drive.append(d)
    elif 'AIPROF mode=' in line and ' frames=' in line:
        rows.append({k: float(v) for k, v in kv.findall(line.split('AIPROF',1)[1])})
    elif 'AIPROF scripts ' in line:
        text = line.split('AIPROF scripts ',1)[1].strip()
        scripts.append(text)
        script_rows.append((len(rows) - 1, [(n, float(ms), float(runs)) for n, ms, runs in sv.findall(text)]))
    elif 'AIPROF sectors ' in line:
        sectors.append(line.split('AIPROF sectors ',1)[1].strip())
rows_all = rows
rows = rows[skip:]
if not rows:
    print(f'{label}: no AIPROF census lines'); sys.exit(1)
def col(k): return [r[k] for r in rows if k in r]
def line(k, unit='', fns=('mean','median','max')):
    v = col(k)
    if not v: return
    out = []
    if 'mean' in fns: out.append(f'mean {st.mean(v):.3f}')
    if 'median' in fns: out.append(f'med {st.median(v):.3f}')
    if 'max' in fns: out.append(f'max {max(v):.3f}')
    print(f'  {k:16s} {", ".join(out)} {unit}')
n = len(rows)
tot_frames = sum(col('frames'))
print(f'=== {label}: {n} windows (~{n}s), {tot_frames:.0f} sim frames total')
print('AI phase:')
line('share'); line('ms', '(per frame, NOT trustworthy timing)'); line('maxMs')
line('runs', '(contexts run / frame)'); line('irqRuns'); line('passes'); line('maxPasses', fns=('max',))
line('loopFrames', fns=('mean','max'))
print('Wakes / events (per frame):')
line('extWakes'); line('timedDue'); line('wakeOwn'); line('wakeForeign')
line('evOwn'); line('evForeign'); line('evCue'); line('evDirector')
line('hazard', '(fraction of runs that touch a foreign sector/cue)')
line('keyless', '(runs with no sector = serial bucket)')
print('Per-sector parallelism (timing NOT trustworthy here):')
line('sectors', '(active AI sectors / frame)'); line('secSumMs'); line('secMaxMs'); line('serialMs')
line('speedup', '(ideal sum/max)'); line('maxShare', '(largest sector share)')
line('fleets'); line('fleetSpeedup')
line('dropped', fns=('max',))
# foreign-wake and foreign-touch rates as fractions
if col('fleetResolver') and max(col('fleetResolver')) > 0:
    print('Fleet-root keying:')
    line('fleetMaxShare', '(largest fleet-root sector share)'); line('wakeFleetSame', '(foreign wakes inside one fleet root)')
    line('evFleetSame'); line('evPendingForeign', '(events to a still-queued foreign context)')
    fs, fw0 = sum(col('wakeFleetSame')), sum(col('wakeForeign'))
    if fw0: print(f'  foreign wakes that fleet keying makes own-bucket: {100*fs/fw0:.1f}%')
fw, wo = sum(col('wakeForeign')), sum(col('wakeOwn'))
ef, eo, ec = sum(col('evForeign')), sum(col('evOwn')), sum(col('evCue'))
if fw + wo: print(f'  foreign-wake rate: {100*fw/(fw+wo):.2f}%  ({fw:.0f}/{fw+wo:.0f})')
if ef + eo + ec: print(f'  foreign+cue event rate: {100*(ef+ec)/(ef+eo+ec):.2f}%  (foreign {ef:.0f}, cue {ec:.0f}, own {eo:.0f})')
if scripts: print('top scripts (last window): ' + scripts[-1][:300])
if sectors: print('top sectors (last window): ' + sectors[-1][:200])
# whole-run script ranking: per-window top lists weighted by the window's frames (scripts outside a
# window's top 8 are missing from that window, so this ranks, it does not total exactly)
agg = {}
for idx, items in script_rows:
    if idx < skip or idx >= len(rows_all): continue
    frames = rows_all[idx].get('frames', 0.0)
    for name, ms, runs in items:
        a = agg.setdefault(name, [0.0, 0.0, 0])
        a[0] += ms * frames; a[1] += runs * frames; a[2] += 1
if agg:
    total = sum(v[0] for v in agg.values()) or 1.0
    print('script ranking over the run (ms summed over frames, share of listed, runs, windows in top 8):')
    for name, (ms, runs, wins) in sorted(agg.items(), key=lambda kv: -kv[1][0])[:15]:
        print(f'  {name:40s} {ms:9.1f} ms {100*ms/total:5.1f}%  runs {runs:9.0f}  windows {wins}')
# stage-1 driver windows (AIDRIVE): per-window counts (driverFrames, refusals, pass counts) are summed,
# per-frame means (cmp channels, branch counters) are averaged over the windows
drive = [d for d in drive if d['_idx'] > skip]
if drive:
    modes = sorted({d['_mode'] for d in drive})
    def dsum(k): return sum(d.get(k, 0.0) for d in drive)
    def dmean(k): v = [d[k] for d in drive if k in d]; return st.mean(v) if v else 0.0
    print(f'stage-1 driver ({", ".join(modes)}): {len(drive)} windows, {dsum("driverFrames"):.0f} driver frames, '
          f'preflight refusals {dsum("preflightRefusals"):.0f}, loop logs {dsum("loopLogs"):.0f}, bad caller {dsum("badCaller"):.0f}')
    print(f'  per frame: globalApplied {dmean("globalApplied"):.2f}, attentionRestarts {dmean("attentionRestarts"):.2f}, '
          f'removed {dmean("removed"):.2f}, buckets {dmean("buckets"):.2f}, deferred {dmean("deferred"):.2f}')
    clean, dirty = dsum('cmpClean'), dsum('cmpDirty')
    if clean + dirty:
        p = clean + dirty
        print(f'  compare: {p:.0f} passes, no observed order hazard {100*clean/p:.2f}%, dirty {dirty:.0f}; '
              f'unchecked (large pass or missing event detail) {dsum("cmpUnchecked"):.0f}')
        print(f'  channels per frame: nullLost {dmean("cmpNullLost"):.3f}, wakeAbsorb {dmean("cmpWakeAbsorb"):.3f}, '
              f'eventPending {dmean("cmpEventPending"):.3f}, eventRan {dmean("cmpEventRan"):.3f}, postCancel {dmean("cmpPostCancel"):.3f}, '
              f'deferredImmediate {dmean("cmpDeferredImmediate"):.3f}')
        print(f'  random order: {dsum("cmpRngPasses"):.0f} passes ({100*dsum("cmpRngPasses")/p:.2f}%) ran drawing contexts '
              f'in another relative order; wakes put back in original order in {dsum("wakeReordered"):.0f} passes '
              f'({100*dsum("wakeReordered")/p:.2f}%), skipped {dsum("wakeOrderSkipped"):.0f}')
