#!/usr/bin/env bash
# Tuber-x: добор полного текста неполных постов (ТЗ-5 задача 1).
# НЕ чаще раза в час: перечитывает ленты аккаунтов из очереди.
#
# Оборона от перекрытия ежечасных прогонов (наблюдение 23–24.09.2026: прогоны
# длиннее 60 мин перекрывались, следующий запуск писал в ту же боевую базу):
#   * flock по образцу `tuber_comments_daily.sh` — второй прогон тихо выходит
#     строкой `fulltext skipped: параллельный прогон уже идёт`, код 0;
#   * внутренний жёсткий `timeout` вокруг CLI (`TUBER_X_FULLTEXT_TIMEOUT`,
#     по умолчанию 4800 с = 80 мин) — заведомо меньше потолка планировщика
#     (5400 с), чтобы обрыв фиксировал сам скрипт, а не убийство извне.
#     Норма прогона 34 мин, но при деградации Nitter (одна лента до 27 с, замер
#     24.09.2026) прогон выходит за 50 мин, а прежний потолок 50 мин обрубал
#     хвост очереди; 4800 с оставляет 10 мин запаса до потолка 5400 с;
#   * в норме МОЛЧИТ в stdout (тихая обёртка не должна давать сообщений в чат);
#     строка `ALERT: ...` — только при реальном сбое (пусто/ненулевой код/обрыв);
#   * всегда завершается кодом 0: сбой прогона — забота сторожа.
#
# Каталоги проекта и журнала переопределяются переменными окружения
# TUBER_X_PROJECT / TUBER_X_LOG_DIR (нужны тестам и приёмке).
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
LOG="$LOG_DIR/tuber_x_fulltext.log"

# Жёсткий внутренний timeout вокруг CLI (по умолчанию 80 мин). Нечисловое/
# пустое значение → дефолт.
FULLTEXT_TIMEOUT="${TUBER_X_FULLTEXT_TIMEOUT:-4800}"
case "$FULLTEXT_TIMEOUT" in ''|*[!0-9]*) FULLTEXT_TIMEOUT=4800 ;; esac

cd "$PROJECT" || { printf 'ALERT: fulltext: нет каталога %s\n' "$PROJECT"; exit 0; }
mkdir -p "$LOG_DIR" || { printf 'ALERT: fulltext: нет каталога журналов %s\n' "$LOG_DIR"; exit 0; }

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Оборона от параллельного запуска: второй прогон тихо выходит. Лок освобождает
# ядро само при закрытии fd 9 (в т.ч. при обрыве/сигнале).
LOCK="$LOG_DIR/tuber_x_fulltext.lock"
exec 9>"$LOCK" || exit 0
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    printf '=== %s fulltext skipped: параллельный прогон уже идёт\n' \
      "$(stamp)" >>"$LOG"
    exit 0
  fi
fi

printf '=== %s fulltext start\n' "$(stamp)" >>"$LOG"

out=""
rc=0
out="$(timeout "$FULLTEXT_TIMEOUT" "$TUBER_PYTHON" -m tuber x fulltext 2>>"$LOG")" || rc=$?

{
  printf '=== %s fulltext done rc=%s\n' "$(stamp)" "$rc"
  printf '%s\n' "$out"
} >>"$LOG"

if [ "$rc" -eq 124 ]; then
  printf '=== %s fulltext timeout после %ss (rc=124)\n' \
    "$(stamp)" "$FULLTEXT_TIMEOUT" >>"$LOG"
  printf 'ALERT: полный текст X: обрыв по внутреннему timeout после %ss, см. %s\n' \
    "$FULLTEXT_TIMEOUT" "$LOG"
elif [ "$rc" -ne 0 ]; then
  printf 'ALERT: полный текст X: прогон завершился отказом (rc=%s), см. %s\n' \
    "$rc" "$LOG"
elif [ -z "$out" ]; then
  printf 'ALERT: полный текст X: прогон не дал вывода, см. %s\n' "$LOG"
fi
# Норма — тишина: вывод уже в журнале.

exit 0
