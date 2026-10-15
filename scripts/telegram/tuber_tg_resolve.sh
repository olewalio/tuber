#!/usr/bin/env bash
# Tuber-Telegram: резолв закрытых/нечитаемых каналов через MTProto (P1-1).
#
# Зачем. 586 каналов со `status='private'`, `read_mode='unreadable'` (лента
# `t.me/s/` отдала заглушку) и пустым `tg_id` не получают ни точного id, ни
# подписчиков. Режим `resolve` берёт каналы с `tg_id IS NULL` и через MTProto
# (`get_entity` + `GetFullChannelRequest`) заполняет `tg_id`/`subs`/`title`.
# Раньше режим не запускался НИ РАЗУ: `collect.modes()` добавляла `resolve` в
# список режимов только при явном `--resolve N`, а расписание всегда звало
# `--mode web`.
#
# Темп. За прогон — не более `RESOLVE_MAX_PER_RUN=20` запросов (внутри CLI пауза
# 3-5 с между ними). Суточный потолок — `RESOLVE_MAX_PER_DAY=200`
# (`account_state.resolves_today`). Расписание `25 8-23/2` даёт 8 прогонов по 20 =
# 160 каналов/сутки: 586 каналов проходятся за ~4 суток.
#
# Окно флуда. Если в `account_state.flood_until` записан будущий момент, CLI НЕ
# делает ни одного запроса (`resolve_gate`), печатает итог с `flood_until`.
# Медленнее — не страшнее: повторный запрос в окне только продлит бан.
#
# Тихая при норме: печатает строку только при реальном сбое. Всегда код 0 —
# сбой прогона не повод красить задание планировщика (ТЗ-7).
#
# Тестовые хуки (без сети и без боевой базы):
#   TUBER_LAUNCHER_DRYRUN=1            — план, CLI не зовётся;
#   TUBER_TG_RESOLVE_CRON_LOG=<путь>   — журнал вместо $LOG_DIR/...;
#   TUBER_DB=<путь>                    — копия базы (пробрасывается как --db);
#   TUBER_TG_RESOLVE_N=<n>             — потолок прогона (по умолчанию 20).
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_TG_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_TG_LOG_DIR:-/root/.hermes/logs}"
LOG="${TUBER_TG_RESOLVE_CRON_LOG:-$LOG_DIR/tuber_tg_resolve.log}"
RESOLVE_N="${TUBER_TG_RESOLVE_N:-20}"
mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 0

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Аргументы CLI. `--mode resolve` + явный `--resolve N` — именно этого требует
# `modes()`: без `--resolve` режим resolve не включится. `--db` только для копии.
cli_args() {
  printf -- '--mode resolve --resolve %s' "$RESOLVE_N"
  if [ -n "${TUBER_DB:-}" ]; then printf -- ' --db %s' "$TUBER_DB"; fi
}

if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf '%s -m tuber tg collect %s\n' "$TUBER_PYTHON" "$(cli_args)"
  exit 0
fi

out="$("$TUBER_PYTHON" -m tuber tg collect $(cli_args) 2>>"$LOG" | tail -1)"
rc=$?
echo "$(stamp) tg resolve $out" >>"$LOG"

# Разбор итога: rc=0 И непустой JSON со ключом "mode" (collect печатает сводку).
if [ "$rc" != "0" ] || [ -z "$out" ] \
   || ! printf '%s' "$out" | grep -q '"mode"'; then
  printf 'ALERT: Tuber-Telegram: резолв закрытых каналов не вернул итог '\
'— см. %s\n' "$LOG"
fi

exit 0
