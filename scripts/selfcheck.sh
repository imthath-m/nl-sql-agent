#!/usr/bin/env bash
# Run every module selfcheck offline (no API, no data). Non-zero exit on any failure.
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
fail=0
run() { # module [arg]
  if "$PY" -m "nlsql.$1" ${2:+"$2"} >/dev/null 2>&1; then echo "ok    $1"
  else echo "FAIL  $1"; "$PY" -m "nlsql.$1" ${2:+"$2"} 2>&1 | tail -5; fail=1; fi
}
for m in score executor retriever planner schema_link taxonomy cost judge gate trace; do run "$m"; done
for m in regression run_eval gate_eval run_phase5 review run_all; do run "$m" _selfcheck; done
for m in tune judge_check; do
  if [ -f "src/nlsql/$m.py" ]; then run "$m" _selfcheck; else echo "skip  $m (not present)"; fi
done
exit $fail
