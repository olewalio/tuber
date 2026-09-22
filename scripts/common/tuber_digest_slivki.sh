#!/usr/bin/env bash
# Tuber: утренняя выдача «Сливки» одной командой + доставка в Telegram (ТЗ-50).
#
# Зачем: владельцу нужна ОДНА утренняя выдача, а не пять команд. Обёртка зовёт
# `python3 -m tuber digest slivki --send`, который:
#   * собирает 10 авторов + 5 подтем + 5 новинок + 3 обсуждения и перцентиль
#     «где я» (ТЗ-45/48/49 + §4 плана);
#   * проверяет ссылки HTTP (каждая позиция — с открывающейся ссылкой);
#   * отправляет текст ПРЯМЫМ Telegram Bot API в явные chat_id/thread_id
#     (топик не угадывается: нет TUBER_DIGEST_THREAD_ID — параметр не передаётся);
#   * пишет в журнал строку `delivered chat_id=… thread_id=… parts=… message_id=…`
#     (ТЗ-50 п.4: факт доставки, а не «отправлено» на слово).
#
# Поведение:
#   * при норме МОЛЧИТ (весь вывод — в /root/.hermes/logs/tuber_digest_slivki.log,
#     факт доставки — в журнале JOURNAL; stdout пуст → планировщик ничего не шлёт);
#   * «пустой день» (нет данных или все ссылки отброшены) — тоже молчит: пустой
#     отчёт владельцу не отправляем;
#   * при отказе (rc != 0) печатает ОДНУ строку `ALERT:` по-русски;
#   * всегда завершается кодом 0: сбой прогона — забота сторожа (ТЗ-7);
#   * оборона от параллельного запуска (flock) и жёсткий `timeout` вокруг CLI.
#
# ВАЖНО (урок ТЗ-45F): подкоманда `--send` ГЕЙТУЕТ запись/отправку по боевой базе
# через `--allow-production`. Обёртка — боевой планировщик, поэтому флаг
# объявляется ВСЕГДА (и в dry-run плане, и в реальном прогоне): иначе утренний
# прогон молча откажет (rc=2), и владелец ничего не получит. Тест-страж
# `tests/analysis/test_tz50_digest_wrapper.py` это проверяет.
#
# Переопределения (тесты/приёмка): TUBER_DIGEST_PROJECT, TUBER_DIGEST_LOG_DIR,
# TUBER_PYTHON, TUBER_DB (копия базы), TUBER_DIGEST_TIMEOUT_SEC, TUBER_DIGEST_CHAT_ID,
# TUBER_DIGEST_THREAD_ID, TUBER_SLIVKI_ME, TUBER_SLIVKI_REACH, TUBER_SLIVKI_REACTIONS,
# TUBER_SLIVKI_G7. TUBER_LAUNCHER_DRYRUN=1 — печатать план.
#
# Личные адреса доставки (ТЗ-54): в самом скрипте личных значений НЕТ — репозиторий
# публикуется на GitHub. Чат владельца и субъект «где я» читаются из файла окружения
# TUBER_OWNER_ENV (по умолчанию /root/.hermes/tuber_owner.env, вне репозитория);
# формат файла — строки `export TUBER_DIGEST_CHAT_ID=…` и `export TUBER_SLIVKI_ME=…`.
# Нет файла или переменных — CLI честно откажет (нет chat_id) либо покажет «субъект
# не задан»; ничего личного в код не вшивается.
#
# Владелец (D-65): субъект «где я» берётся из окружения. Ниже — строка для
# владельца, закомментированная: подставить handle владельца или его числа
# (значений НЕ выдумываем — иначе перцентиль соврёт). Для источника из реестра
# проценты считаются по базе; числа можно задать напрямую.
#   TUBER_SLIVKI_ME=platform:handle  # подставить handle владельца или его числа
#   TUBER_SLIVKI_REACH=… TUBER_SLIVKI_REACTIONS=… TUBER_SLIVKI_G7=…
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан, и
# «голый» python3 уходит на системный SQLite. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_DIGEST_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_DIGEST_LOG_DIR:-/root/.hermes/logs}"
LOG="$LOG_DIR/tuber_digest_slivki.log"

# Личные адреса доставки — ВНЕ репозитория (ТЗ-54): репозиторий публикуется,
# поэтому личный chat_id владельца и субъект «где я» читаются из файла окружения.
OWNER_ENV="${TUBER_OWNER_ENV:-/root/.hermes/tuber_owner.env}"
if [ -r "$OWNER_ENV" ]; then
  # shellcheck disable=SC1090
  . "$OWNER_ENV"
fi

# Жёсткий timeout вокруг CLI (новинки ходят в сеть + проверка ссылок; запас).
HARD_TIMEOUT_SEC="${TUBER_DIGEST_TIMEOUT_SEC:-900}"
case "$HARD_TIMEOUT_SEC" in ''|*[!0-9]*) HARD_TIMEOUT_SEC=900 ;; esac

# Копия базы для приёмки (TUBER_DB) — пробрасываем как есть; флаг
# --allow-production копии не мешает (гейт срабатывает только на боевой базе).
if [ -n "${TUBER_DB:-}" ]; then
  export TUBER_DB
fi

# Точный текст отправки — в файл рядом с журналом (аудит: «тот самый текст»).
export TUBER_DIGEST_MESSAGE_FILE="${TUBER_DIGEST_MESSAGE_FILE:-$LOG_DIR/tuber_digest_slivki.last.txt}"

cd "$PROJECT" || { printf 'ALERT: digest slivki: нет каталога %s\n' "$PROJECT"; exit 0; }
mkdir -p "$LOG_DIR" || { printf 'ALERT: digest slivki: нет каталога журналов %s\n' "$LOG_DIR"; exit 0; }

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Аргументы CLI. --send и --allow-production объявляем ВСЕГДА: назначение
# обёртки — доставить выдачу владельцу из боевой базы. Явные chat_id/thread_id
# берём из окружения (в т.ч. из прочитанного файла владельца); не заданы — CLI
# честно откажет (нет chat_id), а топик НЕ угадывает (не передаётся).
cli_args() {
  printf -- '--send --allow-production'
  if [ -n "${TUBER_DB:-}" ]; then
    printf -- ' --db %s' "$TUBER_DB"
  fi
  if [ -n "${TUBER_DIGEST_CHAT_ID:-}" ]; then
    printf -- ' --chat-id %s' "$TUBER_DIGEST_CHAT_ID"
  fi
  if [ -n "${TUBER_DIGEST_THREAD_ID:-}" ]; then
    printf -- ' --thread-id %s' "$TUBER_DIGEST_THREAD_ID"
  fi
  # Субъект «где я» (D-65): читаем из окружения ЯВНО, чтобы план и прогон были
  # одинаковы. `TUBER_SLIVKI_ME=platform:handle` — проценты из базы по реестру;
  # `TUBER_SLIVKI_REACH/REACTIONS/G7` — числа напрямую. Не заданы — CLI честно
  # скажет «НЕ найден в базе» и покажет типичные значения базы.
  if [ -n "${TUBER_SLIVKI_ME:-}" ]; then
    printf -- ' --me %s' "$TUBER_SLIVKI_ME"
  fi
  if [ -n "${TUBER_SLIVKI_REACH:-}" ]; then
    printf -- ' --me-reach %s' "$TUBER_SLIVKI_REACH"
  fi
  if [ -n "${TUBER_SLIVKI_REACTIONS:-}" ]; then
    printf -- ' --me-reactions %s' "$TUBER_SLIVKI_REACTIONS"
  fi
  if [ -n "${TUBER_SLIVKI_G7:-}" ]; then
    printf -- ' --me-g7 %s' "$TUBER_SLIVKI_G7"
  fi
}

# Тестовый режим: показать план и выйти, базу не трогать, не отправлять.
if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf '%s -m tuber digest slivki %s\n' "$TUBER_PYTHON" "$(cli_args)"
  printf '=== %s digest DRYRUN\n' "$(stamp)" >>"$LOG"
  exit 0
fi

# Оборона от параллельного запуска: второй прогон тихо выходит.
LOCK="$LOG_DIR/tuber_digest_slivki.lock"
exec 9>"$LOCK" || exit 0
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    printf '=== %s digest skipped: параллельный прогон уже идёт\n' \
      "$(stamp)" >>"$LOG"
    exit 0
  fi
fi

args="$(cli_args)"
printf '=== %s digest start\n' "$(stamp)" >>"$LOG"

# stdout CLI при --send пуст (успех/пустой день); ошибки — в журнал.
# shellcheck disable=SC2086
out="$(timeout "$HARD_TIMEOUT_SEC" "$TUBER_PYTHON" -m tuber digest slivki $args 2>>"$LOG")"; rc=$?
{
  printf '=== %s digest done rc=%s\n' "$(stamp)" "$rc"
  printf '%s\n' "$out"
} >>"$LOG"

if [ "$rc" -ne 0 ]; then
  printf 'ALERT: digest slivki: прогон не завершился (код %s), см. %s\n' \
    "$rc" "$LOG"
fi

exit 0
