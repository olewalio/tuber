#!/usr/bin/env bash
# Tuber: очередь замеров 1/6/24/72 ч для X и Telegram (ТЗ-43, контур 1 «Скорость»).
#
# Зачем: до этой волны у X и Telegram не было ни одной точки скорости — снимок
# метрик делался один раз (~через 30 минут после публикации), поэтому прирост
# просмотров считался у 0 постов. `python3 -m tuber metrics run` ставит стадии
# 1h/6h/24h/72h от `published_at`, добирает просроченные замеры, заполняет
# `bucket`/`delta_*` и печатает блок «Раннее».
#
# Что делает (каждый час):
#   * `metrics run` — планирование стадий + бэкфилл дельт + добор просроченных
#     (сеть: t.me/s для Telegram, CDN tweet-result для X) + блок «Раннее»;
#   * при норме МОЛЧИТ (весь вывод — в /root/.hermes/logs/tuber_metric_queue.log);
#   * при отказе (rc != 0 / пустой вывод) печатает ОДНУ строку ALERT по-русски;
#   * всегда завершается кодом 0: сбой прогона — забота сторожа (ТЗ-7);
#   * оборона от параллельного запуска (flock) и жёсткий `timeout` вокруг CLI.
#
# ВАЖНО (урок ТЗ-45F): подкоманда пишет в боевую базу и ГЕЙТУЕТ запись через
# `--allow-production`. Обёртка — боевой планировщик, поэтому флаг объявляется
# ВСЕГДА (и в dry-run плане, и в реальном прогоне): иначе прогон молча
# отказывает писать (rc=2, нулевые записи). Тест-страж
# `tests/analysis/test_tz43_metric_queue_wrapper.py` это проверяет.
#
# Переопределения (тесты/приёмка): TUBER_METRIC_QUEUE_PROJECT,
# TUBER_METRIC_QUEUE_LOG_DIR, TUBER_PYTHON, TUBER_DB (копия базы),
# TUBER_METRIC_QUEUE_LIMIT (потолок записей за прогон),
# TUBER_METRIC_QUEUE_TIMEOUT_SEC (для тестов), TUBER_METRIC_QUEUE_NO_NETWORK=1.
# TUBER_LAUNCHER_DRYRUN=1 — печатать план.
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан, и
# «голый» python3 уходит на системный SQLite. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_METRIC_QUEUE_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_METRIC_QUEUE_LOG_DIR:-/root/.hermes/logs}"
LOG="$LOG_DIR/tuber_metric_queue.log"

# Потолок записей за прогон (0/пусто = без потолка, CLI сам ограничивает).
LIMIT="${TUBER_METRIC_QUEUE_LIMIT:-0}"
case "$LIMIT" in ''|*[!0-9]*) LIMIT=0 ;; esac

# Жёсткий timeout вокруг CLI (по умолчанию 20 мин: сеть мерцает).
HARD_TIMEOUT_SEC="${TUBER_METRIC_QUEUE_TIMEOUT_SEC:-1200}"
case "$HARD_TIMEOUT_SEC" in ''|*[!0-9]*) HARD_TIMEOUT_SEC=1200 ;; esac

# Копия базы для приёмки (TUBER_DB) — пробрасываем как есть; флаг
# --allow-production копии не мешает (гейт срабатывает только на боевой базе).
if [ -n "${TUBER_DB:-}" ]; then
  export TUBER_DB
fi

cd "$PROJECT" || { printf 'ALERT: очередь замеров: нет каталога %s\n' "$PROJECT"; exit 0; }
mkdir -p "$LOG_DIR" || { printf 'ALERT: очередь замеров: нет каталога журналов %s\n' "$LOG_DIR"; exit 0; }

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Аргументы CLI. --allow-production объявляем ВСЕГДА: обёртка — боевой
# планировщик, её назначение — писать замеры в боевую базу.
cli_args() {
  printf -- '--allow-production'
  if [ "$LIMIT" -gt 0 ]; then
    printf -- ' --limit %s' "$LIMIT"
  fi
  if [ "${TUBER_METRIC_QUEUE_NO_NETWORK:-0}" = "1" ]; then
    printf -- ' --no-network'
  fi
}

if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf '%s -m tuber metrics run %s\n' "$TUBER_PYTHON" "$(cli_args)"
  printf '=== %s metrics DRYRUN: limit=%s\n' "$(stamp)" "$LIMIT" >>"$LOG"
  exit 0
fi

# Оборона от параллельного запуска: второй прогон тихо выходит.
LOCK="$LOG_DIR/tuber_metric_queue.lock"
exec 9>"$LOCK" || exit 0
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    printf '=== %s metrics skipped: параллельный прогон уже идёт\n' \
      "$(stamp)" >>"$LOG"
    exit 0
  fi
fi

args="$(cli_args)"
printf '=== %s metrics start: limit=%s\n' "$(stamp)" "$LIMIT" >>"$LOG"

# shellcheck disable=SC2086
out="$(timeout "$HARD_TIMEOUT_SEC" "$TUBER_PYTHON" -m tuber metrics run $args 2>>"$LOG")"; rc=$?
{
  printf '=== %s metrics done rc=%s\n' "$(stamp)" "$rc"
  printf '%s\n' "$out"
} >>"$LOG"

if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
  printf 'ALERT: очередь замеров: прогон не завершился (код %s), см. %s\n' \
    "$rc" "$LOG"
fi
# Норма — тишина: вывод уже в журнале.

exit 0
