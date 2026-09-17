#!/usr/bin/env bash
# Tuber: сквозные сюжеты — связывание материалов X, Telegram, YouTube в один сюжет.
# Создан вручную 17.09.2026 (владельческая часть: команда не была подключена к
# расписанию после её появления в коммите 254b40a). Перенесён в монорепо
# 17.09.2026 (волна tz6debt): единый источник расписания — блоки JOBS/LAUNCHERS
# в scripts/install_hermes_cron.sh.
#
# Зачем: блок 4 суточной выдачи («Сквозной сюжет») читает story_member, но сама
# сборка сквозных сюжетов идёт отдельной командой `x cross-stories`. Пока её не
# вызывает никто из расписания, блок остаётся пустым даже при рабочем движке.
# Ставится ДО суточного отчёта, после классификации моделью.
#
# Порог и окно выбраны ЗАМЕРОМ, а не на глаз (scripts/x_cross_story_eval.py;
# разметки docs/cross-story-labels.jsonl — 47 пар, окно 72ч, и
# docs/cross-story-labels-240.jsonl — 64 пары, окно 240ч):
#
#   до правки (254b40a), 0.30        — 16 сюжетов, точность 0.19;
#   после fab9ce8, окно 72ч / 0.15   — 14 сюжетов: 0.667 на старой разметке,
#                                      но 0.444 на новой — окна мало;
#   после fab9ce8, окно 240ч / 0.15  — 12 сюжетов: 0.875/0.600 на новой
#                                      разметке и 1.000/0.333 на старой.
#
# В бой идёт ОКНО 240ч + порог 0.15: подтверждено повторным независимым прогоном
# 17.09.2026 на своей копии (копия ver15.db: precision 0.875, recall 0.600,
# F1 0.712; боевая база при прогонах не изменялась). Цена: 14.9 с, 545 МБ RSS —
# в пределах лимита 60 с / 1 ГБ.
#
# Поведение: при успехе stdout ПУСТ (задание no_agent молчит — это норма);
# при неуспехе в stdout уходит строка ALERT, чтобы поломка не была молчаливой.
#
# Использование: tuber_x_cross_stories.sh [доп. аргументы cross-stories]
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH: в cron PATH урезан, и «голый» python3
# уходит на системный SQLite < 3.46 (см. D-32 — интерпретатор в обёртках и
# квалифицированный DML в TEMP-триггерах при SQLite < 3.46).
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
PROJECT=/root/tuber
LOG=/root/.hermes/logs/tuber_x_cross_stories.log
TEXT_MIN="${CROSS_STORY_TEXT_MIN:-0.15}"
WINDOW="${CROSS_STORY_WINDOW:-240}"

if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ALERT Tuber-x: интерпретатор контура не найден: $TUBER_PYTHON"
  exit 0
fi
if [ ! -d "$PROJECT" ]; then
  echo "ALERT Tuber-x: нет каталога проекта: $PROJECT"
  exit 0
fi

cd "$PROJECT" || { echo "ALERT Tuber-x: не удалось войти в $PROJECT"; exit 0; }
mkdir -p "$(dirname "$LOG")"

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) cross-stories --window $WINDOW --text-min $TEXT_MIN ===" >>"$LOG"

OUT="$("$TUBER_PYTHON" -m tuber x cross-stories --window "$WINDOW" --text-min "$TEXT_MIN" "$@" 2>>"$LOG")"
RC=$?
printf '%s\n' "$OUT" >>"$LOG"
echo "--- код возврата: $RC" >>"$LOG"

if [ "$RC" -ne 0 ]; then
  echo "ALERT Tuber-x: сквозные сюжеты не собраны (код $RC, подробности в $LOG)"
  exit 0
fi

# Статистику — в журнал, stdout оставляем пустым (молчание при норме).
printf '%s\n' "$OUT" | grep -E "сквозных|участников|пар" | tail -3 >>"$LOG"
exit 0
