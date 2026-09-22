#!/usr/bin/env bash
# Tuber: суточный сбор комментариев и обсуждений (ТЗ-49, контур 6).
#
# Зачем: комментарии собирались только у YouTube (3 163 строки) и не работали на
# дискавери; у Telegram текстов ответов не было вовсе. Задание раз в сутки:
#   * `comments youtube`  — commentThreads по топ-50 виральным видео и топ-3
#     каналам (пагинация), харвест комментаторов likes>=50 в очередь кандидатов;
#   * `comments telegram` — тексты ответов через Telethon (топ-30 каналов,
#     предохранители: пауза, потолок за прогон, честная остановка при flood-wait);
#   * `comments x`        — только сигнал «горячий спор» по conversation_count
#     (текст reply-цепочек недоступен без платного API — не имитируем).
#
# Что делает:
#   * гоняет подкоманды по единой базе; весь вывод — в журнал;
#   * при норме МОЛЧИТ; при отказе печатает ОДНУ строку ALERT по-русски;
#   * всегда завершается кодом 0: сбой прогона — забота сторожа (ТЗ-7);
#   * оборона от параллельного запуска (flock) и жёсткий `timeout` вокруг CLI.
#
# ВАЖНО (урок ТЗ-45F): подкоманды ГЕЙТУЮТ запись в боевую базу через
# `--allow-production`. Обёртка — боевой планировщик, поэтому флаг объявляется
# ВСЕГДА (и в dry-run плане, и в реальном прогоне). Тест-страж
# `tests/analysis/test_tz49_comments_wrapper.py` это проверяет.
#
# Переопределения (тесты/приёмка): TUBER_COMMENTS_PROJECT, TUBER_COMMENTS_LOG_DIR,
# TUBER_PYTHON, TUBER_DB (копия базы), TUBER_COMMENTS_YT_LIMIT,
# TUBER_COMMENTS_TG_CHANNELS, TUBER_COMMENTS_TG_MAX_REPLIES,
# TUBER_COMMENTS_TIMEOUT_SEC (для тестов). TUBER_LAUNCHER_DRYRUN=1 — печатать план.
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): Telethon живёт только в
# venv контура. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24, Telethon)" >&2
  exit 4
fi

PROJECT="${TUBER_COMMENTS_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_COMMENTS_LOG_DIR:-/root/.hermes/logs}"
LOG="$LOG_DIR/tuber_comments_daily.log"

# Потолки/лимиты (0/пусто = дефолты CLI).
YT_LIMIT="${TUBER_COMMENTS_YT_LIMIT:-0}"
TG_CHANNELS="${TUBER_COMMENTS_TG_CHANNELS:-0}"
TG_MAX_REPLIES="${TUBER_COMMENTS_TG_MAX_REPLIES:-0}"
for v in YT_LIMIT TG_CHANNELS TG_MAX_REPLIES; do
  eval "val=\$$v"; case "$val" in ''|*[!0-9]*) eval "$v=0" ;; esac
done

# Жёсткий timeout вокруг каждого CLI-шага (по умолчанию 45 мин).
HARD_TIMEOUT_SEC="${TUBER_COMMENTS_TIMEOUT_SEC:-2700}"
case "$HARD_TIMEOUT_SEC" in ''|*[!0-9]*) HARD_TIMEOUT_SEC=2700 ;; esac

# Копия базы для приёмки (TUBER_DB) — пробрасываем как есть; флаг
# --allow-production копии не мешает (гейт срабатывает только на боевой базе).
if [ -n "${TUBER_DB:-}" ]; then
  export TUBER_DB
fi

cd "$PROJECT" || { printf 'ALERT: комментарии: нет каталога %s\n' "$PROJECT"; exit 0; }
mkdir -p "$LOG_DIR" || { printf 'ALERT: комментарии: нет каталога журналов %s\n' "$LOG_DIR"; exit 0; }

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Аргументы общей части: --allow-production объявляем ВСЕГДА (обёртка — боевой
# планировщик). Дополнительные потолки добавляются подкомандам.
common_args() { printf -- '--allow-production'; }

yt_args() {
  printf -- '%s' "$(common_args)"
  [ "$YT_LIMIT" -gt 0 ] && printf -- ' --limit %s' "$YT_LIMIT"
}
tg_args() {
  printf -- '%s' "$(common_args)"
  [ "$TG_CHANNELS" -gt 0 ] && printf -- ' --channels %s' "$TG_CHANNELS"
  [ "$TG_MAX_REPLIES" -gt 0 ] && printf -- ' --max-replies %s' "$TG_MAX_REPLIES"
}

# Тестовый режим: показать план и выйти, сеть/базу не трогать.
if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf '%s -m tuber comments youtube %s\n' "$TUBER_PYTHON" "$(yt_args)"
  printf '%s -m tuber comments telegram %s\n' "$TUBER_PYTHON" "$(tg_args)"
  printf '%s -m tuber comments x %s\n' "$TUBER_PYTHON" "$(common_args)"
  printf '=== %s comments DRYRUN: yt_limit=%s tg_channels=%s max_replies=%s\n' \
    "$(stamp)" "$YT_LIMIT" "$TG_CHANNELS" "$TG_MAX_REPLIES" >>"$LOG"
  exit 0
fi

# Оборона от параллельного запуска: второй прогон тихо выходит.
LOCK="$LOG_DIR/tuber_comments_daily.lock"
exec 9>"$LOCK" || exit 0
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    printf '=== %s comments skipped: параллельный прогон уже идёт\n' \
      "$(stamp)" >>"$LOG"
    exit 0
  fi
fi

printf '=== %s comments start\n' "$(stamp)" >>"$LOG"

rc_total=0
run_step() {
  # run_step <подпись> <подкоманда> <args>
  local label="$1" sub="$2" args="$3"
  # shellcheck disable=SC2086
  local out
  out="$(timeout "$HARD_TIMEOUT_SEC" "$TUBER_PYTHON" -m tuber comments "$sub" $args 2>>"$LOG")"
  local rc=$?
  {
    printf '=== %s comments %s done rc=%s\n' "$(stamp)" "$sub" "$rc"
    printf '%s\n' "$out"
  } >>"$LOG"
  if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
    LAST_FAIL="$label (код $rc)"
    rc_total=1
  fi
  LAST_OUT="$out"
}

LAST_FAIL=""
LAST_OUT=""
run_step "youtube" youtube "$(yt_args)"
run_step "telegram" telegram "$(tg_args)"
run_step "x" x "$(common_args)"

if [ "$rc_total" -ne 0 ]; then
  printf 'ALERT: комментарии и обсуждения: шаг %s не завершился, см. %s\n' \
    "$LAST_FAIL" "$LOG"
fi
# Норма — тишина: вывод уже в журнале.

exit 0
