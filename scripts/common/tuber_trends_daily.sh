#!/usr/bin/env bash
# Tuber: суточный контур 5 — подтемы сюжетов и новинки (ТЗ-48).
#
# Зачем: `python3 -m tuber trends inside` показывает, какая ПОДТЕМА внутри
# сюжета разгоняется сегодня (accel_sub ≥ 0,5 и ≥ 5 независимых авторов), а
# `python3 -m tuber trends novelties` ловит новинки снаружи (Hacker News,
# GitHub, Product Hunt, arXiv) и подтверждает их нашей базой.
#
# Что делает (раз в сутки):
#   * `trends inside`            — только чтение, печатает подтемы;
#   * `trends novelties --save`  — тянет внешний контур и кладёт новинки дня в
#                                  таблицу `novelty` (гейт записи боевой базы);
#   * при норме МОЛЧИТ (весь вывод — в /root/.hermes/logs/tuber_trends_daily.log);
#   * при отказе (rc != 0 / пустой вывод у любой из команд) печатает ОДНУ
#     строку ALERT по-русски;
#   * всегда завершается кодом 0: сбой прогона — забота сторожа (ТЗ-7);
#   * оборона от параллельного запуска (flock) и жёсткий `timeout` вокруг CLI.
#
# ВАЖНО (урок ТЗ-45F): подкоманда `novelties --save` ГЕЙТУЕТ запись в боевую
# базу через `--allow-production`. Обёртка — боевой планировщик, поэтому флаг
# объявляется ВСЕГДА (и в dry-run плане, и в реальном прогоне): иначе ночной
# прогон молча отказывает писать (rc=2, ноль строк). Тест-страж
# `tests/analysis/test_tz48_trends_wrapper.py` это проверяет.
#
# Переопределения (тесты/приёмка): TUBER_TRENDS_PROJECT,
# TUBER_TRENDS_LOG_DIR, TUBER_PYTHON, TUBER_DB (копия базы),
# TUBER_TRENDS_TIMEOUT_SEC (для тестов). TUBER_LAUNCHER_DRYRUN=1 — печатать план.
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан, и
# «голый» python3 уходит на системный SQLite. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_TRENDS_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_TRENDS_LOG_DIR:-/root/.hermes/logs}"
LOG="$LOG_DIR/tuber_trends_daily.log"

# Жёсткий timeout вокруг КАЖДОЙ команды (внешний контур медленный — запас).
HARD_TIMEOUT_SEC="${TUBER_TRENDS_TIMEOUT_SEC:-900}"
case "$HARD_TIMEOUT_SEC" in ''|*[!0-9]*) HARD_TIMEOUT_SEC=900 ;; esac

# Копия базы для приёмки (TUBER_DB) — пробрасываем как есть; флаг
# --allow-production копии не мешает (гейт срабатывает только на боевой базе).
if [ -n "${TUBER_DB:-}" ]; then
  export TUBER_DB
fi

cd "$PROJECT" || { printf 'ALERT: trends: нет каталога %s\n' "$PROJECT"; exit 0; }
mkdir -p "$LOG_DIR" || { printf 'ALERT: trends: нет каталога журналов %s\n' "$LOG_DIR"; exit 0; }

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Аргументы CLI. `novelties --save` — боевой планировщик, назначение которого
# писать новинки в боевую базу, поэтому --allow-production объявляется ВСЕГДА.
INSIDE_ARGS=""
NOVELTIES_ARGS="--save --allow-production"

if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf '%s -m tuber trends inside %s\n' "$TUBER_PYTHON" "$INSIDE_ARGS"
  printf '%s -m tuber trends novelties %s\n' "$TUBER_PYTHON" "$NOVELTIES_ARGS"
  printf '=== %s trends DRYRUN\n' "$(stamp)" >>"$LOG"
  exit 0
fi

# Оборона от параллельного запуска: второй прогон тихо выходит.
LOCK="$LOG_DIR/tuber_trends_daily.lock"
exec 9>"$LOCK" || exit 0
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    printf '=== %s trends skipped: параллельный прогон уже идёт\n' \
      "$(stamp)" >>"$LOG"
    exit 0
  fi
fi

printf '=== %s trends start\n' "$(stamp)" >>"$LOG"

failed=0
# shellcheck disable=SC2086
inside_out="$(timeout "$HARD_TIMEOUT_SEC" "$TUBER_PYTHON" -m tuber trends inside $INSIDE_ARGS 2>>"$LOG")"; rc=$?
{
  printf '=== %s trends inside rc=%s\n' "$(stamp)" "$rc"
  printf '%s\n' "$inside_out"
} >>"$LOG"
if [ "$rc" -ne 0 ] || [ -z "$inside_out" ]; then failed=1; fi

# shellcheck disable=SC2086
nov_out="$(timeout "$HARD_TIMEOUT_SEC" "$TUBER_PYTHON" -m tuber trends novelties $NOVELTIES_ARGS 2>>"$LOG")"; rc2=$?
{
  printf '=== %s trends novelties rc=%s\n' "$(stamp)" "$rc2"
  printf '%s\n' "$nov_out"
} >>"$LOG"
if [ "$rc2" -ne 0 ] || [ -z "$nov_out" ]; then failed=1; fi

if [ "$failed" -ne 0 ]; then
  printf 'ALERT: trends: прогон не завершился (inside rc=%s, novelties rc=%s), см. %s\n' \
    "$rc" "$rc2" "$LOG"
fi
# Норма — тишина: вывод уже в журнале.

exit 0
