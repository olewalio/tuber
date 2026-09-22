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

cd "$PROJECT" || { echo "нет каталога $PROJECT" >>/dev/stderr; exit 3; }
mkdir -p "$LOG_DIR"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) collect tier=${TIER} ==="
  "$TUBER_PYTHON" -m tuber x collect --tier "$TIER"
} >>"$LOG" 2>&1
exit 0
