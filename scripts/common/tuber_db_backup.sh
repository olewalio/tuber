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
#
# ТЗ-15 (09.10.2026): retention снятия дампов перенесён СЮДА из генерируемого
# запускателя `/root/.hermes/scripts/tuber_db_backup.sh` (ТЗ-14). Запускатель
# создаётся как тонкий `exec` этого воркера установщиком
# scripts/install_hermes_cron.sh, поэтому перегенерация шимов retention больше
# не уносит. После УСПЕШНОГО снятия нового дампа оставляем KEEP самых новых
# `tuber-*.db` (KEEP, по умолчанию 3, переопределяется TUBER_BACKUP_KEEP),
# остальные удаляем; чистим и осиротевшие парные `tuber-*.db-shm` / `.db-wal`
# без своего `.db`. Логика самой съёмки дампа (`--keep 14`) не меняется.
#
# Режим приёмки «только очистка»: TUBER_PRUNE_ONLY=1 — новый дамп не снимается,
# выполняется только retention и в stdout печатается строка результата.
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ALERT Tuber backup: интерпретатор контура не найден: $TUBER_PYTHON"
  exit 0
fi

PROJECT="${TUBER_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_LOG_DIR:-/root/.hermes/logs}"
LOG="${TUBER_BACKUP_LOG:-$LOG_DIR/tuber_db_backup.log}"
BACKUP_DIR="${TUBER_BACKUP_DIR:-$PROJECT/data/backups}"
# Сколько копий хранит сам `db backup` (питон). Отдельное имя, чтобы retention
# не конфликтовал с TUBER_BACKUP_KEEP (значение по умолчанию сохранено: 14).
DB_KEEP="${TUBER_DB_BACKUP_KEEP:-14}"
# Retention-очистка: сколько самых новых `tuber-*.db` оставить (ТЗ-15).
KEEP="${TUBER_BACKUP_KEEP:-3}"
mkdir -p "$LOG_DIR"
cd "$PROJECT" || { echo "ALERT Tuber backup: нет каталога $PROJECT"; exit 0; }

# ---------------------------------------------------------------------------
# Retention дампов (перенесено из ТЗ-14; эквивалент функции prune_backup_dumps).
# Счётчики (аналог глобальных переменных из шаблона ТЗ-14).
# PRUNE_REMOVED считает удалённые ДАМПЫ (tuber-*.db), PRUNE_ORPHANS —
# осиротевшие парные файлы -shm/-wal. Разделены, чтобы removed в отчёте
# приёмки означал именно удалённые дампы (ТЗ-15 §3).
PRUNE_REMOVED=0
PRUNE_KEPT=0
PRUNE_ORPHANS=0

# prune_backup_dumps <dir> <keep>
# Оставить <keep> самых новых tuber-*.db, удалить остальные; убрать осиротевшие
# tuber-*.db-shm / tuber-*.db-wal, для которых нет парного .db.
prune_backup_dumps() {
  local dir="$1" keep="$2"
  local -a dumps=()
  local f base total i

  PRUNE_REMOVED=0
  PRUNE_KEPT=0
  PRUNE_ORPHANS=0

  [ -d "$dir" ] || return 0

  # ls -t: от самого нового к самому старому (как в state_backup.sh).
  while IFS= read -r f; do
    dumps+=("$f")
  done < <(ls -t "$dir"/tuber-*.db 2>/dev/null)

  total=${#dumps[@]}
  for ((i = 0; i < total; i++)); do
    if ((i >= keep)); then
      rm -f -- "${dumps[i]}" && PRUNE_REMOVED=$((PRUNE_REMOVED + 1))
    else
      PRUNE_KEPT=$((PRUNE_KEPT + 1))
    fi
  done

  # Осиротевшие парные файлы SQLite (-shm/-wal) без своего .db.
  shopt -s nullglob
  for f in "$dir"/tuber-*.db-shm "$dir"/tuber-*.db-wal; do
    case "$f" in
      *.db-shm) base="${f%-shm}" ;;
      *.db-wal) base="${f%-wal}" ;;
      *) continue ;;
    esac
    if [ ! -e "$base" ]; then
      rm -f -- "$f" && PRUNE_ORPHANS=$((PRUNE_ORPHANS + 1))
    fi
  done
  return 0
}

# Строка об очистке — в тот же лог, что ведёт обёртка.
log_retention() {
  local tag="$1"
  mkdir -p "$LOG_DIR"
  printf '%s retention%s: removed=%d kept=%d keep=%d orphans=%d dir=%s\n' \
    "$(date '+%F %T')" "$tag" "$PRUNE_REMOVED" "$PRUNE_KEPT" "$KEEP" "$PRUNE_ORPHANS" "$BACKUP_DIR" >> "$LOG"
}

# Режим «только очистка» (приёмка ТЗ-15 §4): новый дамп не снимаем.
if [ "${TUBER_PRUNE_ONLY:-}" = "1" ]; then
  prune_backup_dumps "$BACKUP_DIR" "$KEEP"
  printf '%s retention(prune-only): removed=%d kept=%d keep=%d orphans=%d dir=%s\n' \
    "$(date '+%F %T')" "$PRUNE_REMOVED" "$PRUNE_KEPT" "$KEEP" "$PRUNE_ORPHANS" "$BACKUP_DIR"
  exit 0
fi

ARGS=(db backup --keep "$DB_KEEP")
[ -n "${TUBER_DB:-}" ] && ARGS+=(--db "$TUBER_DB")

TS="$(date '+%F %T')"
out="$("$TUBER_PYTHON" -m tuber "${ARGS[@]}" 2>&1)"
code=$?
{ printf '%s\n' "$out"; } >> "$LOG"

if [ "$code" -ne 0 ]; then
    reason="$(printf '%s\n' "$out" | grep -i 'ПРОБЛЕМА\|ошибка' | head -n 1)"
    [ -n "$reason" ] || reason="см. $LOG"
    printf '%s ALERT Tuber backup: бэкап единой базы не выполнен — %s\n' "$TS" "$reason"
    exit 0
fi

# Новый дамп снят и проверен — только теперь чистим старые.
prune_backup_dumps "$BACKUP_DIR" "$KEEP"
log_retention ""
exit 0
