#!/usr/bin/env bash
# Tuber-x: ежедневная выдача владельцу (ТЗ-6 задача 2; доставка — ТЗ-12 Р1).
#
# Что делает: `cli report` — собирает 7 блоков и (в режиме записи) пишет файл
# `<TUBER_X_REPORT_DIR>/YYYY-MM-DD.md` за дату (по умолчанию сегодня UTC),
# попутно наполняя `report_texts` кэшем переводов. Каталог выдачи
# переопределяется переменной окружения TUBER_X_REPORT_DIR: это нужно приёмке и
# параллельным прогонам, которые обязаны писать во временный каталог и не
# трогать рабочий `reports/`. Отчёт ОБЯЗАН отработать каждый день, даже если
# новых постов нет: тогда блоки честно пишут «нет данных за сутки», а файл всё
# равно создаётся.
#
# Ключ модели (для перевода текстов, Р1.6) берётся ТОЛЬКО из окружения. Если
# DEEPSEEK_API_KEY не задан — подгружаем из /root/.hermes/.env, не печатая его.
# Без ключа отчёт не падает: перевод просто не выполняется (русские шаблоны).
#
# ДОСТАВКА (ТЗ-12 Р1). Задание планировщика `no_agent`: владельцу уходит ровно
# то, что обёртка напечатала в stdout. Раньше stdout был пуст, планировщик
# писал «[SILENT] — skipping delivery», и сводка существовала только файлом на
# сервере. Теперь:
#   * Р1.1 — в stdout идёт КОМПАКТНАЯ сводка: заголовок с датой, блок «Главное
#     за сутки» (до 5 сюжетов: текст, автор, ссылка) и одной строкой цифры
#     (сюжетов за сутки, постов в базе, доля отказов сбора, расход квоты
#     Nitter, кандидатов в реестре, путь к полному файлу);
#   * Р1.2 — stdout ≤ 3500 знаков (лимит Telegram 4096). При превышении
#     отбрасываются ЦЕЛЫЕ сюжеты: обрезка посреди строки или посреди ссылки
#     запрещена, владелец проверяет ссылки кликом;
#   * Р1.3 — при неуспехе (CLI вернул непустой код, файл отчёта за дату не
#     создан, вывод не разобрался) в stdout печатается строка
#     `ALERT Tuber-x: отчёт за <дата> не собран: <причина>` и код возврата 0.
#     Молчание при неуспехе запрещено: именно оно маскировало дефект до сих пор;
#   * Р1.4 — полный вывод CLI по-прежнему идёт в журнал
#     /root/.hermes/logs/tuber_x_report.log (плюс код возврата и режим);
#   * Р1.5 — сводка печатается и при повторном прогоне за ту же дату (состояние
#     на момент запуска), но перезапись файла отчёта запрещена, если явный
#     прогон за дату не запрошен. Поэтому без аргумента: файла за сегодня нет —
#     обычный прогон (файл создаётся), файл есть — прогон `--stdout-only`
#     (файл не трогаем, считаем заново по текущей базе). С аргументом-датой —
#     явный прогон за дату, файл перезаписывается.
#
# Поведение: всегда завершается кодом 0; stdout — либо сводка, либо ALERT.
#
# Использование: tuber_x_report.sh [YYYY-MM-DD]
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
LOG=/root/.hermes/logs/tuber_x_report.log
ENV_FILE=/root/.hermes/.env
DATE="${1:-}"

cd "$PROJECT" || { echo "нет каталога $PROJECT" >>/dev/stderr; exit 3; }
mkdir -p "$(dirname "$LOG")"

load_key() {
  local name="$1" value
  eval "value=\${$name:-}"
  [ -n "$value" ] && return 0
  [ -f "$ENV_FILE" ] || return 0
  value="$(sed -n "s/^${name}=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r')"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  [ -n "$value" ] && export "$name=$value"
  return 0
}
load_key DEEPSEEK_API_KEY

# Дата отчёта и путь к полному файлу (тот же каталог, что видит CLI: config.REPORT_DIR).
REPORT_DATE="$DATE"
[ -n "$REPORT_DATE" ] || REPORT_DATE="$(date -u +%Y-%m-%d)"
REPORT_DIR="${TUBER_X_REPORT_DIR:-$PROJECT/reports}"
REPORT_FILE="$REPORT_DIR/$REPORT_DATE.md"

STDOUT_TMP="$(mktemp)"
ERR_TMP="$(mktemp)"
trap 'rm -f "$STDOUT_TMP" "$ERR_TMP"' EXIT

# Режим прогона (Р1.5): явная дата — с записью файла; без даты и файл уже есть —
# только печать; без даты и файла нет — обычный прогон, файл создаётся.
MODE=write
ARGS=(report)
if [ -n "$DATE" ]; then
  ARGS=(report --date "$DATE")
elif [ -f "$REPORT_FILE" ]; then
  MODE=stdout-only
  ARGS=(report --stdout-only)
fi

# Р1.3: неуспех обязан быть объявлен в stdout. Код возврата — 0.
fail() {
  printf 'ALERT Tuber-x: отчёт за %s не собран: %s\n' "$REPORT_DATE" "$1"
  exit 0
}

# Рабочая база: без неё отчёта нет, а `store.connect` МОЛЧА создаёт пустую базу по
# указанному пути (и каталоги к ней). Тогда прогон «успешен», отчёт состоит из
# «нет данных за сутки», и владелец получает бодрую пустую сводку вместо
# сигнала о сломанной конфигурации. Поэтому путь к базе проверяем ДО запуска:
# файл должен существовать, читаться и содержать ядро схемы.
DB_PATH="$("$TUBER_PYTHON" -c 'from tuber.platforms.x import config; print(config.DB_PATH)' 2>/dev/null)"
if [ -z "$DB_PATH" ] || [ ! -f "$DB_PATH" ] || [ ! -r "$DB_PATH" ]; then
  fail "база данных недоступна: ${DB_PATH:-путь к базе не определён}"
fi
if ! "$TUBER_PYTHON" - "$DB_PATH" <<'PY' >/dev/null 2>&1
import sys

from tuber.platforms.x import store

# ТОЛЬКО через store.connect: legacy-таблицы `posts`/`accounts`/`stories` живут
# в слое совместимости (TEMP-представления над ядром `source`/`content`), голый
# sqlite3.connect их не видит и проверка ложно объявляет базу повреждённой.
con = store.connect(sys.argv[1], readonly=True)
try:
    con.execute("SELECT COUNT(*) FROM posts").fetchone()
    con.execute("SELECT COUNT(*) FROM accounts").fetchone()
    con.execute("SELECT COUNT(*) FROM stories").fetchone()
finally:
    con.close()
PY
then
  fail "база данных недоступна или повреждена: $DB_PATH"
fi

# Р1.4: служебная строка и полный вывод CLI — в журнал, не в stdout (П2).
{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) tuber_x report${DATE:+ --date $DATE} (режим $MODE) ==="
} >>"$LOG"

RC=0
"$TUBER_PYTHON" -m tuber x "${ARGS[@]}" >"$STDOUT_TMP" 2>"$ERR_TMP" || RC=$?

{
  if [ -s "$ERR_TMP" ]; then
    echo "--- stderr ---"
    cat "$ERR_TMP"
  fi
  echo "--- stdout (полный) ---"
  cat "$STDOUT_TMP"
  echo "--- код возврата: $RC, режим: $MODE ---"
} >>"$LOG"

# Р1.3: неуспех обязан быть объявлен в stdout (см. fail выше).
if [ "$RC" -ne 0 ]; then
  if [ "$RC" -eq 4 ]; then
    fail "за сутки нет данных (пустой сбор), код 4"
  fi
  fail "CLI report вернул код $RC"
fi
if [ "$MODE" = "write" ] && [ ! -f "$REPORT_FILE" ]; then
  fail "файл отчёта $REPORT_FILE не создан"
fi
if [ ! -s "$STDOUT_TMP" ]; then
  fail "CLI report не вернул текст отчёта"
fi

# Р1.1/Р1.2: компактная сводка в stdout по границам сюжетов.
SUMMARY_RC=0
"$TUBER_PYTHON" - "$STDOUT_TMP" "$REPORT_DATE" "$REPORT_FILE" "$MODE" <<'PY' || SUMMARY_RC=$?
"""Сводка для владельца из полного текста отчёта (ТЗ-12 Р1.1, Р1.2).

Печатает: заголовок с датой, до 5 сюжетов главного блока (текст, автор,
ссылка) и одной строкой итоговые цифры. Длина ≤ 3500 знаков; при превышении
отбрасываются целые сюжеты (обрезка посреди строки или ссылки запрещена).
Код возврата 1 — сводку собрать не удалось (обёртка объявит ALERT).
"""
import os
import re
import sys

LIMIT = 3500
MAX_STORIES = 5

raw_path, report_date, report_file, mode = sys.argv[1:5]

try:
    with open(raw_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
except OSError:
    sys.exit(1)

# Вывод CLI = текст отчёта; при записи перед ним идёт строка «отчёт сохранён: ...».
head = re.search(r"^# Tuber-x, выдача за (\d{4}-\d{2}-\d{2})\s*$", text, re.M)
if not head:
    sys.stdout.write("")  # обёртка напечатает ALERT: текст отчёта не распознан
    sys.exit(1)
if head.group(1) != report_date:
    sys.exit(1)

lines = text.splitlines()

# --- блок 1: сюжеты главного блока -------------------------------------------
stories = []
inside = False
current = None
for line in lines:
    if re.match(r"^\s*1\.\s+Главное за сутки\s*$", line):
        inside = True
        continue
    if not inside:
        continue
    # Заголовок следующего блока — строка с номером в самом начале (без отступа):
    # строки сюжетов внутри блока 1 отступа имеют, иначе сюжет «2.» обрывал блок.
    if re.match(r"^2\.\s+\S", line):
        break
    m = re.match(r"^\s+\d+\.\s+(.*\S)\s*$", line)
    if m:
        if current:
            stories.append(current)
        current = {"text": m.group(1), "author": None, "link": None}
        continue
    if current is None:
        continue
    m = re.match(r"^\s+Первый:\s+(@[^\s;]+)", line)
    if m:
        current["author"] = m.group(1)
        continue
    m = re.match(r"^\s+Ссылка:\s+(\S+)", line)
    if m:
        current["link"] = m.group(1).rstrip(",;")
if current:
    stories.append(current)

# --- цифры: хвост блока 7 ----------------------------------------------------
def find(pattern, default="н/д"):
    m = re.search(pattern, text)
    return m.group(1) if m else default

fail_rate = find(r"Отказы сбора за сутки:\s*\d+\s*из\s*\d+\s*\(([\d.]+%)\)")
quota = find(r"Расход квоты Nitter за сутки:\s*(\d+\s*запросов\s*\([\d.]+%\s*от потолка\))")
candidates = find(r"Кандидаты:\s*(\d+)")

# Сюжеты за сутки и постов в базе — из рабочей базы (только чтение), тем же
# окном суток и тем же форматом времени, что использует сам отчёт (ТЗ-3).
stories_day = "н/д"
posts_total = "н/д"
influx = None
try:
    from tuber.platforms.x import config, store
    from tuber.platforms.x.report import day_bounds

    start, end = day_bounds(report_date)
    path = config.DB_PATH
    # store.connect: без слоя совместимости `stories`/`posts` не видны, и блок
    # молча уходил в «н/д» (см. тот же класс ошибки в проверке выше).
    con = store.connect(path, readonly=True)
    try:
        stories_day = con.execute(
            "SELECT COUNT(*) FROM stories WHERE published_at >= ? AND published_at < ?",
            (store.iso(start), store.iso(end))).fetchone()[0]
        posts_total = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        # ТЗ-18: приток из фидов (описаний YouTube / постов Telegram). Только
        # чтение; на недо-мигрированной базе честно отдаёт нули.
        from tuber.platforms.x import feeds as _feeds
        influx = _feeds.feed_influx(con)
    finally:
        con.close()
except Exception:
    pass

metrics = (
    f"Сюжетов за сутки: {stories_day}. Постов в базе: {posts_total}. "
    f"Отказы сбора: {fail_rate}. Квота Nitter: {quota}. "
    f"Кандидатов в реестре: {candidates}. Полный файл: {report_file}"
)

header = f"Tuber-x, сводка за {report_date}"
if stories:
    body_head = "Главное за сутки:"
else:
    body_head = "Главное за сутки: нет данных за сутки"

shown = stories[:MAX_STORIES]

# --- сборка с бюджетом по границам сюжетов (Р1.2) ----------------------------
def plural(n, one, few, many):
    if 11 <= n % 100 <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


# --- ТЗ-18: одна строка о притоке из фидов --------------------------------
# Владелец читает её без пояснений: откуда пришли кандидаты, сколько всего в
# очереди и за сколько суток очередь разберётся при текущем суточном бюджете.
if influx is not None:
    _d = influx["horizon_days"]
    metrics += (
        f"\nПриток из фидов: описания YouTube {influx['yt_desc']},"
        f" посты Telegram {influx['tg_posts']}."
        f" Всего в очереди: {influx['queue_total']}."
        f" Проверка очереди: {influx['queue_total']} кандидатов при"
        f" {influx['requests_per_day']} запросах/сутки и {influx['checks_per_day']}"
        f" проверках/сутки — закроется за {_d} {plural(_d, 'сутки', 'суток', 'суток')}."
    )


def render(items, hidden):
    parts = [header, body_head]
    for i, s in enumerate(items, 1):
        block = [f"{i}. {s['text']}"]
        if s["author"]:
            block.append(f"   автор: {s['author']}")
        if s["link"]:
            block.append(f"   ссылка: {s['link']}")
        parts.append("\n".join(block))
    if hidden:
        parts.append(f"... ещё {hidden} {plural(hidden, 'сюжет', 'сюжета', 'сюжетов')}"
                     f" — в полном файле")
    parts.append(metrics)
    return "\n".join(parts)


while True:
    hidden = min(len(stories), MAX_STORIES) - len(shown)
    if hidden < 0:
        hidden = 0
    out = render(shown, hidden)
    if len(out) <= LIMIT or not shown:
        break
    shown = shown[:-1]

sys.stdout.write(out + "\n")
PY

if [ "$SUMMARY_RC" -ne 0 ]; then
  fail "вывод отчёта за дату не распознан (сводку собрать не удалось)"
fi

exit 0
