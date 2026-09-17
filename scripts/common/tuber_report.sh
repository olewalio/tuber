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

# stdout: сводка (её и доставит планировщик владельцу).
printf '%s\n' "$out"
exit 0
