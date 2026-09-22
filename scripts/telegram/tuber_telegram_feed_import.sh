#!/usr/bin/env bash
# Tuber-Telegram: импорт кандидатов-каналов из фида соседних платформ.
#
# Перенос scripts/cron_bridge_import.sh (ТЗ-4): запускает
# `python3 -m tuber tg bridge import --feed <фид>`. Плановое задание 06:35 МСК
# (после экспорта Tuber-OS в 06:10). Тихий при норме, всегда код 0.
#
# Правило про повторный прогон: если ни одного нового канала, но часть уже
# известна (skipped_existing > 0) или отсеяна фильтром, это норма — тишина.
# Строка печатается, только если из непустого фида не опознано НИ ОДНОГО
# кандидата — признак разрыва формата.
#
# Тестовые хуки: TG_IMPORT_TEST_JSON, TG_IMPORT_CRON_LOG, TG_IMPORT_FED,
# TG_IMPORT_DB, TG_IMPORT_LIMIT (боевые прогоны их не задают).
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  exit 4
fi

PROJECT="${TUBER_TG_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_TG_LOG_DIR:-/root/.hermes/logs}"
LOG="${TG_IMPORT_CRON_LOG:-$LOG_DIR/tuber_telegram_feed_import.log}"
mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 0

DEFAULT_FEED="$PROJECT/data/exchange/external_candidates.jsonl"
LEGACY_FEED="/root/tuber-os/data/exchange/external_candidates.jsonl"
FEED="${TG_IMPORT_FED:-}"
if [ -z "$FEED" ]; then
    if [ -f "$DEFAULT_FEED" ]; then FEED="$DEFAULT_FEED"; else FEED="$LEGACY_FEED"; fi
fi
LIMIT="${TG_IMPORT_LIMIT:-1000}"

feed_rows=0
if [ -f "$FEED" ]; then
    feed_rows=$(grep -c '[^[:space:]]' "$FEED" 2>/dev/null || true)
fi
feed_rows=${feed_rows:-0}

ARGS=(--feed "$FEED" --limit "$LIMIT")
[ -n "${TG_IMPORT_DB:-}" ] && ARGS+=(--db "$TG_IMPORT_DB")

if [ "${TG_IMPORT_TEST_JSON+set}" = set ]; then
    out="$TG_IMPORT_TEST_JSON"
elif [ ! -f "$FEED" ]; then
    echo "Tuber-Telegram: фид Tuber-OS не найден — $FEED."
    echo "$(date '+%F %T') фид не найден: $FEED" >> "$LOG"
    exit 0
elif [ "$feed_rows" -eq 0 ]; then
    echo "Tuber-Telegram: фид Tuber-OS пуст — $FEED."
    echo "$(date '+%F %T') фид пуст: $FEED" >> "$LOG"
    exit 0
else
    out=$(timeout 900 "$TUBER_PYTHON" -m tuber tg bridge import "${ARGS[@]}" 2>>"$LOG" | tail -1)
fi
echo "$(date '+%F %T') $out" >> "$LOG"

"$TUBER_PYTHON" - "$out" "$feed_rows" <<'PY'
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ''
try:
    rows = int(sys.argv[2])
except (IndexError, ValueError):
    rows = 0

try:
    d = json.loads(raw)
except Exception:
    print('Tuber-Telegram: импорт фида не вернул итог (см. /root/.hermes/logs/tuber_telegram_feed_import.log).')
    sys.exit(0)
if not isinstance(d, dict):
    print('Tuber-Telegram: итог импорта фида не разобран.')
    sys.exit(0)

if d.get('error'):
    print(f"Tuber-Telegram: импорт фида не выполнен — {d['error']}.")
    sys.exit(0)
if d.get('errors'):
    errs = d['errors']
    first = errs[0] if isinstance(errs, list) and errs else errs
    print(f"Tuber-Telegram: импорт фида завершился с ошибками — {first}.")
    sys.exit(0)

def num(key):
    try:
        return int(d.get(key) or 0)
    except (TypeError, ValueError):
        return 0

imported = num('imported')
existing = num('skipped_existing')
filt = num('skipped_filter')
if rows > 0 and imported == 0 and existing == 0 and filt == 0:
    print(f"Tuber-Telegram: в фиде {rows} строк, но ни один кандидат не опознан — формат разошёлся.")
    sys.exit(0)
PY

exit 0
