#!/usr/bin/env bash
# Tuber (монорепо): объединённая выдача по трём платформам (ТЗ-5 §2, ТЗ-5-доп-2).
#
# Планировщик Hermes для заданий `no_agent: true` доставляет владельцу РОВНО то,
# что обёртка напечатала в stdout (пустой stdout = владелец не видит ничего).
# Поэтому:
#   * `tuber report --compact` печатает в stdout КОМПАКТНУЮ сводку (≤3500 знаков):
#     заголовок, пункты по каждой платформе (русское описание, цифры, ПОЛНАЯ
#     ссылка), сквозной сюжет строкой, подвал со счётчиком скрытых и путём к
#     полному файлу. Места делятся СПРАВЕДЛИВО: у каждого раздела с данными есть
#     минимум 1 пункт, остаток бюджета раздаётся round-robin; при нехватке места
#     укорачивается только описание пункта (с многоточием), ссылка и цифры целы —
#     владелец проверяет ссылки кликом;
#   * ПОЛНЫЙ отчёт по-прежнему сохраняется файлом (--save --out) и целиком
#     идёт в журнал;
#   * при неуспехе печатается строка `ALERT Tuber report: …` и код возврата 0
#     (молчание при неуспехе запрещено).
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ALERT Tuber report: интерпретатор контура не найден: $TUBER_PYTHON"
  exit 0
fi

PROJECT="${TUBER_PROJECT:-/root/tuber}"
REPORTS_DIR="${TUBER_REPORTS_DIR:-$PROJECT/reports}"
LOG_DIR="${TUBER_LOG_DIR:-/root/.hermes/logs}"
LOG="${TUBER_REPORT_LOG:-$LOG_DIR/tuber_report.log}"
mkdir -p "$LOG_DIR" "$REPORTS_DIR"
cd "$PROJECT" || { echo "ALERT Tuber report: нет каталога $PROJECT"; exit 0; }

# Раздел Telegram в сводке (ТЗ-A): код держит дефолт OFF, включение — этой
# строкой; откат — убрать/закомментировать (или TUBER_TG_SECTION=0).
export TUBER_TG_SECTION="${TUBER_TG_SECTION:-1}"

# Ключ DeepSeek нужен для живого перевода описаний X (ТЗ-E). Планировщик его в
# окружение не кладёт, поэтому грузим тем же приёмом load_key, что и
# scripts/x/tuber_x_classify.sh. Значение нигде НЕ печатаем (ни в stdout, ни в
# журнал). При отсутствии ключа перевода не будет: печатаем ALERT в stdout
# (молчание при недоступности перевода запрещено) и продолжаем работу.
ENV_FILE=/root/.hermes/.env
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
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  echo "ALERT Tuber report: нет DEEPSEEK_API_KEY — русские описания недоступны"
fi

ARGS=(report --compact --save --out "$REPORTS_DIR")
[ -n "${TUBER_DB:-}" ] && ARGS+=(--db "$TUBER_DB")
[ -n "${TUBER_REPORT_DAYS:-}" ] && ARGS+=(--days "$TUBER_REPORT_DAYS")

TS="$(date '+%F %T')"
ERR_TMP="$(mktemp)"
trap 'rm -f "$ERR_TMP"' EXIT

# stdout — сводка владельцу; stderr — отдельно, чтобы шум не ушёл в доставку.
out="$("$TUBER_PYTHON" -m tuber "${ARGS[@]}" 2>"$ERR_TMP")"
code=$?

{
  printf '%s\n' "$out"
  if [ -s "$ERR_TMP" ]; then
    echo "--- stderr ---"
    cat "$ERR_TMP"
  fi
} >> "$LOG"

# Полный отчёт целиком — в журнал (владельцу уходит только сводка в stdout).
FULL="$REPORTS_DIR/report-$(date -u +%F).txt"
if [ ! -f "$FULL" ]; then
  FULL="$(ls -t "$REPORTS_DIR"/report-*.txt 2>/dev/null | head -n 1)"
fi
if [ -n "$FULL" ] && [ -f "$FULL" ]; then
  {
    echo "--- полный отчёт: $FULL ---"
    cat "$FULL"
  } >> "$LOG"
fi

if [ "$code" -ne 0 ]; then
    printf '%s ALERT Tuber report: объединённая выдача не собрана (rc=%s), см. %s\n' "$TS" "$code" "$LOG"
    exit 0
fi

# Молчание недопустимо: успешный код, но пустая сводка — это тоже неуспех.
if [ -z "$out" ]; then
    printf '%s ALERT Tuber report: сводка пуста (rc=0), см. %s\n' "$TS" "$LOG"
    exit 0
fi

# Читаемая выдача (ТЗ-41): markdown-мастер и DOCX рядом с txt-отчётом. stdout
# обёртки (то, что уходит владельцу) этим шагом НЕ меняется: вывод в /dev/null,
# ошибки и служебная строка "docx: <путь>, N пунктов" — в лог; падение шага не
# влияет на код возврата и не печатает ALERT.
READABLE_MD="$REPORTS_DIR/report-$(date -u +%F).md"
READABLE_DOCX="$REPORTS_DIR/report-$(date -u +%F).docx"
DOCX_ARGS=(report --save --out "$REPORTS_DIR"
           --readable "$READABLE_MD" --docx "$READABLE_DOCX")
[ -n "${TUBER_DB:-}" ] && DOCX_ARGS+=(--db "$TUBER_DB")
[ -n "${TUBER_REPORT_DAYS:-}" ] && DOCX_ARGS+=(--days "$TUBER_REPORT_DAYS")
"$TUBER_PYTHON" -m tuber "${DOCX_ARGS[@]}" >/dev/null 2>>"$LOG"

# stdout: сводка (её и доставит планировщик владельцу).
printf '%s\n' "$out"
exit 0
