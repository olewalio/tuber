#!/usr/bin/env bash
# Tuber-Telegram: плановый сбор постов (web-режим, без аккаунтной квоты).
#
# Перенос scripts/cron_collect.sh проекта tuber-telegram (ТЗ-4): запускает
# `python3 -m tuber tg collect` в монорепозитории и разбирает итоговый JSON.
# Тихий при штатной работе: печатает строку только при реальном сбое, повторных
# сетевых ошибках, пустом сборе (ни один канал не взят), активном флуде аккаунта
# или неразобранном итоге.
#
# stdout уходит владельцу дословно; пустой stdout = владелец ничего не видит.
# Скрипт всегда завершается кодом 0: аномалия — не повод красить задание
# планировщика в «сбой».
#
# Тестовые хуки (без сети и без боевого сбора):
#   TG_TEST_JSON=<строка>  — не запускать сбор, взять готовый JSON из переменной;
#   TG_CRON_LOG=<путь>     — журнал вместо $LOG_DIR/tuber_telegram_collect.log.
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан, и
# «голый» python3 уходит на системный SQLite. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_TG_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_TG_LOG_DIR:-/root/.hermes/logs}"
LOG="${TG_CRON_LOG:-$LOG_DIR/tuber_telegram_collect.log}"
mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 0

if [ "${TG_TEST_JSON+set}" = set ]; then
    out="$TG_TEST_JSON"
else
    out=$(timeout 900 "$TUBER_PYTHON" -m tuber tg collect \
              --mode web --limit-channels 60 --deadline 780 2>>"$LOG" | tail -1)
fi
echo "$(date '+%F %T') $out" >> "$LOG"

"$TUBER_PYTHON" - "$out" <<'PY'
import sys, json
raw = sys.argv[1] if len(sys.argv) > 1 else ''
try:
    d = json.loads(raw)
except Exception:
    print('Tuber-Telegram: сборщик не вернул итог (см. /root/.hermes/logs/tuber_telegram_collect.log)')
    sys.exit(0)

def num(key):
    try:
        return int(d.get(key) or 0)
    except (TypeError, ValueError):
        return 0

ok = num('channels_ok')
fail = num('channels_fail')
err = num('errors')
new = num('posts_new')
upd = num('posts_upd')
flood = d.get('flood_until')

if flood:
    print(f"Tuber-Telegram: аккаунт под флудом до {flood}. Web-сбор продолжается.")

if fail > 0:
    print(f"ALERT Tuber-Telegram: сбор не удался — каналов ок {ok}, сбоев {fail}, ошибок {err}, новых {new}, обновлено {upd}.")
elif err >= 3 or (ok > 0 and err * 100 // ok >= 5):
    print(f"Tuber-Telegram: повторные ошибки сети при сборе — каналов ок {ok}, ошибок {err}, новых {new}, обновлено {upd}.")
elif ok == 0:
    print(f"ALERT Tuber-Telegram: сбор не взял ни одного канала — каналов ок 0, сбоев {fail}, ошибок {err}, новых {new}, обновлено {upd}.")
PY

exit 0
