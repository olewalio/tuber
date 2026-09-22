#!/usr/bin/env bash
# Tuber-Telegram: дискавери каналов из собственных постов (мост источников).
#
# Перенос scripts/cron_bridge_discover.sh (ТЗ-4): запускает
# `python3 -m tuber tg discover`. Плановое задание 07:40 МСК. Тихий при норме,
# всегда код 0.
#
# Тестовые хуки: TG_DISCOVER_TEST_JSON, TG_DISCOVER_CRON_LOG, TG_DISCOVER_DB.
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  exit 4
fi

PROJECT="${TUBER_TG_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_TG_LOG_DIR:-/root/.hermes/logs}"
LOG="${TG_DISCOVER_CRON_LOG:-$LOG_DIR/tuber_telegram_discover.log}"
mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 0

ARGS=()
[ -n "${TG_DISCOVER_DB:-}" ] && ARGS+=(--db "$TG_DISCOVER_DB")

if [ "${TG_DISCOVER_TEST_JSON+set}" = set ]; then
    out="$TG_DISCOVER_TEST_JSON"
else
    out=$(timeout 900 "$TUBER_PYTHON" -m tuber tg discover "${ARGS[@]}" 2>>"$LOG" | tail -1)
fi
echo "$(date '+%F %T') $out" >> "$LOG"

"$TUBER_PYTHON" - "$out" <<'PY'
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ''
try:
    d = json.loads(raw)
except Exception:
    print('Tuber-Telegram: дискавери не вернул итог (см. /root/.hermes/logs/tuber_telegram_discover.log).')
    sys.exit(0)
if not isinstance(d, dict):
    print('Tuber-Telegram: итог дискавери не разобран.')
    sys.exit(0)
if d.get('error') or d.get('errors'):
    print(f"Tuber-Telegram: дискавери завершился с ошибкой — {d.get('error') or d.get('errors')}.")
    sys.exit(0)
PY

exit 0
