#!/usr/bin/env bash
# Tuber-x: добор полного текста неполных постов (ТЗ-5 задача 1).
# НЕ чаще раза в час: перечитывает ленты аккаунтов из очереди.
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан, и
# «голый» python3 уходит на системный SQLite. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi


PROJECT=/root/tuber
LOG=/root/.hermes/logs/tuber_x_fulltext.log

cd "$PROJECT" || { echo "нет каталога $PROJECT" >>/dev/stderr; exit 3; }
mkdir -p "$(dirname "$LOG")"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) fulltext ==="
  "$TUBER_PYTHON" -m tuber x fulltext
} >>"$LOG" 2>&1
