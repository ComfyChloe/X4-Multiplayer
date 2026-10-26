#!/bin/bash
# Load profiling run on the VPS: a separate headless X4 (user x4server, profile 45842,
# x4_lan_lab disabled, nice 19) with the agent preloaded, plus `perf record` on that
# X4 process during the load. See reports/LOAD-PROFILING.md.
#
# Run as root. Touches only $OUT and profile 45842. Never profile 45841, never the
# x4mp-server / x4mp-steam units, never another X4 process.
#
#   AGENT=/home/x4server/x4/agent/libx4mp_agent-loadprof.so dev/linux_loadprof_run.sh
#
# Environment (defaults in brackets):
#   G       game directory                 [/home/x4server/x4/game]
#   AGENT   agent library                  [/home/x4server/x4/agent/libx4mp_agent-loadprof.so]
#   OUT     output directory               [/home/x4server/x4/loadprof/<timestamp>]
#   FREQ    perf sampling frequency (Hz)   [99]
#   STACK   dwarf stack bytes per sample   [8192]
#   PERF    1 = record with perf, 0 = agent only [1]
#   SECS    hard limit for the X4 process  [600]
#   CPUS    optional taskset CPU list for X4 (e.g. 2-5) [unset]
set -u
G=${G:-/home/x4server/x4/game}
AGENT=${AGENT:-/home/x4server/x4/agent/libx4mp_agent-loadprof.so}
OUT=${OUT:-/home/x4server/x4/loadprof/$(date +%Y%m%d-%H%M%S)}
FREQ=${FREQ:-99}
STACK=${STACK:-8192}
PERF=${PERF:-1}
SECS=${SECS:-600}
CPUS=${CPUS:-}
U=x4server
P=/home/$U/.config/EgoSoft/X4/45842

[ "$(id -u)" = 0 ] || { echo "run as root (perf on another user's process needs it)"; exit 2; }
[ -f "$AGENT" ] || { echo "agent library $AGENT missing"; exit 2; }
[ -x "$G/X4" ] || { echo "no X4 in $G"; exit 2; }
if [ "$PERF" = 1 ]; then
    command -v perf >/dev/null || { echo "perf missing (apt install linux-tools-\$(uname -r) or linux-perf), or PERF=0"; exit 2; }
fi
own_x4() {  # X4 processes of $U started with -personalfolderid 45842 (never 45841)
    for pid in $(pgrep -u "$U" -x X4); do
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- '-personalfolderid 45842 ' && echo "$pid"
    done
}
[ -z "$(own_x4)" ] || { echo "an X4 with profile 45842 already runs: $(own_x4)"; exit 1; }

mkdir -p "$OUT"
chown "$U": "$OUT"
runuser -u "$U" -- mkdir -p "$P"
runuser -u "$U" -- tee "$P/content.xml" >/dev/null <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<content>
  <extension id="x4_lan_lab" enabled="false"/>
</content>
XML
[ -f "$P/config.xml" ] || printf '<?xml version="1.0" encoding="UTF-8"?>\n<root><autosave>false</autosave><uisafemode>false</uisafemode><fullscreen>false</fullscreen></root>\n' \
    | runuser -u "$U" -- tee "$P/config.xml" >/dev/null
cp "$P/progressbar.xml" "$OUT/progressbar.before.xml" 2>/dev/null || echo "no progressbar.xml yet (first load in 45842)"

PIN=()
[ -n "$CPUS" ] && PIN=(taskset -c "$CPUS")
cd "$G" || exit 2
runuser -u "$U" -- env HOME=/home/$U DISPLAY=:10 SteamAppId=392160 SteamGameId=392160 LD_LIBRARY_PATH="$G/lib" \
    X4MP_AGENT=1 X4MP_AGENT_LOG="$OUT/agent.log" LD_PRELOAD="$AGENT" \
    timeout -s KILL "$SECS" "${PIN[@]}" nice -n 19 ./X4 -personalfolderid 45842 -module custom_creative -skipintro \
    -logfile loadprof-debug.txt -nocputhrottle -noworkshopsync > "$OUT/x4-out.log" 2>&1 &
XPID=""
for _ in $(seq 1 100); do XPID=$(own_x4 | head -1); [ -n "$XPID" ] && break; sleep 0.05; done
[ -n "$XPID" ] || { echo "X4 did not start; see $OUT/x4-out.log"; exit 1; }
echo "$XPID" > "$OUT/x4.pid"
echo "X4 pid=$XPID out=$OUT"

PERFPID=""
if [ "$PERF" = 1 ]; then
    # dwarf unwinding: the Linux X4 build has no frame pointers (about 2% of functions
    # set up rbp), so -g with frame pointers would give one-frame stacks.
    nice -n 5 perf record -F "$FREQ" --call-graph "dwarf,$STACK" -k CLOCK_MONOTONIC -p "$XPID" \
        -o "$OUT/perf.data" > "$OUT/perf-record.log" 2>&1 &
    PERFPID=$!
fi

# Until the agent's profiler ends (load end + tail), then up to 130 s for LOADSTAGES.
for _ in $(seq 1 $((SECS * 2))); do
    grep -q 'LOADPROF SUMMARY' "$OUT/agent.log" 2>/dev/null && break
    grep -q 'REFUSED' "$OUT/agent.log" 2>/dev/null && { echo "agent refused:"; grep REFUSED "$OUT/agent.log"; break; }
    kill -0 "$XPID" 2>/dev/null || { echo "X4 exited early"; break; }
    sleep 0.5
done
[ -n "$PERFPID" ] && { kill -INT "$PERFPID" 2>/dev/null; wait "$PERFPID" 2>/dev/null; }
for _ in $(seq 1 130); do
    grep -q -E 'LOADSTAGES (type=|unchanged|changed_without)' "$OUT/agent.log" 2>/dev/null && break
    grep -q -E 'LOADSTAGES unavailable' "$OUT/agent.log" 2>/dev/null && break
    kill -0 "$XPID" 2>/dev/null || break
    sleep 1
done
cp "$P/progressbar.xml" "$OUT/progressbar.after.xml" 2>/dev/null
cp "$P/loadprof-debug.txt" "$OUT/" 2>/dev/null
kill "$XPID" 2>/dev/null; sleep 6; kill -9 "$XPID" 2>/dev/null; sleep 1
ps -p "$XPID" >/dev/null && echo "WARNING: X4 $XPID still running" || echo "X4 $XPID stopped"

grep -E 'VERIFIED|ARMED|REFUSED|LOADSTAGES|LOADPROF (start|SUMMARY|GROUP)' "$OUT/agent.log"
if [ -n "$PERFPID" ] && [ -f "$OUT/perf.data" ]; then
    tail -3 "$OUT/perf-record.log"
    # Unwinding happens here; keep it at low priority. --no-inline avoids addr2line.
    nice -n 19 perf script -i "$OUT/perf.data" -F comm,pid,tid,time,period,event,ip,sym,dso --no-inline \
        > "$OUT/perf.txt" 2> "$OUT/perf-script.log" \
        || nice -n 19 perf script -i "$OUT/perf.data" -F comm,pid,tid,time,period,event,ip,sym,dso \
        > "$OUT/perf.txt" 2> "$OUT/perf-script.log"
    ls -la "$OUT/perf.data" "$OUT/perf.txt"
    [ "${KEEP:-0}" = 1 ] || rm -f "$OUT/perf.data"  # large; perf.txt holds what the symbolizer needs (KEEP=1 keeps it)
fi
chown -R "$U": "$OUT"
echo "done: $OUT"
