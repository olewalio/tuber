#!/usr/bin/env bash
# Приёмка ТЗ-5 §3.2: три живых прогона коллекторов на КОПИИ единой базы.
#
# Проверяется, что сбор дописывает данные в ядро и числа растут без ошибок:
#   python3 -m tuber yt collect   (YouTube)
#   python3 -m tuber x  collect   (X)
#   python3 -m tuber tg collect   (Telegram)
#
# Боевая единая база НЕ трогается: путь к базе передаётся ЯВНО через --db
# (правило волны после инцидента ТЗ-4). Прогоны идут с урезанными лимитами,
# чтобы не тратить квоты и не грузить сеть.
#
# Запуск: scripts/acceptance/tz5_live_runs.sh <копия_базы>
set -uo pipefail

PY="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
PROJECT="${TUBER_PROJECT:-/root/tuber}"
DB="${1:-}"
if [ -z "$DB" ] || [ ! -f "$DB" ]; then
  echo "использование: $0 <путь к копии единой базы>" >&2
  exit 2
fi

cd "$PROJECT" || exit 3

counts() {
  "$PY" - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
for t in ("content", "metric_snapshot", "score", "run_log"):
    try:
        print(f"  {t}: {c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
    except Exception as exc:
        print(f"  {t}: ошибка {exc}")
c.close()
PY
}

echo "=== ДО ==="
counts

stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""
echo "=== YouTube: tuber yt collect --queries 1 --budget 180 ==="
"$PY" -m tuber yt collect --queries 1 --budget 180 --no-classify --db "$DB" 2>&1 | tail -20
echo "rc_yt=${PIPESTATUS[0]}"

echo ""
echo "=== X: tuber x collect --tier A --max-accounts ${X_MAX_ACCOUNTS:-15} ==="
"$PY" -m tuber x collect --tier A --max-accounts "${X_MAX_ACCOUNTS:-15}" --db "$DB" 2>&1 | tail -20
echo "rc_x=${PIPESTATUS[0]}"

echo ""
echo "=== Telegram: tuber tg collect --mode web --handle ${TG_HANDLE:-chatgptv} ==="
"$PY" -m tuber tg collect --mode web --handle "${TG_HANDLE:-chatgptv}" --limit-channels 1 --deadline 90 --db "$DB" 2>&1 | tail -20
echo "rc_tg=${PIPESTATUS[0]}"

echo ""
echo "=== ПОСЛЕ ($stamp) ==="
counts
