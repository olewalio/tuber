#!/usr/bin/env bash
# ТЗ-46: приёмка разбора очереди кандидатов и мили повышения на КОПИИ базы.
#
# Боевую базу не трогаем: копия создаётся `tuber db backup`, все шаги идут по
# копии. Сеть нужна (t.me, YouTube Data API), DeepSeek НЕ зовётся.
#
# Порядок: разбор → повышение (+ первый сбор) → снимок метрик → отчёт.
# Итог: таблица вердиктов и примеры повышённых авторов в «сливках».
#
# Переопределения: TUBER_BACKUP_DB (готовая копия), TUBER_WORK_DB (рабочая
# копия), TUBER_QUEUE_X_BUDGET (по умолчанию 0 — X в hold), TUBER_PYTHON.
set -uo pipefail

PROJECT="${TUBER_PROJECT:-/root/tuber}"
P="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
BACKUP_DB="${TUBER_BACKUP_DB:-}"
WORK_DB="${TUBER_WORK_DB:-$PROJECT/data/acceptance-tz46.db}"
X_BUDGET="${TUBER_QUEUE_X_BUDGET:-0}"

cd "$PROJECT" || exit 1

if [ -z "$BACKUP_DB" ]; then
  echo "нужен TUBER_BACKUP_DB=<копия базы> (создаётся: python3 -m tuber db backup --dir DIR)" >&2
  exit 2
fi
cp "$BACKUP_DB" "$WORK_DB" && rm -f "$WORK_DB-wal" "$WORK_DB-shm" || exit 1
export TUBER_DB="$WORK_DB"
echo "рабочая копия: $WORK_DB"

echo "=== разбор очереди ==="
"$P" -m tuber queue review --platform youtube --workers 4
"$P" -m tuber queue review --platform telegram --workers 6
"$P" -m tuber queue review --platform x --x-budget "$X_BUDGET"
"$P" -m tuber queue review --platform web

echo "=== повышение (сухой прогон) ==="
"$P" -m tuber queue promote --dry-run | tail -c 300; echo

echo "=== повышение + первый сбор ==="
"$P" -m tuber queue promote --collect

echo "=== снимок метрик автора ==="
"$P" -m tuber rating slivki capture

echo "=== отчёт ==="
"$P" -m tuber queue report --top 12
