#!/usr/bin/env bash
# Tuber-Telegram: экспорт фида X/YouTube для соседних платформ (мост источников).
#
# Перенос scripts/cron_bridge_export.sh (ТЗ-4): запускает
# `python3 -m tuber tg bridge export`. Плановое задание 06:30 МСК. Тихий при
# норме: одна строка в stdout только при аномалии. Код возврата всегда 0.
#
# Тестовые хуки: TG_BRIDGE_TEST_JSON, TG_BRIDGE_CRON_LOG, TG_BRIDGE_DB,
# TG_BRIDGE_OUT (боевые прогоны их не задают).
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  exit 4
fi

PROJECT="${TUBER_TG_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_TG_LOG_DIR:-/root/.hermes/logs}"
LOG="${TG_BRIDGE_CRON_LOG:-$LOG_DIR/tuber_telegram_feed_export.log}"
mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 0

ARGS=(--out "${TG_BRIDGE_OUT:-$PROJECT/data/exchange/tg_candidates.jsonl}")
[ -n "${TG_BRIDGE_DB:-}" ] && ARGS+=(--db "$TG_BRIDGE_DB")

if [ "${TG_BRIDGE_TEST_JSON+set}" = set ]; then
    out="$TG_BRIDGE_TEST_JSON"
else
    out=$(timeout 900 "$TUBER_PYTHON" -m tuber tg bridge export "${ARGS[@]}" 2>>"$LOG" | tail -1)
fi
echo "$(date '+%F %T') $out" >> "$LOG"

"$TUBER_PYTHON" - "$out" <<'PY'
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ''
try:
    d = json.loads(raw)
except Exception:
    print('Tuber-Telegram: экспорт фида не вернул итог (см. /root/.hermes/logs/tuber_telegram_feed_export.log).')
    sys.exit(0)
if not isinstance(d, dict):
    print('Tuber-Telegram: итог экспорта фида не разобран.')
    sys.exit(0)
if d.get('error') or d.get('errors'):
    print(f"Tuber-Telegram: экспорт фида завершился с ошибкой — {d.get('error') or d.get('errors')}.")
    sys.exit(0)
written = d.get('written')
if written is None:
    print('Tuber-Telegram: в итоге экспорта нет числа записанных строк — не разобран.')
    sys.exit(0)
try:
    written = int(written)
except (TypeError, ValueError):
    print('Tuber-Telegram: число записанных строк не разобрано — формат итога разошёлся.')
    sys.exit(0)
if written == 0:
    print('Tuber-Telegram: экспорт фида записал 0 строк — соседним проектам нечего отдавать.')
    sys.exit(0)
PY

exit 0
