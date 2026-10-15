#!/usr/bin/env bash
# Tuber: суточный граф первопроходцев (ТЗ-47, контур 4 «Сливки»).
#
# Зачем: `python3 -m tuber graph first-movers` считает по уже собранным рёбрам
# `edge` и сюжетам `story_member` три оси — `trusted_indegree_30d` (сколько разных
# крупных авторов сослались), `lead_time_median` (медианный лаг автора внутри
# сюжетов) и `first_mover`/`first_mover_share` (кто первым написал). В расписании
# этого задания не было, поэтому оси не наполнялись сами. Сеть не нужна: только
# база.
#
# Что делает:
#   * гоняет `python3 -m tuber graph first-movers` по единой базе (ночью, после
#     сбора и пересборки сюжетов);
#   * потолок разбора за прогон — `TUBER_GRAPH_FM_LIMIT` (0/пусто = весь корпус,
#     CLI отрабатывает за секунды);
#   * при норме МОЛЧИТ (весь вывод — в /root/.hermes/logs/tuber_graph_first_movers.log);
#   * при отказе (rc != 0 / пустой вывод) печатает ОДНУ строку ALERT по-русски;
#   * всегда завершается кодом 0: сбой прогона — забота сторожа (ТЗ-7);
#   * оборона от параллельного запуска (flock) и жёсткий `timeout` вокруг CLI.
#
# ВАЖНО (урок ТЗ-45F): подкоманда ГЕЙТУЕТ запись в боевую базу через
# `--allow-production`. Обёртка — боевой планировщик, поэтому флаг объявляется
# ВСЕГДА (и в dry-run плане, и в реальном прогоне): иначе ночной прогон молча
# отказывает писать (rc=2, нулевые записи). Тест-страж
# `tests/graph/test_tz47_first_movers_wrapper.py` это проверяет.
#
# Переопределения (тесты/приёмка): TUBER_GRAPH_FM_PROJECT, TUBER_GRAPH_FM_LOG_DIR,
# TUBER_PYTHON, TUBER_DB (копия базы), TUBER_GRAPH_FM_LIMIT,
# TUBER_GRAPH_FM_TIMEOUT_SEC (для тестов). TUBER_LAUNCHER_DRYRUN=1 — печатать план.
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан, и
# «голый» python3 уходит на системный SQLite. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_GRAPH_FM_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_GRAPH_FM_LOG_DIR:-/root/.hermes/logs}"
LOG="$LOG_DIR/tuber_graph_first_movers.log"

# Потолок разбора за прогон (0/пусто = весь корпус).
LIMIT="${TUBER_GRAPH_FM_LIMIT:-0}"
case "$LIMIT" in ''|*[!0-9]*) LIMIT=0 ;; esac

# Жёсткий timeout вокруг CLI (по умолчанию 15 мин; сеть не нужна, запас на диск).
HARD_TIMEOUT_SEC="${TUBER_GRAPH_FM_TIMEOUT_SEC:-900}"
case "$HARD_TIMEOUT_SEC" in ''|*[!0-9]*) HARD_TIMEOUT_SEC=900 ;; esac

# Копия базы для приёмки (TUBER_DB) — пробрасываем как есть; флаг
# --allow-production копии не мешает (гейт срабатывает только на боевой базе).
if [ -n "${TUBER_DB:-}" ]; then
  export TUBER_DB
fi

cd "$PROJECT" || { printf 'ALERT: граф первопроходцев: нет каталога %s\n' "$PROJECT"; exit 0; }
mkdir -p "$LOG_DIR" || { printf 'ALERT: граф первопроходцев: нет каталога журналов %s\n' "$LOG_DIR"; exit 0; }

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Аргументы CLI. --allow-production объявляем ВСЕГДА: обёртка — боевой
# планировщик, её назначение — писать оси в боевую базу (иначе гейт глушит
# запись). Копии TUBER_DB флаг не мешает.
cli_args() {
  printf -- '--allow-production'
  if [ "$LIMIT" -gt 0 ]; then
    printf -- ' --limit %s' "$LIMIT"
  fi
}

# Тестовый режим: показать план и выйти, базу не трогать.
if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf '%s -m tuber graph first-movers %s\n' "$TUBER_PYTHON" "$(cli_args)"
  printf '=== %s first-movers DRYRUN: limit=%s\n' "$(stamp)" "$LIMIT" >>"$LOG"
  exit 0
fi

# Оборона от параллельного запуска: второй прогон тихо выходит.
LOCK="$LOG_DIR/tuber_graph_first_movers.lock"
exec 9>"$LOCK" || exit 0
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    printf '=== %s first-movers skipped: параллельный прогон уже идёт\n' \
      "$(stamp)" >>"$LOG"
    exit 0
  fi
fi

args="$(cli_args)"
printf '=== %s first-movers start: limit=%s\n' "$(stamp)" "$LIMIT" >>"$LOG"

# shellcheck disable=SC2086
out="$(timeout "$HARD_TIMEOUT_SEC" "$TUBER_PYTHON" -m tuber graph first-movers $args 2>>"$LOG")"; rc=$?
{
  printf '=== %s first-movers done rc=%s\n' "$(stamp)" "$rc"
  printf '%s\n' "$out"
} >>"$LOG"

if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
  printf 'ALERT: граф первопроходцев: прогон не завершился (код %s), см. %s\n' \
    "$rc" "$LOG"
fi
# Норма — тишина: вывод уже в журнале.

exit 0
