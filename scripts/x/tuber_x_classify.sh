#!/usr/bin/env bash
# Tuber-x: классификация постов моделью DeepSeek (ТЗ-6 задача 2).
#
# Что делает: один прогон `cli classify`. Дневной потолок CLASSIFY_DAILY_CAP
# (config.py, по умолчанию 400 постов/сутки) соблюдает сам CLI: если лимит не
# задан, он берёт ровно остаток потолка за текущие сутки. Поэтому в норме
# обёртку можно звать чаще одного раза в сутки — лишнего она не потратит.
#
# Ключ модели берётся ТОЛЬКО из окружения. Если DEEPSEEK_API_KEY не задан,
# аккуратно подгружаем его из /root/.hermes/.env, НЕ печатая значение в лог.
#
# Поведение: в норме молчит в stdout (весь вывод уходит в журнал
# /root/.hermes/logs/tuber_x_classify.log). Всегда завершается кодом 0: сбой
# прогона — забота сторожа, а не уведомления крона.
#
# Использование: tuber_x_classify.sh [лимит_постов]
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
LOG=/root/.hermes/logs/tuber_x_classify.log
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

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) classify${LIMIT:+ --limit $LIMIT} ==="
  if [ -n "$LIMIT" ]; then
    "$TUBER_PYTHON" -m tuber x classify --limit "$LIMIT"
  else
    "$TUBER_PYTHON" -m tuber x classify
  fi
} >>"$LOG" 2>&1
exit 0
