#!/usr/bin/env bash
# Tuber-YouTube (монорепо): экспорт внешних кандидатов (Telegram/X) из описаний
# видео для соседней платформы Telegram (перенос tuber-os cron_bridge_export.sh,
# ТЗ-5). Тихий при норме: одна строка в stdout только при аномалии.
#
# Код возврата (ТЗ-13): норма — пустой stdout и код 0; провал команды,
# нераспарсенный JSON, пустой stdout или 0 записанных строк — человеческая
# строка-аномалия в stdout и НЕНУЛЕВОЙ код (чтобы крон-джоб не показывал «ok»).
#
# Фид пишется в ЕДИНЫЙ каталог монорепо data/exchange/ и читается обёрткой
# импорта Telegram (scripts/telegram/tuber_telegram_feed_import.sh).
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  exit 4
fi

PROJECT="${TUBER_YT_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_YT_LOG_DIR:-/root/.hermes/logs}"
LOG="${TUBER_YT_FEED_EXPORT_LOG:-$LOG_DIR/tuber_yt_feed_export.log}"
mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 0

OUT="${TU_EXPORT_OUT:-$PROJECT/data/exchange/external_candidates.jsonl}"
mkdir -p "$(dirname "$OUT")"

if [ "${TU_EXPORT_TEST_JSON+set}" = set ]; then
    out="$TU_EXPORT_TEST_JSON"
    cmd_rc=0
else
    out=$(timeout 900 "$TUBER_PYTHON" -m tuber yt candidates-export --out "$OUT" 2>>"$LOG" | tail -1)
    cmd_rc=$?
fi
echo "$(date '+%F %T') $out" >> "$LOG"

/usr/bin/python3 - "$out" "$cmd_rc" <<'PY'
import json, sys
raw = sys.argv[1] if len(sys.argv) > 1 else ''
try:
    cmd_rc = int(sys.argv[2])
except (IndexError, ValueError):
    cmd_rc = 0

# Провал самой команды: крон-джоб обязан показать ошибку, а не «ok».
if cmd_rc != 0:
    print(f'Tuber-YouTube: команда экспорта фида завершилась с кодом {cmd_rc} '
          '(см. tuber_yt_feed_export.log).')
    sys.exit(1)
# Пустой/нераспарсенный stdout — тоже провал, а не «норма».
if not raw:
    print('Tuber-YouTube: экспорт фида не вернул итог (см. tuber_yt_feed_export.log).')
    sys.exit(1)
try:
    d = json.loads(raw)
except Exception:
    print('Tuber-YouTube: экспорт фида не вернул итог (см. tuber_yt_feed_export.log).')
    sys.exit(1)
if not isinstance(d, dict):
    print('Tuber-YouTube: итог экспорта фида не разобран.')
    sys.exit(1)
if d.get('error') or d.get('errors'):
    print(f"Tuber-YouTube: экспорт фида завершился с ошибкой — {d.get('error') or d.get('errors')}.")
    sys.exit(1)
written = d.get('written')
try:
    written = int(written)
except (TypeError, ValueError):
    print('Tuber-YouTube: число записанных строк не разобрано — формат итога разошёлся.')
    sys.exit(1)
if written == 0:
    print('Tuber-YouTube: экспорт фида записал 0 строк — соседям нечего отдавать.')
    sys.exit(1)
PY
exit $?
