#!/usr/bin/env bash
# Tuber: ЕДИНЫЙ установщик запускалок для планировщика Hermes (ТЗ-5 §1).
#
# До этой волны было ТРИ независимых набора заданий и три установщика. Теперь
# список заданий один и живёт ЗДЕСЬ: блоки JOBS / NEW_JOBS / LAUNCHERS ниже —
# единственный источник истины о расписании монорепо. Тест
# tests/test_schedule_sync.py сверяет его с фактическим /root/.hermes/cron/jobs.json.
#
# ВАЖНО (граница работ ТЗ-5): реестр заданий принадлежит планировщику Hermes и
# правит его ВЛАДЕЛЕЦ штатным инструментом. Этот установщик:
#   * пишет ТОЛЬКО шимы в ~/.hermes/scripts/ — обычные файлы (не симлинки:
#     планировщик разыменовывает путь и отклоняет выход из каталога);
#   * печатает таблицу заданий; режим `--dry-run` ничего не пишет;
#   * для НОВЫХ заданий печатает отдельный блок «требуется регистрация владельцем»;
#   * НЕ трогает jobs.json и не пишет в crontab.
#
# Что делает:
#   * LAUNCHERS — «шим : цель-в-монорепо : аргумент : команда монорепо». Шим
#     делегирует в обёртку монорепо (scripts/x|telegram|youtube|common), а та
#     вызывает `python3 -m tuber <платформа> <команда>`. Так сохраняются журналы,
#     загрузка ключей из /root/.hermes/.env и формат строк-алертов, наработанные
#     обёртками платформ, — расписание при этом единое, а цель команд монорепо.
#   * JOBS — уже зарегистрированные владельцем задания: 19 штук, имена и времена
#     СОХРАНЕНЫ ровно как в jobs.json (меняется только содержимое шрамов).
#   * NEW_JOBS — новые общие задания (бэкап единой базы, объединённая выдача).
#     Они объявлены здесь, шим пишется, но задание регистрирует владелец.
#   * идемпотентен: повторный запуск даёт те же шимы и код 0 (перезапись всегда,
#     время/состав обновляются);
#   * удаляет «осиротевшие» шимы, которые ставил этот же установщик (по маркеру),
#     если их имена больше не объявлены — «удаление заданий с устаревшими именами»;
#   * посторонние файлы в каталоге планировщика не трогает.
#
# Режим проверки без записи: `--dry-run` или TUBER_INSTALL_DRYRUN=1.
# Каталог планировщика переопределяется HERMES_SCRIPTS_DIR (для тестов/приёмки),
# корень проекта — TUBER_PROJECT, интерпретатор контура — TUBER_PYTHON.
#
# Запуск: scripts/install_hermes_cron.sh [--dry-run]
set -uo pipefail

PROJECT="${TUBER_PROJECT:-/root/tuber}"
SCRIPTS_DIR="${HERMES_SCRIPTS_DIR:-/root/.hermes/scripts}"
JOBS_JSON="${TUBER_HERMES_JOBS_JSON:-/root/.hermes/cron/jobs.json}"

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

DRYRUN="${TUBER_INSTALL_DRYRUN:-0}"
for arg in "$@"; do
  [ "$arg" = "--dry-run" ] && DRYRUN=1
done

# Маркер управляемых шрамов: по нему распознаются «осиротевшие» файлы.
MARKER="# Управляется scripts/install_hermes_cron.sh (ТЗ-5). Правка вручную будет перезаписана."

# «шим : цель-в-монорепо : аргумент : команда монорепо» (для печати).
LAUNCHERS=(
  # --- YouTube (был tuber-os) ---
  "tuber_snapshots.sh:scripts/youtube/tuber_snapshots.sh::python3 -m tuber yt snapshots"
  "tuber_daily.sh:scripts/youtube/tuber_daily.sh::python3 -m tuber yt daily --json"
  "tuber_os_feed_export.sh:scripts/youtube/tuber_os_feed_export.sh::python3 -m tuber yt candidates-export"
  "tuber_os_feed_import.sh:scripts/youtube/tuber_os_feed_import.sh::python3 -m tuber yt candidates-import --feed data/exchange/tg_candidates.jsonl"
  # --- X (был tuber-x) ---
  "tuber_x_collect_a.sh:scripts/x/tuber_x_collect.sh:A:python3 -m tuber x collect --tier A"
  "tuber_x_collect_b.sh:scripts/x/tuber_x_collect.sh:B:python3 -m tuber x collect --tier B"
  "tuber_x_collect_c.sh:scripts/x/tuber_x_collect.sh:C:python3 -m tuber x collect --tier C"
  "tuber_x_enrich.sh:scripts/x/tuber_x_enrich.sh::python3 -m tuber x enrich --batch 400"
  "tuber_x_fulltext.sh:scripts/x/tuber_x_fulltext.sh::python3 -m tuber x fulltext"
  "tuber_x_synd.sh:scripts/x/tuber_x_synd.sh::python3 -m tuber x synd-snapshot --tier A --accounts 5"
  "tuber_x_classify.sh:scripts/x/tuber_x_classify.sh::python3 -m tuber x classify"
  "tuber_x_scores.sh:scripts/x/tuber_x_scores.sh::python3 -m tuber x scores --limit 500"
  "tuber_x_cross_stories.sh:scripts/x/tuber_x_cross_stories.sh::python3 -m tuber x cross-stories --window 240 --text-min 0.15"
  "tuber_x_report.sh:scripts/x/tuber_x_report.sh::python3 -m tuber x report"
  "tuber_x_health.sh:scripts/x/tuber_x_health.sh::python3 -m tuber x health (tuber_x_health)"
  "tuber_x_discover.sh:scripts/x/tuber_x_discover.sh::python3 -m tuber x discover"
  # --- Telegram (был tuber-telegram) ---
  "tuber_telegram_collect.sh:scripts/telegram/tuber_telegram_collect.sh::python3 -m tuber tg collect --mode web"
  "tuber_telegram_feed_export.sh:scripts/telegram/tuber_telegram_feed_export.sh::python3 -m tuber tg bridge export"
  "tuber_telegram_feed_import.sh:scripts/telegram/tuber_telegram_feed_import.sh::python3 -m tuber tg bridge import --feed data/exchange/external_candidates.jsonl"
  "tuber_telegram_discover.sh:scripts/telegram/tuber_telegram_discover.sh::python3 -m tuber tg discover"
  # --- общие (монорепо) ---
  "tuber_db_backup.sh:scripts/common/tuber_db_backup.sh::python3 -m tuber db backup --keep 14"
  "tuber_report.sh:scripts/common/tuber_report.sh::python3 -m tuber report --save"
)

# Задания, УЖЕ зарегистрированные владельцем: «шим : расписание (МСК) : подпись».
# Имена и времена совпадают с jobs.json (ТЗ-5 §1.2: часы прогонов не менять).
JOBS=(
  "tuber_snapshots.sh:0 */3 * * *:YouTube: замеры просмотров"
  "tuber_daily.sh:0 7 * * *:YouTube: суточный цикл (сбор, разбор, замеры, отчёт)"
  "tuber_telegram_collect.sh:*/30 * * * *:Telegram: сбор постов (web)"
  "tuber_x_collect_a.sh:0 * * * *:X: сбор тира A"
  "tuber_x_collect_b.sh:15 */4 * * *:X: сбор тира B"
  "tuber_x_collect_c.sh:30 3 * * *:X: сбор тира C"
  "tuber_x_enrich.sh:5 * * * *:X: метрики (лайки/ответы, каждый час)"
  "tuber_x_fulltext.sh:20 * * * *:X: полный текст"
  "tuber_x_synd.sh:40 4 * * *:X: разовый снимок ленты"
  "tuber_x_classify.sh:20 */2 * * *:X: классификация моделью"
  "tuber_x_scores.sh:40 */2 * * *:X: сюжеты и оценки (каждые 2 ч)"
  "tuber_x_cross_stories.sh:50 6 * * *:X: сквозные сюжеты (X+Telegram+YouTube)"
  "tuber_x_report.sh:0 7 * * *:X: сводный отчёт"
  "tuber_x_health.sh:*/30 * * * *:X: сторож (алерты в Telegram)"
  "tuber_x_discover.sh:50 4 * * *:X: расширение реестра (дискавери)"
  "tuber_os_feed_export.sh:10 6 * * *:YouTube: экспорт фида кандидатов"
  "tuber_os_feed_import.sh:50 6 * * *:YouTube: импорт кандидатов из фида Telegram"
  "tuber_telegram_feed_export.sh:30 6 * * *:Telegram: экспорт фида X/YouTube"
  "tuber_telegram_feed_import.sh:35 6 * * *:Telegram: импорт каналов из фида YouTube"
  "tuber_telegram_discover.sh:40 7 * * *:Telegram: дискавери каналов из своих постов"
)

# НОВЫЕ задания монорепо: объявлены здесь и шим ставится, но задание регистрирует
# ВЛАДЕЛЕЦ (установщик в jobs.json не пишет). «шим : расписание : подпись».
NEW_JOBS=(
  "tuber_db_backup.sh:0 5 * * *:Tuber: суточный бэкап единой базы + integrity_check"
  "tuber_report.sh:0 8 * * *:Tuber: объединённая выдача по трём платформам"
)

if [ "$DRYRUN" = "1" ]; then
  echo "Tuber: ТЕСТОВЫЙ РЕЖИМ (боевой каталог $SCRIPTS_DIR не трогаем)"
else
  mkdir -p "$SCRIPTS_DIR" || {
    echo "ошибка: не удалось создать каталог $SCRIPTS_DIR" >&2
    exit 1
  }
  echo "Tuber: установка запускалок в $SCRIPTS_DIR"
fi
echo "источник обёрток: $PROJECT/scripts"
echo ""

# Имена всех управляемых шрамов (JOBS + NEW_JOBS).
all_names=()
for entry in "${JOBS[@]}" "${NEW_JOBS[@]}"; do
  all_names+=("${entry%%:*}")
done
declare -A WANTED=()
for n in "${all_names[@]}"; do WANTED["$n"]=1; done

install_one() {
  # install_one <name> <target> <arg> <monorepo-cmd>
  local name="$1" target="$2" arg="$3" cmd="$4"
  local full="$PROJECT/$target"
  if [ ! -f "$full" ]; then
    echo "  ОШИБКА: нет обёртки $full" >&2
    return 1
  fi
  local exec_line="exec $full"
  [ -n "$arg" ] && exec_line="$exec_line $arg"

  if [ "$DRYRUN" = "1" ]; then
    echo "  будет создан файл $SCRIPTS_DIR/$name -> $exec_line"
    return 0
  fi
  local dest="$SCRIPTS_DIR/$name"
  rm -f "$dest"   # снять прежний симлинк/файл строго по имени
  {
    printf '#!/usr/bin/env bash\n'
    printf '%s\n' "$MARKER"
    printf '# Цель (монорепо): %s\n' "$cmd"
    printf '# Источник расписания: JOBS/NEW_JOBS в scripts/install_hermes_cron.sh.\n'
    printf 'if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then\n'
    printf '  printf "%%s\\n" "%s"\n' "$cmd"
    printf '  exit 0\n'
    printf 'fi\n'
    printf '%s\n' "$exec_line"
  } > "$dest"
  chmod +x "$dest"
  if [ ! -f "$dest" ]; then
    echo "  ОШИБКА: не удалось записать $dest" >&2
    return 1
  fi
  echo "  $name -> $exec_line"
  return 0
}

echo "установлено:"
shims=0; errors=0
for entry in "${LAUNCHERS[@]}"; do
  IFS=: read -r name target arg cmd <<<"$entry"
  if install_one "$name" "$target" "$arg" "$cmd"; then
    shims=$((shims + 1))
  else
    errors=$((errors + 1))
  fi
done

# Удаление «осиротевших» шрамов, которые ставил этот установщик.
removed=()
if [ "$DRYRUN" = "0" ] && [ -d "$SCRIPTS_DIR" ]; then
  while IFS= read -r -d '' f; do
    base="$(basename "$f")"
    case "$base" in tuber_*.sh) ;; *) continue ;; esac
    [ -L "$f" ] && continue
    [ -z "${WANTED[$base]:-}" ] || continue
    grep -qF "$MARKER" "$f" 2>/dev/null || continue
    rm -f "$f" && removed+=("$base")
  done < <(find "$SCRIPTS_DIR" -maxdepth 1 -type f -name 'tuber_*.sh' -print0)
fi

if [ "$DRYRUN" = "1" ]; then
  echo ""
  echo "итого запускалок: ${#LAUNCHERS[@]}"
  echo "в тестовом режиме ничего не записано (боевой каталог не тронут)"
else
  symlinks=0
  for entry in "${LAUNCHERS[@]}"; do
    name="${entry%%:*}"
    [ -L "$SCRIPTS_DIR/$name" ] && symlinks=$((symlinks + 1))
  done
  echo ""
  echo "итого запускалок: ${#LAUNCHERS[@]}"
  echo "шимы установлены: $shims (симлинков: $symlinks)"
  if [ "${#removed[@]}" -gt 0 ]; then
    echo "удалено устаревших шимов: ${#removed[@]} (${removed[*]})"
  else
    echo "устаревших шимов не найдено"
  fi
fi

echo ""
echo "задания планировщика Hermes (время МСК), регистрирует владелец:"
printf '  %-11s %-24s %s\n' "расписание" "шим" "подпись"
for entry in "${JOBS[@]}"; do
  IFS=: read -r name schedule label <<<"$entry"
  printf '  %-11s %-24s %s\n' "$schedule" "$name" "$label"
done

echo ""
echo "=== ТРЕБУЕТСЯ РЕГИСТРАЦИЯ ВЛАДЕЛЬЦЕМ (новые задания монорепо) ==="
echo "Задания ниже объявлены установщиком и шимы уже стоят, но в jobs.json их"
echo "добавляет ВЛАДЕЛЕЦ штатным инструментом (установщик реестр не правит):"
printf '  %-11s %-24s %s\n' "расписание" "шим" "подпись"
for entry in "${NEW_JOBS[@]}"; do
  IFS=: read -r name schedule label <<<"$entry"
  printf '  %-11s %-24s %s\n' "$schedule" "$name" "$label"
done

# Сверка с фактическим реестром: зарегистрированные, но не объявленные задания.
if [ -f "$JOBS_JSON" ] && command -v python3 >/dev/null 2>&1; then
  declared_names="$(printf '%s\n' "${all_names[@]}")"
  echo ""
  echo "=== СВЕРКА С РЕЕСТРОМ $JOBS_JSON ==="
  DECLARED="$declared_names" JOBS_JSON="$JOBS_JSON" python3 - <<'PY'
import json, os, sys
try:
    data = json.load(open(os.environ["JOBS_JSON"], encoding="utf-8"))
except Exception as exc:
    print("  реестр не прочитан: %s" % exc)
    sys.exit(0)
jobs = data.get("jobs") if isinstance(data, dict) else data
declared = {n for n in (os.environ.get("DECLARED") or "").split("\n") if n}
extra = []
missing = []
actual = {}
for job in jobs or []:
    if not isinstance(job, dict):
        continue
    script = job.get("script")
    if not isinstance(script, str):
        continue
    base = os.path.basename(script)
    if not base.startswith("tuber_"):
        continue
    sched = (job.get("schedule") or {}).get("expr")
    actual[base] = sched
    if base not in declared:
        extra.append((base, sched, job.get("name")))
for n in sorted(declared):
    if n not in actual:
        missing.append(n)
if extra:
    print("  зарегистрированы в планировщике, но НЕ объявлены установщиком")
    print("  (удаление таких заданий — за владельцем):")
    for base, sched, name in extra:
        print("    %-26s %-11s %s" % (base, sched or "?", name or ""))
else:
    print("  лишних заданий Tuber в реестре нет.")
if missing:
    print("  объявлены установщиком, но в реестре отсутствуют"
          " (ожидают регистрации владельцем):")
    for n in missing:
        print("    %s" % n)
PY
else
  echo ""
  echo "реестр $JOBS_JSON не найден — сверка пропущена (для приёмки задайте путь)."
fi

echo ""
echo "в crontab ничего не записано и jobs.json не изменён: задания регистрирует владелец."
echo "обёртки печатают строку только при аномалии; в планировщике Hermes такие"
echo "строки доставляются владельцу в Telegram."

if [ "$errors" -ne 0 ]; then
  echo "ошибка: не установлено обёрток: $errors" >&2
  exit 1
fi
exit 0
