#!/usr/bin/env bash
# Приёмка волны ТЗ-1 (см. docs/MIGRATION-LEGACY.md и §9 ТЗ).
#
# Что делает:
#   1. снимает sha256+mtime боевых legacy-баз ДО (доказательство нетронутости);
#   2. делает согласованные копии в /tmp через sqlite3 .backup (источник read-only);
#   3. мигрирует копии в /tmp/tuber-unified-test.db со сводкой;
#   4. сверяет parity (ожидается код 0);
#   5. печатает три показательных SQL-запроса к единой базе;
#   6. печатает число строк legacy_map и строк ядра по платформам;
#   7. повторно снимает sha256+mtime боевых баз.
#
# Боевые базы НЕ модифицируются: все обращения к ним — read-only. Если их mtime
# изменился, это сделали живые коллекторы (кроны), а не этот скрипт.
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан, и
# «голый» python3 уходит на системный SQLite. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi


cd "$(dirname "$0")/.."
ROOT="$(pwd)"
WORK="${WORK:-/tmp}"
TARGET="${WORK}/tuber-unified-test.db"
COPY_OS="${WORK}/copy-os.db"
COPY_X="${WORK}/copy-x.db"
COPY_TG="${WORK}/copy-tg.db"

OS_DB="${OS_DB:-/root/tuber-os/data/tuber.db}"
X_DB="${X_DB:-/root/tuber-x/data/tuber_x.db}"
TG_DB="${TG_DB:-/root/tuber-telegram/data/tuber_telegram.db}"

ORIG=( "$OS_DB" "$X_DB" "$TG_DB" )
COPIES=( "$COPY_OS" "$COPY_X" "$COPY_TG" )

hr() { printf '%s\n' "------------------------------------------------------------------------"; }

echo "== 1. Боевые legacy-базы: sha256 + mtime ДО =="
for f in "${ORIG[@]}"; do
  [ -f "$f" ] || { echo "НЕТ ФАЙЛА: $f (пропуск)"; continue; }
  sha256sum "$f"
  stat -c '%n mtime=%y size=%s' "$f"
done
hr

echo "== 2. Копии через sqlite3 .backup (источник read-only) =="
rm -f "$COPY_OS" "$COPY_X" "$COPY_TG" "$TARGET" \
      "$COPY_OS-wal" "$COPY_OS-shm" "$COPY_X-wal" "$COPY_X-shm" \
      "$COPY_TG-wal" "$COPY_TG-shm" "$TARGET-wal" "$TARGET-shm"
for i in 0 1 2; do
  src="${ORIG[$i]}"; dst="${COPIES[$i]}"
  if [ -f "$src" ]; then
    sqlite3 "file:${src}?mode=ro" -readonly ".backup '${dst}'" && echo "копия: $dst"
  fi
done
hr

echo "== 3. Миграция копий → $TARGET =="
"$TUBER_PYTHON" -m tuber migrate --target "$TARGET" \
  --os "$COPY_OS" --x "$COPY_X" --tg "$COPY_TG"
hr

echo "== 4. Parity (ожидается код 0) =="
"$TUBER_PYTHON" -m tuber parity --target "$TARGET" \
  --os "$COPY_OS" --x "$COPY_X" --tg "$COPY_TG"
PARITY_RC=$?
echo "parity exit code: ${PARITY_RC}"
hr

echo "== 4b. Повторная миграция на том же файле (идемпотентность + домерживание) =="
counts_sql="
SELECT 'content', COUNT(*) FROM content
UNION ALL SELECT 'classification', COUNT(*) FROM classification
UNION ALL SELECT 'candidate', COUNT(*) FROM candidate
UNION ALL SELECT 'cursor', COUNT(*) FROM cursor
UNION ALL SELECT 'classify_daily', COUNT(*) FROM classify_daily
UNION ALL SELECT 'legacy_map', COUNT(*) FROM legacy_map
UNION ALL SELECT 'title_ru_notnull', COUNT(*) FROM classification WHERE title_ru IS NOT NULL
UNION ALL SELECT 'summary_ru_notnull', COUNT(*) FROM classification WHERE summary_ru IS NOT NULL
UNION ALL SELECT 'reason_notnull', COUNT(*) FROM classification WHERE reason IS NOT NULL
UNION ALL SELECT 'display_handle_notnull', COUNT(*) FROM candidate WHERE display_handle IS NOT NULL;
"
sqlite3 -separator '|' "$TARGET" "$counts_sql" > "${WORK}/tuber-counts-before.txt"
"$TUBER_PYTHON" -m tuber migrate --target "$TARGET" \
  --os "$COPY_OS" --x "$COPY_X" --tg "$COPY_TG" >/dev/null
sqlite3 -separator '|' "$TARGET" "$counts_sql" > "${WORK}/tuber-counts-after.txt"
if diff -u "${WORK}/tuber-counts-before.txt" "${WORK}/tuber-counts-after.txt"; then
  echo "OK: числа после повторной миграции не изменились:"
  cat "${WORK}/tuber-counts-after.txt"
else
  echo "ОШИБКА: повторная миграция изменила числа (см. diff выше)"
fi
hr

echo "== 5. Закрытие долгов D-03/D-04/D-05 (числа legacy ↔ ядро) =="
echo "-- D-03: непустые title_ru/summary_ru (ядро = legacy):"
sqlite3 -header -column "$TARGET" "
SELECT
  (SELECT COUNT(*) FROM classification WHERE title_ru IS NOT NULL) AS core_title_ru,
  (SELECT COUNT(*) FROM classification WHERE summary_ru IS NOT NULL) AS core_summary_ru,
  (SELECT COUNT(*) FROM classification WHERE title_ru IS NOT NULL OR summary_ru IS NOT NULL) AS core_either,
  (SELECT COUNT(*) FROM classification WHERE reason IS NOT NULL) AS core_reason;"
echo "-- D-03: legacy (ожидаются те же числа):"
sqlite3 -header -column "file:${COPY_OS}?mode=ro" "
SELECT
  (SELECT COUNT(*) FROM video_classification WHERE title_ru IS NOT NULL) AS legacy_title_ru,
  (SELECT COUNT(*) FROM video_classification WHERE summary_ru IS NOT NULL) AS legacy_summary_ru,
  (SELECT COUNT(*) FROM video_classification WHERE title_ru IS NOT NULL OR summary_ru IS NOT NULL) AS legacy_either,
  (SELECT COUNT(*) FROM video_classification WHERE reason IS NOT NULL) AS legacy_reason;"
echo "-- D-05: курсоры X (account + search) в ядре; D-04: classify_daily:"
sqlite3 -header -column "$TARGET" "
SELECT
  (SELECT COUNT(*) FROM cursor WHERE platform='x') AS cursor_x,
  (SELECT COUNT(*) FROM cursor WHERE platform='x' AND kind='account') AS cursor_account,
  (SELECT COUNT(*) FROM cursor WHERE platform='x' AND kind='search') AS cursor_search,
  (SELECT COUNT(*) FROM classify_daily WHERE platform='x') AS classify_daily_x;"
echo "-- D-05/D-04: legacy X (ожидается cursor_x = cursors, classify_daily_x = classify_daily):"
sqlite3 -header -column "file:${COPY_X}?mode=ro" "
SELECT
  (SELECT COUNT(*) FROM cursors) AS legacy_cursors,
  (SELECT COUNT(*) FROM cursors WHERE kind='account') AS legacy_account,
  (SELECT COUNT(*) FROM cursors WHERE kind='search') AS legacy_search,
  (SELECT COUNT(*) FROM classify_daily) AS legacy_classify_daily;"
hr

echo "== 6. Демонстрационные запросы к ЕДИНОЙ базе =="
echo "-- (а) топ-5 content по views_per_day за последние 10 дней, все платформы сразу:"
sqlite3 -header -column "$TARGET" "
SELECT c.platform, c.external_id, m.captured_at, m.views_per_day
FROM metric_snapshot m JOIN content c ON c.id = m.content_id
WHERE m.views_per_day IS NOT NULL
  AND m.captured_at >= datetime('now','-10 days')
ORDER BY m.views_per_day DESC LIMIT 5;"
echo
echo "-- (б) счётчики content по платформам:"
sqlite3 -header -column "$TARGET" "SELECT platform, COUNT(*) AS n FROM content GROUP BY platform ORDER BY platform;"
echo
echo "-- (в) топ-5 score по significance с платформой:"
sqlite3 -header -column "$TARGET" "
SELECT c.platform, c.external_id, s.significance, s.computed_at
FROM score s JOIN content c ON c.id = s.content_id
ORDER BY s.significance DESC LIMIT 5;"
hr

echo "== 7. legacy_map и строки ядра по платформам =="
echo "-- legacy_map по legacy_db:"
sqlite3 -header -column "$TARGET" "SELECT legacy_db, COUNT(*) AS n FROM legacy_map GROUP BY legacy_db ORDER BY legacy_db;"
echo "-- всего legacy_map:"
sqlite3 "$TARGET" "SELECT COUNT(*) FROM legacy_map;"
echo "-- source/content/metric_snapshot/score по платформам:"
sqlite3 -header -column "$TARGET" "
SELECT s.platform,
       COUNT(DISTINCT s.id) AS sources,
       (SELECT COUNT(*) FROM content c WHERE c.platform=s.platform) AS content,
       (SELECT COUNT(*) FROM metric_snapshot m JOIN content c ON c.id=m.content_id WHERE c.platform=s.platform) AS snapshots,
       (SELECT COUNT(*) FROM score sc JOIN content c ON c.id=sc.content_id WHERE c.platform=s.platform) AS scores
FROM source s GROUP BY s.platform ORDER BY s.platform;"
hr

echo "== 8. Боевые legacy-базы: sha256 + mtime ПОСЛЕ =="
for f in "${ORIG[@]}"; do
  [ -f "$f" ] || continue
  sha256sum "$f"
  stat -c '%n mtime=%y size=%s' "$f"
done
echo
echo "Примечание: если sha256/mtime изменились, это работа живых коллекторов"
echo "(кронов legacy-проектов), а не этого скрипта: все обращения — mode=ro."

exit "$PARITY_RC"
