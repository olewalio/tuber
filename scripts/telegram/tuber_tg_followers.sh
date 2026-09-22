#!/usr/bin/env bash
# Tuber-Telegram: суточные снимки подписчиков с публичной превью-страницы (ТЗ-45).
#
# Зачем: `python3 -m tuber tg followers` умеет снять подписчиков с
# `https://t.me/<handle>` (без `/s/`), но в расписании этого задания не было —
# ряд роста не наполнялся сам. Прежняя заливка поставила `subs_at` константой
# (2026-09-14), роста подписчиков Telegram не существовало как данных.
#
# Что делает:
#   * обходит реестр `python3 -m tuber tg followers` (порядок в CLI: сначала
#     никогда не снятые, потом самые старые); потолок обхода за прогон —
#     `TUBER_TG_FOLLOWERS_LIMIT` (0/пусто = без потолка, CLI сам тормозит по
#     времени);
#   * пауза между запросами и потолок времени прогона — предохранители CLI
#     (`TUBER_TG_FOLLOWERS_PAUSE`, `TUBER_TG_FOLLOWERS_TIME_CAP_MIN`);
#   * при норме МОЛЧИТ (весь вывод — в журнал /root/.hermes/logs/tuber_tg_followers.log);
#   * при отказе/429 (или прогон не вернул сводку) печатает ОДНУ понятную
#     строку ALERT по-русски — её доставит планировщик;
#   * всегда завершается кодом 0: сбой прогона — забота сторожа, а не
#     уведомления крона (ТЗ-7);
#   * оборона от параллельного запуска (flock) и жёсткий `timeout` вокруг CLI.
#
# Переопределения (тесты/приёмка): TUBER_TG_PROJECT, TUBER_TG_LOG_DIR,
# TUBER_PYTHON, TUBER_DB (копия базы), TUBER_TG_FOLLOWERS_LIMIT,
# TUBER_TG_FOLLOWERS_TIME_CAP_MIN, TUBER_TG_FOLLOWERS_TIME_CAP_SEC (перебивает
# минуты — для тестов). TUBER_LAUNCHER_DRYRUN=1 — только напечатать план, в
# сеть не ходить.
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан, и
# «голый» python3 уходит на системный SQLite. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_TG_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_TG_LOG_DIR:-/root/.hermes/logs}"
LOG="$LOG_DIR/tuber_tg_followers.log"

# Потолок обхода за прогон (0/пусто = без потолка, CLI сам тормозит по времени).
LIMIT="${TUBER_TG_FOLLOWERS_LIMIT:-0}"
case "$LIMIT" in ''|*[!0-9]*) LIMIT=0 ;; esac

# Потолок времени прогона CLI (по умолчанию 60 мин), и жёсткий timeout вокруг
# него с запасом, чтобы зависшая сеть не держала задание бесконечно.
RUN_TIME_CAP_MIN="${TUBER_TG_FOLLOWERS_TIME_CAP_MIN:-60}"
RUN_TIME_CAP_SEC="${TUBER_TG_FOLLOWERS_TIME_CAP_SEC:-$((RUN_TIME_CAP_MIN * 60))}"
case "$RUN_TIME_CAP_SEC" in
  ''|*[!0-9]*) RUN_TIME_CAP_SEC=$((RUN_TIME_CAP_MIN * 60)) ;;
esac
HARD_TIMEOUT_SEC=$((RUN_TIME_CAP_SEC + 120))
export TUBER_TG_FOLLOWERS_TIME_CAP_SEC="$RUN_TIME_CAP_SEC"
export TUBER_TG_FOLLOWERS_PAUSE="${TUBER_TG_FOLLOWERS_PAUSE:-1.2}"

# Копия базы для приёмки (TUBER_DB) — пробрасываем как есть; флаг
# --allow-production копии не мешает (гейт срабатывает только на боевой базе).
# ВАЖНО: `tuber tg followers` ГЕЙТИТСЯ `scoring.assert_can_write` — без
# --allow-production боевой прогон молча отказывается писать (ноль снимков,
# rc=1). Обёртка и есть боевой планировщик, поэтому `cli_args` ВСЕГДА объявляет
# намерение писать в боевую. Гейт остаётся для РУЧНЫХ прогонов (приёмка на
# копии по-прежнему идёт без флага). См. TODO(debt-D-43).
if [ -n "${TUBER_DB:-}" ]; then
  export TUBER_DB
fi

cd "$PROJECT" || { printf 'ALERT: подписчики Telegram: нет каталога %s\n' "$PROJECT"; exit 0; }
mkdir -p "$LOG_DIR" || { printf 'ALERT: подписчики Telegram: нет каталога журналов %s\n' "$LOG_DIR"; exit 0; }

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Собрать аргументы CLI. --allow-production объявляем ВСЕГДА: обёртка — боевой
# планировщик, её назначение — писать в боевую базу (иначе `tuber tg followers`
# гейтится и отрабатывает вхолостую: ноль снимков, rc=1). Копии TUBER_DB флаг не
# мешает. Гейт остаётся только для ручных прогонов CLI.
cli_args() {
  printf -- '--allow-production'
  if [ "$LIMIT" -gt 0 ]; then
    printf -- ' --limit %s' "$LIMIT"
  fi
}

# Тестовый режим: показать план и выйти, сеть не трогать.
if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf '%s -m tuber tg followers %s\n' "$TUBER_PYTHON" "$(cli_args)"
  printf '=== %s followers DRYRUN: limit=%s потолок=%ss\n' \
    "$(stamp)" "$LIMIT" "$RUN_TIME_CAP_SEC" >>"$LOG"
  exit 0
fi

# Оборона от параллельного запуска: второй прогон тихо выходит.
LOCK="$LOG_DIR/tuber_tg_followers.lock"
exec 9>"$LOCK" || exit 0
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    printf '=== %s followers skipped: параллельный прогон уже идёт\n' "$(stamp)" >>"$LOG"
    exit 0
  fi
fi

args="$(cli_args)"
{
  printf '=== %s followers start: limit=%s потолок=%ss\n' \
    "$(stamp)" "$LIMIT" "$RUN_TIME_CAP_SEC"
} >>"$LOG"

# shellcheck disable=SC2086
out="$(timeout "$HARD_TIMEOUT_SEC" "$TUBER_PYTHON" -m tuber tg followers $args 2>>"$LOG")"; rc=$?
{
  printf '=== %s followers done rc=%s\n' "$(stamp)" "$rc"
  printf '%s\n' "$out"
} >>"$LOG"

json_num() { printf '%s\n' "$1" | sed -n "s/.*$2 \([0-9][0-9]*\).*/\1/p" | tail -n 1; }

fail="$(json_num "$out" 'отказов')"
ok="$(json_num "$out" 'обновлено')"
skip="$(json_num "$out" 'пропущено')"
fail="${fail:-0}"; ok="${ok:-0}"; skip="${skip:-0}"

# Прогон не вернул сводку (Traceback / обрыв / timeout) — это отказ.
if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
  printf 'ALERT: подписчики Telegram: прогон не завершился (код %s), см. %s\n' "$rc" "$LOG"
elif [ "$fail" -gt 0 ]; then
  printf 'ALERT: подписчики Telegram: отказов %s (403/429/сеть), обход остановлен, см. %s\n' \
    "$fail" "$LOG"
fi
# Норма (fail=0) — тишина: вывод уже в журнале.

exit 0
