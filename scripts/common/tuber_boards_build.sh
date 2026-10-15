#!/usr/bin/env bash
# Tuber: пересборка страницы «Что залетело» (борды виральности X/YouTube/Telegram,
# разделы ТЗ-4 и графики) и выкладка её под nginx для 4 окон (ТЗ-12/ТЗ-13).
#
# Зачем: витрина «Что залетело» (tools/build_boards.py) читает боевую базу ТОЛЬКО
# на чтение и собирает три доски, разделы «Темы окна» / «ИИ-инструменты» /
# «Залетевшие сюжеты» и графики (tools/build_charts.py) для окон 1/3/5/10 дней.
#
# Что делает (каждые 3 часа, МСК):
#   * для каждого окна d из PERIODS (1 3 5 10):
#       - `tools/build_boards.py --days $d --limit $LIMIT --out reports/boards/${d}d/index.html`;
#       - `tools/build_charts.py --days $d --out reports/boards/${d}d` — три PNG;
#       - проверяет непустоту (адаптивный порог карточек 9*d, секций >=3, панелей >=4);
#       - выкладывает index.html и три PNG в /var/www/boards/${d}d/;
#   * после цикла копирует окно 10 дней в корень /var/www/boards/ (обратная совместимость);
#   * при успехе МОЛЧИТ (stdout пуст, exit 0);
#   * при сбое печатает ОДНУ строку по-русски и выходит с кодом 1 (алерт
#     планировщика), не выкладывая битую страницу.
#
# Переопределения (тесты/приёмка): TUBER_BOARDS_PROJECT, TUBER_BOARDS_OUT,
# TUBER_BOARDS_LOG, TUBER_PYTHON, TUBER_BOARDS_LIMIT.
# TUBER_LAUNCHER_DRYRUN=1 — печатать план (4 итерации) и выйти 0.
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
# Размер выдачи каждого раздела (доски и панели): 50 позиций по ТЗ-11.
LIMIT="${TUBER_BOARDS_LIMIT:-50}"
PROJECT="${TUBER_BOARDS_PROJECT:-/root/tuber}"
# Корневая страница (совместимость): 10 дней. Веб-каталог = её каталог.
OUT="${TUBER_BOARDS_OUT:-/var/www/boards/index.html}"
PUB="$(dirname "$OUT")"
LOG="${TUBER_BOARDS_LOG:-/root/.hermes/logs/tuber_boards_build.log}"
# Окна отчёта (дни) по ТЗ-12/ТЗ-13.
PERIODS="1 3 5 10"
# Графики ТЗ-4 §4: собираются в reports/boards/<окно>/ и кладутся рядом.
CHARTS="topics_daily.png topics_bar.png platform_daily.png"
NCHARTS=$(echo $CHARTS | wc -w)

if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  for d in $PERIODS; do
    echo "ПЛАН (борды): python3 tools/build_boards.py --days $d --limit $LIMIT" \
         "--out reports/boards/${d}d/index.html && python3 tools/build_charts.py" \
         "--days $d --out reports/boards/${d}d && install -m 644 ... $CHARTS" \
         "в ${PUB}/${d}d/"
  done
  echo "ПЛАН (борды): после цикла reports/boards/10d/index.html + $CHARTS -> ${PUB}/"
  exit 0
fi

if [ ! -x "$TUBER_PYTHON" ]; then
  echo "БОРДЫ: интерпретатор контура не найден: $TUBER_PYTHON"
  exit 1
fi

cd "$PROJECT" || { echo "БОРДЫ: нет каталога $PROJECT"; exit 1; }

for d in $PERIODS; do
  OUT_DIR="reports/boards/${d}d"
  mkdir -p "$OUT_DIR"

  if ! "$TUBER_PYTHON" tools/build_boards.py --days "$d" --limit "$LIMIT" \
          --out "$OUT_DIR/index.html" >>"$LOG" 2>&1; then
    echo "БОРДЫ НЕ СОБРАЛИСЬ ($d дн): $(tail -5 "$LOG" | tr '\n' ' ')"
    exit 1
  fi

  if ! "$TUBER_PYTHON" tools/build_charts.py --days "$d" \
          --out "$OUT_DIR" >>"$LOG" 2>&1; then
    echo "ГРАФИКИ НЕ СОБРАЛИСЬ ($d дн): $(tail -5 "$LOG" | tr '\n' ' ')"
    exit 1
  fi

  CARDS=$(grep -o 'class="card"' "$OUT_DIR/index.html" | wc -l)
  SECTIONS=$(grep -o 'class="board__title"' "$OUT_DIR/index.html" | wc -l)
  PANELS=$(grep -o 'class="panel__title"' "$OUT_DIR/index.html" | wc -l)
  # Адаптивный порог по ТЗ-12: 9 карточек на день окна (1д→9, 3д→27, 5д→45,
  # 10д→90). Телеграм физически даёт меньше 50, поэтому порог, а не 3*LIMIT.
  MIN_CARDS=$(( 9 * d ))
  if [ "$CARDS" -lt "$MIN_CARDS" ] || [ "$SECTIONS" -lt 3 ] || [ "$PANELS" -lt 4 ]; then
    echo "БОРДЫ ПУСТЫЕ ($d дн): секций $SECTIONS, панелей $PANELS, карточек $CARDS (порог $MIN_CARDS) — страница не выложена"
    exit 1
  fi

  for f in $CHARTS; do
    if [ ! -s "$OUT_DIR/$f" ]; then
      echo "ГРАФИКИ ПУСТЫЕ ($d дн): нет $f — страница не выложена"
      exit 1
    fi
  done

  mkdir -p "${PUB}/${d}d" || { echo "БОРДЫ: не создали ${PUB}/${d}d"; exit 1; }
  install -m 644 "$OUT_DIR/index.html" "${PUB}/${d}d/" \
    || { echo "БОРДЫ: не выложились в ${PUB}/${d}d"; exit 1; }
  for f in $CHARTS; do
    install -m 644 "$OUT_DIR/$f" "${PUB}/${d}d/" \
      || { echo "БОРДЫ: не выложился $f (${d}d)"; exit 1; }
  done

  echo "$(date -u '+%Y-%m-%d %H:%M UTC') ок: $d дн, секций $SECTIONS, панелей $PANELS, карточек $CARDS, графиков $NCHARTS" >>"$LOG"
done

# Корневой /boards/ = окно 10 дней (обратная совместимость).
mkdir -p "$PUB" || { echo "БОРДЫ: не создали $PUB"; exit 1; }
install -m 644 "reports/boards/10d/index.html" "$PUB/" \
  || { echo "БОРДЫ: не выложился корневой index.html"; exit 1; }
for f in $CHARTS; do
  install -m 644 "reports/boards/10d/$f" "$PUB/" \
    || { echo "БОРДЫ: не выложился корневой $f"; exit 1; }
done

exit 0
