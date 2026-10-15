#!/usr/bin/env bash
# Tuber: суточный рейтинг «сливки» (ТЗ-45, контуры 2-3).
#
# Зачем: `python3 -m tuber rating slivki capture` снимает суточный снимок метрик
# источника (`source_metric_history`) — подписчики, посты за 7 дней, медиана
# метрики за 24 ч, виральные посты за 14 дней, trusted_indegree и lead_time из
# графа первопроходцев (ТЗ-47). Без ряда снимков `g7` (рост за 7 дней) не из
# чего считать, и «сливки» честно показывают «история N дн из 7». Сеть не нужна:
# только база.
#
# Что делает:
#   * гоняет `python3 -m tuber rating slivki capture` по единой базе (ночью,
#     ПОСЛЕ сбора постов, пересчёта метрик и графа первопроходцев);
#   * при норме МОЛЧИТ (весь вывод — в /root/.hermes/logs/tuber_rating_slivki.log);
#   * при отказе (rc != 0 / пустой вывод) печатает ОДНУ строку ALERT по-русски;
#   * всегда завершается кодом 0: сбой прогона — забота сторожа (ТЗ-7);
#   * оборона от параллельного запуска (flock) и жёсткий `timeout` вокруг CLI.
#
# ВАЖНО (урок ТЗ-45F): подкоманда `capture` ГЕЙТУЕТ запись в боевую базу через
# `--allow-production`. Обёртка — боевой планировщик, поэтому флаг объявляется
# ВСЕГДА (и в dry-run плане, и в реальном прогоне): иначе ночной прогон молча
# отказывает писать (rc=2, нулевые записи). Тест-страж
# `tests/analysis/test_tz45_slivki_wrapper.py` это проверяет.
#
# Переопределения (тесты/приёмка): TUBER_RATING_SLIVKI_PROJECT,
# TUBER_RATING_SLIVKI_LOG_DIR, TUBER_PYTHON, TUBER_DB (копия базы),
# TUBER_RATING_SLIVKI_TIMEOUT_SEC (для тестов). TUBER_LAUNCHER_DRYRUN=1 —
# печатать план.
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан, и
# «голый» python3 уходит на системный SQLite. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_RATING_SLIVKI_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_RATING_SLIVKI_LOG_DIR:-/root/.hermes/logs}"
LOG="$LOG_DIR/tuber_rating_slivki.log"

# Жёсткий timeout вокруг CLI (по умолчанию 15 мин; сеть не нужна, запас на диск).
HARD_TIMEOUT_SEC="${TUBER_RATING_SLIVKI_TIMEOUT_SEC:-900}"
case "$HARD_TIMEOUT_SEC" in ''|*[!0-9]*) HARD_TIMEOUT_SEC=900 ;; esac

# Копия базы для приёмки (TUBER_DB) — пробрасываем как есть; флаг
# --allow-production копии не мешает (гейт срабатывает только на боевой базе).
if [ -n "${TUBER_DB:-}" ]; then
  export TUBER_DB
fi

cd "$PROJECT" || { printf 'ALERT: рейтинг сливки: нет каталога %s\n' "$PROJECT"; exit 0; }
mkdir -p "$LOG_DIR" || { printf 'ALERT: рейтинг сливки: нет каталога журналов %s\n' "$LOG_DIR"; exit 0; }

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Аргументы CLI. --allow-production объявляем ВСЕГДА: обёртка — боевой
# планировщик, её назначение — писать снимок в боевую базу (иначе гейт глушит
# запись). Копии TUBER_DB флаг не мешает.
cli_args() {
  printf -- '--allow-production'
}

# Тестовый режим: показать план и выйти, базу не трогать.
if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf '%s -m tuber rating slivki capture %s\n' "$TUBER_PYTHON" "$(cli_args)"
  printf '=== %s slivki DRYRUN\n' "$(stamp)" >>"$LOG"
  exit 0
fi

# Оборона от параллельного запуска: второй прогон тихо выходит.
LOCK="$LOG_DIR/tuber_rating_slivki.lock"
exec 9>"$LOCK" || exit 0
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    printf '=== %s slivki skipped: параллельный прогон уже идёт\n' \
      "$(stamp)" >>"$LOG"
    exit 0
  fi
fi

args="$(cli_args)"
printf '=== %s slivki start\n' "$(stamp)" >>"$LOG"

# shellcheck disable=SC2086
out="$(timeout "$HARD_TIMEOUT_SEC" "$TUBER_PYTHON" -m tuber rating slivki capture $args 2>>"$LOG")"; rc=$?
{
  printf '=== %s slivki done rc=%s\n' "$(stamp)" "$rc"
  printf '%s\n' "$out"
} >>"$LOG"

if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
  printf 'ALERT: рейтинг сливки: прогон не завершился (код %s), см. %s\n' \
    "$rc" "$LOG"
fi
# Норма — тишина: вывод уже в журнале.

exit 0
