#!/usr/bin/env bash
# Tuber: суточный разбор очереди кандидатов и миля повышения (ТЗ-46, ТЗ-51).
#
# Зачем: очередь новых аккаунтов (`candidate`) не разбиралась — 17 770 записей в
# статусе `new` ждали вердикта, новые авторы не попадали в активный сбор. Задание
# раз в сутки ПОСЛЕ сбора постов:
#   1) `python3 -m tuber queue review` — КАЖДОМУ кандидату вердикт с причиной
#      (`promote`/`hold`/`reject`) по эвристикам и бесплатным страницам;
#   2) `python3 -m tuber tg promote` — по измерениям (посты, антифрод, `vr`,
#      посчитанный по фактическим постам) поднять канал `candidate → active` и
#      закрыть строку очереди (`promoted_at`), связав её с реестром по
#      каноническому хендлу/`tg_id` (ТЗ-51, долги D-54/D-55).
#
# Что делает:
#   * гоняет обе подкоманды по единой базе; весь вывод — в журнал;
#   * при норме МОЛЧИТ (весь вывод — в /root/.hermes/logs/tuber_queue_review.log);
#   * при отказе (rc != 0 / пустой вывод) печатает ОДНУ строку ALERT по-русски
#     и завершается НЕНУЛЕВЫМ кодом (ТЗ-51: сбой обязан быть виден крону);
#   * оборона от параллельного запуска (flock) и жёсткий `timeout` вокруг CLI.
#
# ВАЖНО (урок ТЗ-45F): все подкоманды ГЕЙТУЮТ запись в боевую базу через
# `--allow-production`. Обёртка — боевой планировщик, поэтому флаг объявляется
# ВСЕГДА и доходит до КАЖДОЙ подкоманды (и review, и promote): иначе ночной
# прогон молча отказывает писать (rc=2, ноль вердиктов/повышений). Тест-страж
# `tests/analysis/test_tz46_queue_wrapper.py` это проверяет.
#
# Переопределения (тесты/приёмка): TUBER_QUEUE_PROJECT, TUBER_QUEUE_LOG_DIR,
# TUBER_PYTHON, TUBER_DB (копия базы), TUBER_QUEUE_X_BUDGET,
# TUBER_QUEUE_TIMEOUT_SEC (для тестов). TUBER_LAUNCHER_DRYRUN=1 — печатать план.
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан, и
# «голый» python3 уходит на системный SQLite. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_QUEUE_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_QUEUE_LOG_DIR:-/root/.hermes/logs}"
LOG="$LOG_DIR/tuber_queue_review.log"

# Потолок проверки X-аккаунтов сетью за прогон (0/пусто = всех, кого осилит
# бюджет сессии X; обычно бюджет исчерпывается за ~400 аккаунтов/сутки).
X_BUDGET="${TUBER_QUEUE_X_BUDGET:-0}"
case "$X_BUDGET" in ''|*[!0-9]*) X_BUDGET=0 ;; esac

# Жёсткий timeout вокруг CLI (по умолчанию 60 мин; сеть, но потолок времени CLI).
HARD_TIMEOUT_SEC="${TUBER_QUEUE_TIMEOUT_SEC:-3600}"
case "$HARD_TIMEOUT_SEC" in ''|*[!0-9]*) HARD_TIMEOUT_SEC=3600 ;; esac

# Копия базы для приёмки (TUBER_DB) — пробрасываем как есть; флаг
# --allow-production копии не мешает (гейт срабатывает только на боевой базе).
if [ -n "${TUBER_DB:-}" ]; then
  export TUBER_DB
fi

cd "$PROJECT" || { printf 'ALERT: разбор очереди: нет каталога %s\n' "$PROJECT"; exit 1; }
mkdir -p "$LOG_DIR" || { printf 'ALERT: разбор очереди: нет каталога журналов %s\n' "$LOG_DIR"; exit 1; }

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Аргументы CLI. --allow-production объявляем ВСЕГДА: обёртка — боевой
# планировщик, её назначение — писать вердикты и повышения в боевую базу.
# Копии TUBER_DB флаг не мешает.
common_args() { printf -- '--allow-production'; }

review_args() {
  printf -- '%s' "$(common_args)"
  if [ "$X_BUDGET" -gt 0 ]; then
    printf -- ' --x-budget %s' "$X_BUDGET"
  fi
}

promote_args() { printf -- '%s' "$(common_args)"; }

# Тестовый режим: показать план и выйти, базу не трогать.
if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf '%s -m tuber queue review %s\n' "$TUBER_PYTHON" "$(review_args)"
  printf '%s -m tuber tg promote %s\n' "$TUBER_PYTHON" "$(promote_args)"
  printf '=== %s queue review DRYRUN: x_budget=%s\n' "$(stamp)" "$X_BUDGET" >>"$LOG"
  exit 0
fi

# Оборона от параллельного запуска: второй прогон тихо выходит.
LOCK="$LOG_DIR/tuber_queue_review.lock"
exec 9>"$LOCK" || exit 0
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    printf '=== %s queue review skipped: параллельный прогон уже идёт\n' \
      "$(stamp)" >>"$LOG"
    exit 0
  fi
fi

printf '=== %s queue review start\n' "$(stamp)" >>"$LOG"

rc_total=0
LAST_FAIL=""

# run_step <подпись> <аргументы CLI> <всё после `python3 -m tuber`>
run_step() {
  local label="$1" args="$2" sub="$3"
  local out
  # shellcheck disable=SC2086
  out="$(timeout "$HARD_TIMEOUT_SEC" "$TUBER_PYTHON" -m tuber $sub $args 2>>"$LOG")"
  local rc=$?
  {
    printf '=== %s %s done rc=%s\n' "$(stamp)" "$label" "$rc"
    printf '%s\n' "$out"
  } >>"$LOG"
  if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
    LAST_FAIL="$label (код $rc)"
    rc_total=1
  fi
}

run_step "queue review" "$(review_args)" "queue review"
run_step "tg promote" "$(promote_args)" "tg promote"

if [ "$rc_total" -ne 0 ]; then
  printf 'ALERT: разбор очереди кандидатов: шаг %s не завершился, см. %s\n' \
    "$LAST_FAIL" "$LOG"
  exit 1
fi
# Норма — тишина: вывод уже в журнале.

exit 0
