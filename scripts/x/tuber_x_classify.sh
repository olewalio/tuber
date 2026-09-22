#!/usr/bin/env bash
# Tuber-x: классификация постов моделью DeepSeek (ТЗ-6 задача 2).
#
# Что делает: один прогон `cli classify`. Дневной потолок CLASSIFY_DAILY_CAP
# (config.py, default 400 постов/сутки) соблюдает сам CLI: он берёт min(лимит
# прогона, остаток потолка). Поэтому обёртку можно звать чаще одного раза в
# сутки — лишнего она не потратит.
#
# ТЗ-B (поднять потолок X). Потолок задаётся средой, без правки кода:
#   * TUBER_X_CLASSIFY_DAILY_CAP   — постов/сутки (default в коде 400);
#   * TUBER_X_CLASSIFY_RUN_LIMIT   — постов за прогон, размазывает суточную
#     норму по 12 запускам (cron «20 */2 * * *»), чтобы первый прогон дня не
#     делал сотни вызовов модели подряд;
#   * TUBER_X_CLASSIFY_DAILY_USD_CAP — мягкий денежный предохранитель стадии.
#
# Фазы (решение владельца 20.09.2026):
#   фаза 1 (до 24.09.2026) — разобрать накопленный хвост 3 264 текста:
#       cap=2000/сутки, 250/прогон (~$0,31/сутки, ~2,6 % лимита $12);
#   фаза 2 (после разбора хвоста) — ровный режим ~1,3x притока:
#       cap=1200/сутки, 120/прогон.
# Откат = одна строка: убрать переменные ниже или выставить cap=400 — код не
# меняется, поведение возвращается к до-ТЗ-B. Глобальный стоп-флаг DeepSeek
# (/root/.hermes/.deepseek_halt) и сторож бюджета действуют поверх этого.
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

# Фаза 1 ТЗ-B: временно поднятый потолок для разбора хвоста. Значения можно
# переопределить извне (окружение крона) — явный приоритет за внешним значением.
TUBER_X_CLASSIFY_DAILY_CAP="${TUBER_X_CLASSIFY_DAILY_CAP:-2000}"
TUBER_X_CLASSIFY_RUN_LIMIT="${TUBER_X_CLASSIFY_RUN_LIMIT:-250}"
TUBER_X_CLASSIFY_DAILY_USD_CAP="${TUBER_X_CLASSIFY_DAILY_USD_CAP:-1.0}"
export TUBER_X_CLASSIFY_DAILY_CAP TUBER_X_CLASSIFY_RUN_LIMIT \
       TUBER_X_CLASSIFY_DAILY_USD_CAP

# Аргумент обёртки перекрывает лимит прогона; иначе берём TUBER_X_CLASSIFY_RUN_LIMIT.
LIMIT="${1:-$TUBER_X_CLASSIFY_RUN_LIMIT}"

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
