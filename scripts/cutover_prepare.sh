#!/usr/bin/env bash
# Tuber: подготовка переключения на единую базу (ТЗ-5, дополнение владельца).
#
# На этапе переключения боевую единую базу надо ПЕРЕСОБРАТЬ из замороженных
# снимков legacy, иначе теряется дельта между «legacy сейчас» и «единая база».
# Этот скрипт делает всю подготовку одной командой и НЕ трогает боевые legacy:
#
#   а) снимает замороженные копии трёх legacy-баз (только чтение, SQLite backup
#      API) в отдельный каталог со штампом даты;
#   б) обязательно делает страховочную копию боевой единой базы ДО пересборки в
#      data/backups/ (с датой в имени) — штатная `tuber db backup`;
#   в) пересобирает боевую единую базу ИЗ СНИМКОВ:
#      `python3 -m tuber tools migrate --target ... --os ... --x ... --tg ...`;
#   г) проверяет схему: `migrate --schema-only` и требует «Проблем схемы ядра:
#      после 0»;
#   д) делает VACUUM и печатает до/после: размер, freelist_count, quick_check.
#
# Режим `--dry-run` печатает план и не меняет НИЧЕГО.
#
# ВАЖНО: запускать на боевой базе — шаг владельца на переключении, не этой волны.
# Проверка делается на копии: `TUBER_TARGET=... TUBER_SNAP_DIR=... scripts/cutover_prepare.sh`.
set -uo pipefail

PROJECT="${TUBER_PROJECT:-/root/tuber}"
PY="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
TARGET="${TUBER_TARGET:-$PROJECT/data/tuber.db}"
SNAP_ROOT="${TUBER_SNAP_DIR:-$PROJECT/data/cutover_snapshots}"
BACKUP_DIR="${TUBER_BACKUP_DIR:-$PROJECT/data/backups}"

OS_DB="${TUBER_OS_DB:-/root/tuber-os/data/tuber.db}"
X_DB="${TUBER_X_DB:-/root/tuber-x/data/tuber_x.db}"
TG_DB="${TUBER_TG_DB:-/root/tuber-telegram/data/tuber_telegram.db}"

DRYRUN=0
for arg in "$@"; do
  [ "$arg" = "--dry-run" ] && DRYRUN=1
done

STAMP="$(date -u +%Y%m%d-%H%M%S)"
SNAP_DIR="$SNAP_ROOT/$STAMP"
SNAP_OS="$SNAP_DIR/tuber-os.db"
SNAP_X="$SNAP_DIR/tuber_x.db"
SNAP_TG="$SNAP_DIR/tuber_telegram.db"

if [ ! -x "$PY" ]; then
  echo "ошибка: интерпретатор контура не найден: $PY" >&2
  exit 4
fi

echo "tuber cutover_prepare"
echo "  проект:      $PROJECT"
echo "  цель:        $TARGET"
echo "  снимки:      $SNAP_DIR"
echo "  страховка:   $BACKUP_DIR"
echo "  legacy OS:   $OS_DB"
echo "  legacy X:    $X_DB"
echo "  legacy TG:   $TG_DB"
[ "$DRYRUN" = "1" ] && echo "  РЕЖИМ:       --dry-run (ничего не меняем)"

if [ "$DRYRUN" = "1" ]; then
  echo ""
  echo "план:"
  echo "  1. снять read-only снимки legacy в $SNAP_DIR"
  echo "  2. страховочная копия $TARGET в $BACKUP_DIR (tuber db backup)"
  echo "  3. migrate --target $TARGET --os $SNAP_OS --x $SNAP_X --tg $SNAP_TG"
  echo "  4. migrate --schema-only (требование: «Проблем схемы ядра: после 0»)"
  echo "  5. VACUUM + печать до/после (размер, freelist_count, quick_check)"
  echo "ничего не изменено."
  exit 0
fi

for f in "$OS_DB" "$X_DB" "$TG_DB"; do
  if [ ! -f "$f" ]; then
    echo "ошибка: нет legacy-базы $f" >&2
    exit 2
  fi
done
if [ ! -f "$TARGET" ]; then
  echo "ошибка: нет боевой единой базы $TARGET" >&2
  echo "  (на пересборку с нуля этот скрипт не рассчитан — нужна страховка)" >&2
  exit 2
fi

mkdir -p "$SNAP_DIR"

echo ""
echo "== а) замороженные снимки legacy (read-only) =="
"$PY" - "$OS_DB" "$SNAP_OS" "$X_DB" "$SNAP_X" "$TG_DB" "$SNAP_TG" <<'PY'
import sqlite3, sys
pairs = list(zip(sys.argv[1::2], sys.argv[2::2]))
for src, dst in pairs:
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close(); s.close()
    print(f"  снимок: {src} -> {dst}")
PY
if [ $? -ne 0 ]; then echo "ошибка: снимок legacy не снят" >&2; exit 3; fi

echo ""
echo "== страховочная копия боевой единой базы ДО пересборки =="
"$PY" -m tuber db backup --db "$TARGET" --dir "$BACKUP_DIR" --keep 14 || {
  echo "ошибка: страховочная копия не сделана — пересборка отменена" >&2
  exit 3
}

echo ""
echo "== б) пересборка единой базы из снимков =="
"$PY" -m tuber tools migrate --target "$TARGET" \
  --os "$SNAP_OS" --x "$SNAP_X" --tg "$SNAP_TG"
migrate_rc=$?
if [ "$migrate_rc" -ne 0 ]; then
  echo "ошибка: migrate вернул код $migrate_rc" >&2
  exit "$migrate_rc"
fi

echo ""
echo "== в) проверка схемы: migrate --schema-only =="
schema_out="$("$PY" -m tuber tools migrate --target "$TARGET" --schema-only)"
echo "$schema_out"
if ! printf '%s\n' "$schema_out" | grep -q "Проблем схемы ядра: до [0-9]*, после 0\."; then
  echo "ошибка: после миграции схема ядра не чиста (ожидалось «после 0»)" >&2
  exit 3
fi
echo "схема ядра чиста: «Проблем схемы ядра: после 0»."

echo ""
echo "== г) VACUUM и метрики до/после =="
"$PY" - "$TARGET" <<'PY'
import os, sqlite3, sys
path = sys.argv[1]

def metrics():
    c = sqlite3.connect(path)
    try:
        page = c.execute("PRAGMA page_size").fetchone()[0]
        free = c.execute("PRAGMA freelist_count").fetchone()[0]
        qc = c.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        c.close()
    return os.path.getsize(path), free * page, qc

size0, free0, qc0 = metrics()
c = sqlite3.connect(path)
try:
    c.execute("VACUUM")
finally:
    c.close()
size1, free1, qc1 = metrics()
print(f"  размер:        {size0} -> {size1} байт")
print(f"  freelist:      {free0} -> {free1} байт")
print(f"  quick_check:   {qc0} -> {qc1}")
PY

echo ""
echo "готово: боевая единая база пересобрана из снимков $SNAP_DIR."
echo "снимки legacy оставлены как страховка; кроны переключает владелец."
exit 0
