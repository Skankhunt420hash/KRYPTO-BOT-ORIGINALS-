#!/usr/bin/env bash
# Laeuft automatisch per Cron auf dem Server, alle paar Minuten.
# Holt neue Auftraege (jobs/pending/*.sh) von GitHub, fuehrt sie aus,
# schreibt das Ergebnis zurueck nach GitHub (jobs/done/<name>.log).
# Niemand muss sich dafuer einloggen -- einmal mit setup_autopilot.sh
# eingerichtet, laeuft das von selbst.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

LOCKFILE=/tmp/nemesis_autopilot.lock
exec 9>"$LOCKFILE"
flock -n 9 || exit 0   # laeuft schon ein Durchlauf -> diesen ueberspringen

echo "[$(date -u +%FT%TZ)] autopilot: pruefe auf neue Auftraege" >> autopilot.log
git pull -q origin main --no-rebase >> autopilot.log 2>&1 || { echo "git pull fehlgeschlagen" >> autopilot.log; exit 0; }

shopt -s nullglob
mkdir -p jobs/pending jobs/done jobs/archive

for job in jobs/pending/*.sh; do
  name=$(basename "$job" .sh)
  echo "[$(date -u +%FT%TZ)] Start: $name" >> autopilot.log

  {
    echo "=== $name gestartet $(date -u +%FT%TZ) ==="
    bash "$job"
    rc=$?
    echo "=== $name fertig $(date -u +%FT%TZ) (exit $rc) ==="
  } > "jobs/done/${name}.log" 2>&1

  git mv -f "$job" "jobs/archive/${name}.sh" 2>/dev/null || mv -f "$job" "jobs/archive/${name}.sh"
  git add -A jobs/done jobs/archive
  git commit -q -m "autopilot: $name fertig" || true
  if ! git push -q origin main 2>>autopilot.log; then
    echo "push fehlgeschlagen, hole neuen Stand und versuche nochmal" >> autopilot.log
    git pull -q origin main --no-rebase >> autopilot.log 2>&1 || true
    git push -q origin main >> autopilot.log 2>&1 || echo "push endgueltig fehlgeschlagen" >> autopilot.log
  fi
  echo "[$(date -u +%FT%TZ)] Fertig: $name" >> autopilot.log
done
