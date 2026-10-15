#!/usr/bin/env bash
# Tuber-Telegram: ВОЗОБНОВЛЯЕМЫЙ добор форвардов через MTProto (D-34, ТЗ-B12b).
#
# Зачем. Волна ТЗ-B12 читала `Message.forwards` через MTProto, но прогон
# упирался в FloodWait и останавливался. Отбор каналов сделан возобновляемым
# (`select_priority_channels` пропускает каналы с уже измеренными форвардами),
# поэтому эту обёртку ставят в расписание: каждый прогон продолжает с
# необработанных каналов, а не начинает те же с начала.
#
# Умеренный потолок. За прогон — не более 200 запросов MTProto
# (`TUBER_TG_FORWARDS_RESUME_REQUESTS`, по умолчанию 200). Это меньше боевого
# потолка 1500, чтобы ночное окно флуда не раздувалось, а квота расходовалась
# постепенно.
#
# Окно флуда. Если в `transport_account_state` записан `flood_until` в будущем,
# обёртка НЕ делает ни одного запроса (повторный запрос продлевает бан) — тихо
# (строка только в журнал), код 0.
#
# Тихая при норме: печатает строку ALERT только при реальном сбое (пусто/не
# разобран итог/ненулевой код). Всегда завершается кодом 0 — сбой не повод
# красить задание планировщика (урок ТЗ-7).
#
# Тестовые хуки (без сети и без боевой базы):
#   TUBER_LAUNCHER_DRYRUN=1       — печатает план, CLI не зовётся;
#   TUBER_TG_FORWARDS_CRON_LOG=<путь> — журнал вместо $LOG_DIR/...;
#   TUBER_DB=<путь>               — копия базы (пробрасывается как --db);
#   TUBER_TG_FORWARDS_RESUME_REQUESTS=<n> — потолок запросов (по умолчанию 200).
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_TG_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_TG_LOG_DIR:-/root/.hermes/logs}"
LOG="${TUBER_TG_FORWARDS_CRON_LOG:-$LOG_DIR/tuber_tg_forwards_resume.log}"
REQUESTS="${TUBER_TG_FORWARDS_RESUME_REQUESTS:-200}"
mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 0

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Аргументы CLI: --allow-production всегда (обёртка — боевой планировщик);
# --db для копии; --max-requests с потолком прогона.
cli_args() {
  printf -- '--allow-production'
  printf -- ' --max-requests %s' "$REQUESTS"
  if [ -n "${TUBER_DB:-}" ]; then printf -- ' --db %s' "$TUBER_DB"; fi
}

# ---------------------------------------------------------------------------
# Окно флуда (D-34, ТЗ-B12e).
# Читаем ВСЕ строки transport_account_state с platform='telegram' (любой kind
# состояния — имя хранится в колонке name) и берём максимальный flood_until.
# Активно только время в БУДУЩЕМ. Формат времени приходит в разных видах
# ('2026-09-24T05:31:44', '...Z', с пробелом вместо 'T') — разбираем все.
# Печатаем в журнал, ГДЕ взято состояние (flood/kind/источник), чтобы отсутствие
# строки было видно сразу, а не выглядело как «предохранитель сработал».
# Если прочитать НЕ удалось (нет БД/таблицы) — строка flood_gate=unknown, но не
# чаще одного прогона подряд (не спамить); прогон при этом продолжается.
# ---------------------------------------------------------------------------
DB="${TUBER_DB:-$PROJECT/data/tuber.db}"

gate_read() {
  "$TUBER_PYTHON" - "$DB" <<'PY' 2>/dev/null
import sqlite3, sys
from datetime import datetime, timezone

path = sys.argv[1]


def parse(value):
    """Разобрать flood_until: T/пробел/Z/наивное UTC. None — не разобрано."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    cand = text
    if cand.endswith("Z") or cand.endswith("z"):        # ...Z -> +00:00
        cand = cand[:-1] + "+00:00"
    elif " " in cand and "T" not in cand:               # пробел вместо T
        cand = cand.replace(" ", "T", 1)
    dt = None
    try:
        dt = datetime.fromisoformat(cand)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


try:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT name, flood_until FROM transport_account_state "
        "WHERE platform='telegram'").fetchall()
    con.close()
except Exception:
    print("UNKNOWN")
    sys.exit(0)

now = datetime.now(timezone.utc)
best = None  # (datetime, исходный_текст, kind)
for name, until in rows:
    dt = parse(until)
    if dt is not None and dt > now:
        if best is None or dt > best[0]:
            best = (dt, str(until).strip(), str(name or ""))
if best is not None:
    print("ACTIVE\t%s\t%s" % (best[1], best[2]))
else:
    print("CLEAR")
PY
}

gate="$(gate_read)"
case "$gate" in
  ACTIVE*)
    rest="${gate#*$'\t'}"
    flood_until="${rest%%$'\t'*}"
    flood_kind="${rest#*$'\t'}"
    echo "$(stamp) flood_gate=active flood=$flood_until kind=$flood_kind источник=$DB" >>"$LOG"
    echo "$(stamp) skip: окно флуда активно до $flood_until (запросов не делаем)" >>"$LOG"
    exit 0
    ;;
  CLEAR)
    echo "$(stamp) flood_gate=clear источник=$DB" >>"$LOG"
    ;;
  *)
    # Не прочитали состояние (нет БД/таблицы/ошибка). Не молчим, но не спамим:
    # пишем flood_gate=unknown, только если прошлый раз было НЕ unknown.
    last_gate="$(grep -o 'flood_gate=[a-z]*' "$LOG" 2>/dev/null | tail -1)"
    if [ "$last_gate" != "flood_gate=unknown" ]; then
      echo "$(stamp) flood_gate=unknown источник=$DB" >>"$LOG"
    fi
    ;;
esac

if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf '%s -m tuber tg scoring forwards-mtproto %s\n' "$TUBER_PYTHON" "$(cli_args)"
  exit 0
fi

out="$("$TUBER_PYTHON" -m tuber tg scoring forwards-mtproto $(cli_args) --json 2>>"$LOG" | tail -1)"
rc=$?
echo "$(stamp) forwards-mtproto $out" >>"$LOG"

# Разбор итога: rc=0 И строка содержит ключ forwards_mtproto (непустой JSON).
if [ "$rc" != "0" ] || [ -z "$out" ] \
   || ! printf '%s' "$out" | grep -q '"forwards_mtproto"'; then
  printf 'ALERT: Tuber-Telegram: возобновляемый добор форвардов не вернул итог '\
'— см. /root/.hermes/logs/tuber_tg_forwards_resume.log\n'
fi

exit 0
