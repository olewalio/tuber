#!/usr/bin/env bash
# Tuber (монорепо): суточный бэкап ЕДИНОЙ базы + проверка целостности (ТЗ-5 §1.4).
#
# До сведения данных бэкапов было три (по одному на legacy-базу). Теперь он один:
# `python3 -m tuber db backup --keep 14` снимает копию в data/backups/ и пишет в
# data/backups/backup.log вердикт `PRAGMA integrity_check` источника и копии.
#
# В норме молчит в stdout (результат — в backup.log). При проблеме печатает одну
# строку ALERT (её доставит планировщик). Код возврата всегда 0: сбой бэкапа —
# не повод красить задание в «сбой», но одна строка владельцу уходит.
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ALERT Tuber backup: интерпретатор контура не найден: $TUBER_PYTHON"
  exit 0
fi

PROJECT="${TUBER_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_LOG_DIR:-/root/.hermes/logs}"
LOG="${TUBER_BACKUP_LOG:-$LOG_DIR/tuber_db_backup.log}"
KEEP="${TUBER_BACKUP_KEEP:-14}"
mkdir -p "$LOG_DIR"
cd "$PROJECT" || { echo "ALERT Tuber backup: нет каталога $PROJECT"; exit 0; }

ARGS=(db backup --keep "$KEEP")
[ -n "${TUBER_DB:-}" ] && ARGS+=(--db "$TUBER_DB")

TS="$(date '+%F %T')"
out="$("$TUBER_PYTHON" -m tuber "${ARGS[@]}" 2>&1)"
code=$?
{ printf '%s\n' "$out"; } >> "$LOG"

if [ "$code" -ne 0 ]; then
    reason="$(printf '%s\n' "$out" | grep -i 'ПРОБЛЕМА\|ошибка' | head -n 1)"
    [ -n "$reason" ] || reason="см. $LOG"
    printf '%s ALERT Tuber backup: бэкап единой базы не выполнен — %s\n' "$TS" "$reason"
fi
exit 0
