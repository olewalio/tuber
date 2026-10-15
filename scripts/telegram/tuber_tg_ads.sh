#!/usr/bin/env bash
# Tuber-Telegram: семантический детектор рекламы (D-37, P1-4).
#
# Что делает. Один прогон `python3 -m tuber tg ads --limit 200
# --allow-production`: DeepSeek размечает посты на «реклама/не реклама», пишет
# признак в кэш `ad_cache` (повторные прогоны по тому же тексту бесплатны).
#
# Ограничитель `--limit 200` ОБЯЗАТЕЛЕН. Без него команда берёт ВСЮ очередь
# (разовый `ads --dry-run` показывал 54 321 пост) и расход DeepSeek становится
# непредсказуемым (~$0,003 за вызов). 200 постов за ночной прогон — управляемый
# кусок; кэш делает прогресс кумулятивным, очередь тает за несколько суток.
#
# Ключ модели берётся ТОЛЬКО из окружения. Если `DEEPSEEK_API_KEY` не задан,
# аккуратно подгружаем его из /root/.hermes/.env, НЕ печатая значение. Без ключа
# CLI печатает ALERT и завершается кодом 0.
#
# Тихая при норме: печатает строку только при реальном сбое. Всегда код 0.
#
# Тестовые хуки (без сети и без боевой базы):
#   TUBER_LAUNCHER_DRYRUN=1        — план, CLI не зовётся;
#   TUBER_TG_ADS_CRON_LOG=<путь>   — журнал вместо $LOG_DIR/...;
#   TUBER_DB=<путь>                — копия базы (ads читает config.resolve_db(),
#                                    т.е. переменную окружения, а не --db);
#   TUBER_TG_ADS_LIMIT=<n>         — потолок прогона (по умолчанию 200).
set -uo pipefail

TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_TG_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_TG_LOG_DIR:-/root/.hermes/logs}"
LOG="${TUBER_TG_ADS_CRON_LOG:-$LOG_DIR/tuber_tg_ads.log}"
LIMIT="${TUBER_TG_ADS_LIMIT:-200}"
ENV_FILE=/root/.hermes/.env
mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 0

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Копия базы: ads не принимает --db, читает config.resolve_db() (env TUBER_DB).
if [ -n "${TUBER_DB:-}" ]; then export TUBER_DB; fi

# Ключ модели — из окружения; при отсутствии грузим из приватного .env.
# Значение нигде не печатаем (ни в stdout, ни в журнал).
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

# Аргументы CLI. `--limit` обязателен; `--allow-production` — обёртка и есть
# боевой планировщик (гейт `production_gate_error` иначе откажет писать).
cli_args() {
  printf -- '--limit %s --allow-production' "$LIMIT"
}

if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf '%s -m tuber tg ads %s\n' "$TUBER_PYTHON" "$(cli_args)"
  exit 0
fi

out="$("$TUBER_PYTHON" -m tuber tg ads $(cli_args) 2>>"$LOG" | tail -1)"
rc=$?
echo "$(stamp) tg ads $out" >>"$LOG"

# Прогон не вернул сводку (Traceback / обрыв / timeout) — единственный повод
# для ALERT. Строка «ALERT: нет ключа …» от CLI проходит как норма (rc=0).
if [ "$rc" != "0" ] || [ -z "$out" ]; then
  printf 'ALERT: Tuber-Telegram: детектор рекламы не вернул итог '\
'— см. %s\n' "$LOG"
fi

exit 0
