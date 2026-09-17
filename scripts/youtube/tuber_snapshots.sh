#!/usr/bin/env bash
# Tuber-YouTube (монорепо): замеры просмотров по расписанию (перенос
# tuber-os tuber_snapshots.sh в монорепозиторий, ТЗ-5).
#
# В stdout не пишем ничего: планировщик не должен слать уведомление, в том числе
# при «прогон уже идёт». Код возврата CLI пробрасывается как есть.
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  exit 4
fi

PROJECT="${TUBER_YT_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_YT_LOG_DIR:-/root/.hermes/logs}"
LOG="${TUBER_YT_SNAPSHOTS_LOG:-$LOG_DIR/tuber_yt_snapshots.log}"
mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 3

ARGS=(snapshots)
[ -n "${TUBER_DB:-}" ] && ARGS+=(--db "$TUBER_DB")

TS="$(date '+%Y-%m-%d %H:%M:%S')"
output="$("$TUBER_PYTHON" -m tuber yt "${ARGS[@]}" 2>&1)"
code=$?
if [ -n "$output" ]; then
    printf '%s\n' "$output" | while IFS= read -r line || [ -n "$line" ]; do
        printf '%s %s\n' "$TS" "$line"
    done >> "$LOG"
fi
exit "$code"
