#!/usr/bin/env bash
# Tuber-Telegram: классификация постов ИИ-каналов моделью DeepSeek (ТЗ-G).
#
# Что делает: один прогон `python3 -m tuber tg classify`. Суточные потолки
# расхода (`TG_CLASSIFY_DAILY_CAP`, default 150 постов; `TG_CLASSIFY_USD_CAP`,
# default 0.40 USD) соблюдает сам CLI: при достижении любого из них прогон
# аккуратно завершается строкой `TG-CAP: …`. Поэтому обёртку можно звать чаще
# одного раза в сутки — лишнего она не потратит (кэш по text_hash).
#
# Расписание: `50 */2 * * *` (каждые 2 часа, смещено от X-классификации в :20).
#
# Ключ модели берётся ТОЛЬКО из окружения. Если DEEPSEEK_API_KEY не задан,
# аккуратно подгружаем его из /root/.hermes/.env, НЕ печатая значение в лог.
# Без ключа CLI печатает ALERT и завершается кодом 0.
#
# Поведение: весь вывод уходит в /root/.hermes/logs/tuber_tg_classify.log.
# Всегда завершается кодом 0: сбой прогона — забота сторожа, а не крона.
#
# Использование: tuber_tg_classify.sh [лимит_постов]
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (в cron PATH урезан).
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT=/root/tuber
LOG=/root/.hermes/logs/tuber_tg_classify.log
ENV_FILE=/root/.hermes/.env

LIMIT="${1:-}"

cd "$PROJECT" || { echo "нет каталога $PROJECT" >>/dev/stderr; exit 3; }
mkdir -p "$(dirname "$LOG")"

# Ключ модели — только из окружения; при отсутствии грузим из приватного .env.
# Значение нигде не печатаем (ни в stdout, ни в журнал).
load_key() {
  local name="$1" value
  eval "value=\${$name:-}"
  [ -n "$value" ] && return 0
  [ -f "$ENV_FILE" ] || return 0
  value="$(sed -n "s/^${name}=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r')"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  [ -n "$value" ] && export "$name=$value"
  return 0
}
load_key DEEPSEEK_API_KEY

# Потолки расхода. Замер 20.09.2026: 83 поста = $0.0062, то есть ~$0.00008 за пост.
# Бэклог ИИ-телеграма — 5 533 поста; при дефолтных 150/сутки хвост разбирался бы
# пять недель. Поэтому боевая обёртка держит 1500 постов и $0.50 в сутки: хвост
# уходит за ~4 суток, дальше работает ровный поток (~122 новых поста/сутки).
export TG_CLASSIFY_DAILY_CAP="${TG_CLASSIFY_DAILY_CAP:-1500}"
export TG_CLASSIFY_USD_CAP="${TG_CLASSIFY_USD_CAP:-0.50}"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) tg classify${LIMIT:+ --limit $LIMIT} ==="
  if [ -n "$LIMIT" ]; then
    "$TUBER_PYTHON" -m tuber tg classify --limit "$LIMIT"
  else
    "$TUBER_PYTHON" -m tuber tg classify
  fi
} >>"$LOG" 2>&1
exit 0
