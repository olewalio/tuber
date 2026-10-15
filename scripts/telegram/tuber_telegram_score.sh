#!/usr/bin/env bash
# Tuber-Telegram: пересчёт баз каналов и скоринга ПОСЛЕ сбора (D-42).
#
# Зачем: `channel_baselines`/`scores` считались только вручную (`tuber tg
# scoring`), поэтому после сбора значимость не пересчитывалась сама. Обёртка
# запускает пересчёт сразу после сбора и объявляет запись в боевую базу
# (`--allow-production`) — она и есть боевой планировщик (урок ТЗ-45F: без
# флага боевой прогон молча отказывает писать).
#
# По умолчанию — ИНКРЕМЕНТ (D-42): пересчитываются только каналы с новыми
# постами и новые/неоценённые посты. Полный пересчёт — `TG_SCORE_FULL=1`
# (флаг `--full`).
#
# Тихая при норме: печатает строку ALERT только при реальном сбое шага (пустой
# или неразобранный итог). Всегда завершается кодом 0: сбой — не повод красить
# задание планировщика (ТЗ-7).
#
# Тестовые хуки (без сети и без боевого пересчёта):
#   TG_SCORE_TEST_JSON=<json>  — готовый итог ОБОИХ шагов (не запускать CLI);
#   TG_SCORE_CRON_LOG=<путь>   — журнал вместо $LOG_DIR/tuber_telegram_score.log;
#   TG_SCORE_DB=<путь>         — копия базы (пробрасывается в CLI как --db).
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_TG_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_TG_LOG_DIR:-/root/.hermes/logs}"
LOG="${TG_SCORE_CRON_LOG:-$LOG_DIR/tuber_telegram_score.log}"
mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 0

# Аргументы CLI: --allow-production объявляем ВСЕГДА (обёртка — боевой
# планировщик). Копии (TG_SCORE_DB) флаг не мешает: гейт срабатывает только на
# боевой базе. Полный пересчёт — по TG_SCORE_FULL.
cli_args() {
  printf -- '--allow-production'
  if [ -n "${TG_SCORE_DB:-}" ]; then printf -- ' --db %s' "$TG_SCORE_DB"; fi
  if [ -n "${TG_SCORE_FULL:-}" ]; then printf -- ' --full'; fi
}

if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf '%s -m tuber tg scoring baselines %s\n' "$TUBER_PYTHON" "$(cli_args)"
  printf '%s -m tuber tg scoring scores %s\n' "$TUBER_PYTHON" "$(cli_args)"
  exit 0
fi

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

run_step() {
  local cmd="$1"
  if [ "${TG_SCORE_TEST_JSON+set}" = set ]; then
    printf '%s\n' "$TG_SCORE_TEST_JSON"
    return 0
  fi
  # shellcheck disable=SC2086
  timeout 900 "$TUBER_PYTHON" -m tuber tg scoring "$cmd" $(cli_args) --json 2>>"$LOG" | tail -1
}

b_out="$(run_step baselines)"
s_out="$(run_step scores)"
echo "$(stamp) baselines $b_out" >>"$LOG"
echo "$(stamp) scores $s_out" >>"$LOG"

# Разбор итога: обе строки обязаны быть непустым JSON с ожидаемыми ключами.
"$TUBER_PYTHON" - "$b_out" "$s_out" <<'PY'
import json, sys

def parse(raw, key):
    try:
        d = json.loads(raw)
    except Exception:
        return None
    if not isinstance(d, dict) or key not in d:
        return None
    return d

b = parse(sys.argv[1] if len(sys.argv) > 1 else "", "baselines")
s = parse(sys.argv[2] if len(sys.argv) > 2 else "", "scores")
missing = []
if b is None:
    missing.append("baselines")
if s is None:
    missing.append("scores")
if missing:
    print(f"ALERT Tuber-Telegram: пересчёт значимости не вернул итог ({', '.join(missing)}) "
          "— см. /root/.hermes/logs/tuber_telegram_score.log")
PY

exit 0
