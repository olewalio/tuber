#!/usr/bin/env bash
# Tuber-YouTube (монорепо): суточный цикл по расписанию (перенос tuber-os
# tuber_daily.sh в монорепозиторий, ТЗ-5).
#
# Запускает `python3 -m tuber yt daily --json` в /root/tuber. Вся логика — в CLI.
# Здесь: рабочий каталог, явный интерпретатор контура, журнал с датой, одна
# короткая строка итога в stdout (уйдёт владельцу) и код 0 при штатном прогоне.
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_YT_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_YT_LOG_DIR:-/root/.hermes/logs}"
LOG="${TUBER_YT_DAILY_LOG:-$LOG_DIR/tuber_yt_daily.log}"
mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 3

ARGS=(daily --json)
[ -n "${TUBER_DB:-}" ] && ARGS+=(--db "$TUBER_DB")

started=$(date +%s)
TS="$(date '+%Y-%m-%d %H:%M:%S')"

output="$("$TUBER_PYTHON" -m tuber yt "${ARGS[@]}" 2>&1)"
code=$?
elapsed=$(( $(date +%s) - started ))

if [ -n "$output" ]; then
    printf '%s\n' "$output" | while IFS= read -r line || [ -n "$line" ]; do
        printf '%s %s\n' "$TS" "$line"
    done >> "$LOG"
fi

if [ "$code" -ne 0 ]; then
    reason="$(printf '%s\n' "$output" | head -n 1)"
    [ -n "$reason" ] || reason="неизвестная ошибка"
    printf '%s Tuber_YouTube daily: ОШИБКА за %ss — %s\n' "$TS" "$elapsed" "$reason"
    exit 1
fi

case "$output" in
    "прогон уже идёт"*)
        printf '%s Tuber_YouTube daily: прогон уже идёт, пропущено (за %ss).\n' "$TS" "$elapsed"
        exit 0
        ;;
esac

summary="$(printf '%s' "$output" | "$TUBER_PYTHON" -c '
import json, sys
try:
    payload = json.loads(sys.stdin.read())
except Exception:
    print("итог не разобран"); sys.exit(0)
collect = payload.get("collect") or {}
classify = payload.get("classify") or {}
snapshots = payload.get("snapshots") or {}
text = payload.get("report_text") or ""
parts = [
    "собрано новых {}".format(collect.get("new", 0)),
    "разобрано {}".format(classify.get("classified", 0)),
    "снято замеров {}".format(snapshots.get("captured", 0)),
]
comments = payload.get("comments")
if isinstance(comments, dict):
    if comments.get("error"):
        parts.append("комментариев: сбой")
    else:
        parts.append("комментариев {} по {} видео".format(
            comments.get("comments", 0), comments.get("videos", 0)))
if payload.get("report_file_error"):
    parts.append("отчёт {} знаков → файл не записан: {}".format(
        len(text), payload["report_file_error"]))
elif payload.get("report_file"):
    parts.append("отчёт {} знаков → {}".format(len(text), payload["report_file"]))
else:
    parts.append("отчёт {} знаков".format(len(text)))
rotated = payload.get("report_rotated") or 0
if rotated > 0:
    parts.append("архив: {} файлов".format(rotated))
print(", ".join(parts))
')"
[ -n "$summary" ] || summary="итог не разобран"

printf '%s Tuber_YouTube daily: %s (за %ss).\n' "$TS" "$summary" "$elapsed"
exit 0
