#!/usr/bin/env bash
# Tuber-YouTube (монорепо): импорт YouTube-каналов из фида Telegram
# (перенос tuber-os cron_bridge_import.sh, ТЗ-5). Тихий при норме, код 0.
#
# Читает фид, который пишет scripts/telegram/tuber_telegram_feed_export.sh
# в data/exchange/tg_candidates.jsonl (fallback — legacy-путь tuber-telegram).
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  exit 4
fi

PROJECT="${TUBER_YT_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_YT_LOG_DIR:-/root/.hermes/logs}"
LOG="${TUBER_YT_FEED_IMPORT_LOG:-$LOG_DIR/tuber_yt_feed_import.log}"
mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 0

DEFAULT_FEED="$PROJECT/data/exchange/tg_candidates.jsonl"
LEGACY_FEED="/root/tuber-telegram/data/exchange/external_candidates.jsonl"
FEED="${TU_IMPORT_FED:-}"
if [ -z "$FEED" ]; then
    if [ -f "$DEFAULT_FEED" ]; then FEED="$DEFAULT_FEED"; else FEED="$LEGACY_FEED"; fi
fi
LIMIT="${TU_IMPORT_LIMIT:-300}"

feed_rows=0
if [ -f "$FEED" ]; then
    feed_rows=$(grep -c '[^[:space:]]' "$FEED" 2>/dev/null || true)
fi
feed_rows=${feed_rows:-0}

ARGS=(candidates-import --feed "$FEED" --limit "$LIMIT")
[ -n "${TU_CRON_DB:-}" ] && ARGS+=(--db "$TU_CRON_DB")

if [ "${TU_IMPORT_TEST_JSON+set}" = set ]; then
    out="$TU_IMPORT_TEST_JSON"
elif [ ! -f "$FEED" ]; then
    echo "Tuber-YouTube: фид Telegram не найден — $FEED."
    echo "$(date '+%F %T') фид не найден: $FEED" >> "$LOG"
    exit 0
elif [ "$feed_rows" -eq 0 ]; then
    echo "Tuber-YouTube: фид Telegram пуст — $FEED."
    echo "$(date '+%F %T') фид пуст: $FEED" >> "$LOG"
    exit 0
else
    out=$(timeout 900 "$TUBER_PYTHON" -m tuber yt "${ARGS[@]}" 2>>"$LOG" | tail -1)
fi
echo "$(date '+%F %T') $out" >> "$LOG"

/usr/bin/python3 - "$out" "$feed_rows" <<'PY'
import json, sys
raw = sys.argv[1] if len(sys.argv) > 1 else ''
try:
    rows = int(sys.argv[2])
except (IndexError, ValueError):
    rows = 0
try:
    d = json.loads(raw)
except Exception:
    print('Tuber-YouTube: импорт фида не вернул итог (см. tuber_yt_feed_import.log).')
    sys.exit(0)
if not isinstance(d, dict):
    print('Tuber-YouTube: итог импорта фида не разобран.')
    sys.exit(0)
if d.get('error'):
    print(f"Tuber-YouTube: импорт фида не выполнен — {d['error']}.")
    sys.exit(0)
if d.get('errors'):
    errs = d['errors']
    print(f"Tuber-YouTube: импорт фида завершился с ошибками — {errs[0] if isinstance(errs, list) and errs else errs}.")
    sys.exit(0)
def num(k):
    try: return int(d.get(k) or 0)
    except (TypeError, ValueError): return 0
ids, new, known = num('youtube_ids'), num('new'), num('known')
if rows > 0 and ids == 0:
    print(f"Tuber-YouTube: в фиде {rows} строк, но ни одного видео не найдено — формат разошёлся.")
    sys.exit(0)
if rows > 0 and new == 0 and known == 0:
    print(f"Tuber-YouTube: импорт фида не разрешил ни одного канала — строк {rows}.")
PY
exit 0
