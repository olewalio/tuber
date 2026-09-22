#!/usr/bin/env bash
# Tuber-x: сторож свежести для крона (ТЗ-5 задача 2).
#
# Поведение: В НОРМЕ МОЛЧИТ (пустой stdout => крону нечего отправлять),
# при аномалии печатает по одной строке ALERT на проблему, по-русски, с числами.
# Всегда возвращает 0: аномалия — это не сбой самого крона.
#
# Проверки (tuber_x/health.py): нет новых постов 6 ч; доля отказов канала > 50%
# за час; 429 на канале метрик > 20 за час; исчерпан суточный бюджет ленточного
# канала; доля постов с обрезанным текстом > 5% за сутки; инстанс Nitter в
# cooldown > 30 мин; доля text_src='cdn' > 5% за сутки.
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
LOG=/root/.hermes/logs/tuber_x_health.log

cd "$PROJECT" || { echo "ALERT: каталог $PROJECT недоступен"; exit 0; }
mkdir -p "$(dirname "$LOG")"

# Модуль health печатает в stdout только строки ALERT (в норме — ничего).
out="$("$TUBER_PYTHON" -m tuber.platforms.x.health 2>>"$LOG")"
rc=$?

if [ -n "$out" ]; then
  printf '%s\n' "$out"
  {
    printf '=== %s ALERT (rc=%s)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rc"
    printf '%s\n' "$out"
  } >>"$LOG"
fi
exit 0
