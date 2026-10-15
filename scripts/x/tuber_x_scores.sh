#!/usr/bin/env bash
# Tuber-x: сюжеты -> скоринг значимости (ТЗ-3 Р3, уточнено ТЗ-6 задача 3).
#
# В ежедневной цепочке шаг `scores` обязан идти ПОСЛЕ `classify` (нужны темы) и
# включать кластеризацию сюжетов: без `stories` блоки отчёта 1–4 пусты. Поэтому
# обёртка сначала пересобирает сюжеты, затем считает оси значимости.
#
# Модель не нужна: сеть здесь не используется.
#
# Лимит выборки (замер 17.09.2026 на копии боевой базы /root/tz5/score_test.db):
#   `scores --limit 400`  -> 3.1 с
#   `scores --limit 2000` -> 3.2 с
# Прежний лимит 50 был выставлен без замера: в таблице оценок всегда лежало
# только ~50 свежих строк, и подавляющая часть собранного X физически не могла
# попасть в выдачу (при 371 посте, проходящем фильтр скоринга). Лимит поднят до
# 500, чтобы один прогон покрывал все годные посты: рост стоимости не заметен
# (счёт идёт на десятые доли секунды).
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан, и
# «голый» python3 уходит на системный SQLite. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi


PROJECT=/root/tuber
LOG=/root/.hermes/logs/tuber_x_scores.log

cd "$PROJECT" || { echo "нет каталога $PROJECT" >>/dev/stderr; exit 3; }
mkdir -p "$(dirname "$LOG")"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) stories ==="
  "$TUBER_PYTHON" -m tuber x stories --limit 500
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) scores ==="
  "$TUBER_PYTHON" -m tuber x scores --limit 500
} >>"$LOG" 2>&1
exit 0
