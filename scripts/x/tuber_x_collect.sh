#!/usr/bin/env bash
# Tuber-x: сбор одного тира по расписанию (ТЗ-5 задача 3, ТЗ-7 задача 1).
#
# Использование: tuber_x_collect.sh A|B|C
#
# Тир можно не передавать аргументом: если имя самого файла оканчивается на
# _a / _b / _c (перед .sh), тир берётся из имени — tuber_x_collect_a.sh -> A.
# Это нужно потому, что планировщик Hermes запускает скрипты без аргументов
# командной строки (ТЗ-7): для каждого тира рядом лежит своя запускалка со
# своим суфиксом. Явный аргумент всегда имеет приоритет над именем файла.
#
# Если ни аргумента, ни распознанного имени нет — это ошибка (код возврата 2)
# с понятным сообщением, а не молчаливый сбор тира A.
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан, и
# «голый» python3 уходит на системный SQLite. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi


PROJECT="${TUBER_X_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_X_LOG_DIR:-/root/.hermes/logs}"
LOG="$LOG_DIR/tuber_x_collect.log"

# Определить тир: аргумент имеет приоритет, иначе — суфикс имени файла.
resolve_tier() {
  local arg="${1:-}" tier self
  if [ -n "$arg" ]; then
    tier="$(printf '%s' "$arg" | tr '[:lower:]' '[:upper:]')"
    case "$tier" in
      A|B|C) printf '%s' "$tier"; return 0 ;;
      *)
        echo "ошибка: неверный тир '$arg' (ожидается A, B или C)" >&2
        return 2
        ;;
    esac
  fi
  self="$(basename -- "$0")"
  self="${self%.sh}"
  case "$self" in
    *_a|*_A) printf '%s' A ;;
    *_b|*_B) printf '%s' B ;;
    *_c|*_C) printf '%s' C ;;
    *)
      echo "ошибка: тир не задан и не распознан из имени файла '$0'." >&2
      echo "Передайте тир аргументом ($(basename -- "$0") A|B|C) или назовите" >&2
      echo "файл-запускалку с суфиксом _a/_b/_c (например tuber_x_collect_a.sh)." >&2
      return 2
      ;;
  esac
}

TIER="$(resolve_tier "${1:-}")" || exit 2

# Квота одной сессии X (~1000 запросов/сутки) делится между тирами (ТЗ-13).
# min_interval не даёт прогонам ходить по одним и тем же аккаунтам каждый час,
# max_accounts не даёт одному прогону занять весь час.
case "$TIER" in
  A) DEF_INTERVAL=12; DEF_MAX=40 ;;
  B) DEF_INTERVAL=24; DEF_MAX=15 ;;
  C) DEF_INTERVAL=24; DEF_MAX=10 ;;
esac
MIN_INTERVAL="${TUBER_X_MIN_INTERVAL_H:-$DEF_INTERVAL}"
# Здесь именно '-': пустое (но установленное) значение — это явное «без потолка»,
# а не «взять дефолт»; дефолт подставляется только когда переменная не задана.
MAX_ACCOUNTS="${TUBER_X_MAX_ACCOUNTS-$DEF_MAX}"

# Собираем аргументы массивом: пустой EXTRA безопасен только для объявленного
# массива bash, поэтому объявляем его всегда.
EXTRA=(--min-interval-hours "$MIN_INTERVAL")
# TUBER_X_MAX_ACCOUNTS=0 или пусто = без потолка аккаунтов.
if [ -n "$MAX_ACCOUNTS" ] && [ "$MAX_ACCOUNTS" != "0" ]; then
  EXTRA+=(--max-accounts "$MAX_ACCOUNTS")
fi

# Холостая проверка аргументов: печатает итоговую команду и выходит, не трогая
# python и базу (ТЗ-13, проверка без живого прогона).
if [ "${TUBER_X_DRY_ARGS:-0}" = "1" ]; then
  printf '%s -m tuber x collect --tier %s' "$TUBER_PYTHON" "$TIER"
  for a in "${EXTRA[@]}"; do printf ' %s' "$a"; done
  printf '\n'
  exit 0
fi

cd "$PROJECT" || { echo "нет каталога $PROJECT" >>/dev/stderr; exit 3; }
mkdir -p "$LOG_DIR"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) collect tier=${TIER} interval=${MIN_INTERVAL}h max_accounts=${MAX_ACCOUNTS} ==="
  "$TUBER_PYTHON" -m tuber x collect --tier "$TIER" "${EXTRA[@]}"
} >>"$LOG" 2>&1
exit 0
