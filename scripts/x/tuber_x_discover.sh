#!/usr/bin/env bash
# Tuber-x: расширение реестра (дискавери) по расписанию (ТЗ-11 задача 1).
#
# Что делает: один прогон `cli discover` с ЯВНЫМИ потолками из конфига проекта:
#   * дневной бюджет поисковых запросов `config.DISCOVERY_DAILY_BUDGET`
#     (передаётся `--budget`, чтобы не полагаться на умолчание CLI);
#   * число кандидатов на верификацию `config.DISCOVERY_MAX_VERIFY_DEFAULT`
#     (передаётся `--max-verify`).
#
# Темп поиска Nitter не меняется: идут те же вызовы broker.fetch_search, что и
# при ручном запуске, поэтому штатный лимитер и суточный потолок запросов
# (ТЗ-1/ТЗ-2) соблюдаются без обхода. Прокси и платные API не используются.
#
# Поведение (образец обёрток ТЗ-7):
#   * в норме (прогон прошёл, состояние реестра не изменилось) — МОЛЧИТ в stdout;
#     весь вывод уходит в журнал /root/.hermes/logs/tuber_x_discover.log;
#   * при изменении состояния (нашлись кандидаты / прошла верификация) —
#     короткая сводка в stdout;
#   * при отказе прогона — строка ALERT в stdout (её доставит планировщик);
#   * если суточная квота уже исчерпана — прогон НЕ повторяется: в журнал
#     пишется `skipped: quota exhausted`, код возврата 0 (это не отказ).
#
# Всегда завершается кодом 0: сбой прогона — забота сторожа, а не уведомления
# крона (ТЗ-7). Каталоги проекта и журнала переопределяются переменными
# окружения TUBER_X_PROJECT / TUBER_X_LOG_DIR (нужны тестам и приёмке).
set -euo pipefail

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
LOG="$LOG_DIR/tuber_x_discover.log"

cd "$PROJECT" || { printf 'ALERT: дискавери: нет каталога %s\n' "$PROJECT"; exit 0; }
mkdir -p "$LOG_DIR"

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Потолки — строго из конфига проекта. Заодно читаем остаток суточной квоты
# дискавери (только чтение БД: `db.connect`, без миграций и записи).
mapfile -t cfg < <("$TUBER_PYTHON" - <<'PY' 2>>"$LOG"
import os
from tuber.platforms.x import config, db, discover
remain = None
if os.path.exists(config.DB_PATH):
    con = db.connect(config.DB_PATH)
    try:
        remain = discover.budget_remaining(con, config.DISCOVERY_DAILY_BUDGET)
    finally:
        con.close()
print(config.DISCOVERY_DAILY_BUDGET)
print(config.DISCOVERY_MAX_VERIFY_DEFAULT)
print("" if remain is None else remain)
PY
)
BUDGET="${cfg[0]:-}"
MAX_VERIFY="${cfg[1]:-}"
REMAIN="${cfg[2]:-}"

if [ -z "$BUDGET" ] || [ -z "$MAX_VERIFY" ]; then
  printf 'ALERT: дискавери: не удалось прочитать потолки из config (см. %s)\n' "$LOG"
  printf '=== %s discover ALERT: не удалось прочитать потолки из config\n' \
    "$(stamp)" >>"$LOG"
  exit 0
fi

# Пункт 4: квота исчерпана — не повторять прогон, отметить в журнале, код 0.
if [ -n "$REMAIN" ] && [ "$REMAIN" -le 0 ]; then
  printf '=== %s discover skipped: quota exhausted (остаток %s/%s за сутки)\n' \
    "$(stamp)" "$REMAIN" "$BUDGET" >>"$LOG"
  exit 0
fi

out=""
rc=0
out="$("$TUBER_PYTHON" -m tuber x discover --budget "$BUDGET" \
        --max-verify "$MAX_VERIFY" 2>>"$LOG")" || rc=$?

# Полный машинный вывод — в журнал (с временем и кодом возврата).
{
  printf '=== %s discover budget=%s max_verify=%s rc=%s\n' \
    "$(stamp)" "$BUDGET" "$MAX_VERIFY" "$rc"
  printf '%s\n' "$out"
} >>"$LOG"

# Разбор сводки прогона.
num() { printf '%s\n' "$1" | sed -n "s/.*$2=\([0-9][0-9]*\).*/\1/p" | tail -n 1; }
found="$(num "$out" 'кандидатов_найдено')"
vline="$(printf '%s\n' "$out" | grep '^верификация:' || true)"
checked="$(num "$vline" 'проверено')"
prov="$(num "$vline" 'provisional')"
rej="$(num "$vline" 'rejected')"
found="${found:-0}"; checked="${checked:-0}"; prov="${prov:-0}"; rej="${rej:-0}"

failed=0
[ "$rc" -ne 0 ] && failed=1
case "$out" in *Traceback*) failed=1 ;; esac

skipped=0
case "$out" in *пропущено*) skipped=1 ;; esac

if [ "$failed" -ne 0 ]; then
  printf 'ALERT: дискавери завершился отказом (rc=%s), см. %s\n' "$rc" "$LOG"
elif [ "$skipped" -eq 1 ]; then
  # Квота исчерпана гонкой: CLI уже записал причину в журнал. Норма, тишина.
  printf '=== %s discover skipped: quota exhausted (причина от CLI)\n' \
    "$(stamp)" >>"$LOG"
elif [ "$found" -gt 0 ] || [ "$checked" -gt 0 ]; then
  printf 'discover: кандидатов_найдено=%s, проверено=%s, provisional=%s, rejected=%s\n' \
    "$found" "$checked" "$prov" "$rej"
fi

exit 0
