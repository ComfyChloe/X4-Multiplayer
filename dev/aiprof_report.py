"""Aggregate [X4LAN] AIPROF census lines from an X4 debug log (ai-driver stage 0).
usage: python aiprof_report.py <x4lan-debug.txt> [label] [--skip N]  (--skip drops the first N windows)"""
import re, sys, statistics as st
args = [a for a in sys.argv[1:] if not a.startswith('--')]
skip = 0
for i, a in enumerate(sys.argv):
    if a == '--skip': skip = int(sys.argv[i + 1])
path = args[0]; label = args[1] if len(args) > 1 else path
kv = re.compile(r'(\w+)=(-?\d+\.?\d*)')
rows, scripts, sectors = [], [], []
for line in open(path, encoding='utf-8', errors='replace'):
    if 'AIPROF mode=census' in line:
        rows.append({k: float(v) for k, v in kv.findall(line.split('AIPROF',1)[1])})
    elif 'AIPROF scripts ' in line:
        scripts.append(line.split('AIPROF scripts ',1)[1].strip())
    elif 'AIPROF sectors ' in line:
        sectors.append(line.split('AIPROF sectors ',1)[1].strip())
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
fw, wo = sum(col('wakeForeign')), sum(col('wakeOwn'))
ef, eo, ec = sum(col('evForeign')), sum(col('evOwn')), sum(col('evCue'))
if fw + wo: print(f'  foreign-wake rate: {100*fw/(fw+wo):.2f}%  ({fw:.0f}/{fw+wo:.0f})')
if ef + eo + ec: print(f'  foreign+cue event rate: {100*(ef+ec)/(ef+eo+ec):.2f}%  (foreign {ef:.0f}, cue {ec:.0f}, own {eo:.0f})')
if scripts: print('top scripts (last window): ' + scripts[-1][:300])
if sectors: print('top sectors (last window): ' + sectors[-1][:200])
