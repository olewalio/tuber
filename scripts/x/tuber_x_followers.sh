#!/usr/bin/env bash
# Tuber-x: суточные снимки подписчиков X на сессии (ТЗ-44B, пейджинг ТЗ-44C).
#
# Зачем: `python3 -m tuber x followers` умеет снять подписчиков через сессию X
# (ТЗ-44), но в расписании этого задания не было — ряд роста не наполнялся сам.
# Эта обёртка ставит сбор подписчиков на суточный прогон.
#
# Что делает:
#   * считает реестр так же, как CLI (`status IN
#     ('active','provisional','candidate')`) и ОСТАТОК по фактическому состоянию
#     реестра (`subs_at IS NULL`), а не по счётчику попыток: CLI обходит реестр
#     по `subs_at ASC`, поэтому повторный вызов сам продолжит с места (ТЗ-44C);
#   * берёт бюджет сессии X из `transport_request` (`store.session_budget`) и
#     назначает `--limit` = min(остаток реестра, остаток суточного бюджета,
#     остаток окна, жёсткий потолок);
#   * обходит реестр ПОРЦИЯМИ по остатку окна (50 запросов / 900 с): транспорт
#     САМ отказывает при исчерпании окна. Пока остаток реестра не ноль, обёртка
#     ждёт освобождения окна (сон до времени из строки CLI «продолжение после
#     … UTC» или до `window_free_at` из плана) и зовёт CLI снова (ТЗ-44C);
#   * потолок времени прогона — `RUN_TIME_CAP_MIN` минут (по умолчанию 100):
#     дольше не крутимся, даже если реестр не обойдён (ТЗ-44C);
#   * в конце пишет ОДНУ строку со сводкой обновлено/отложено/отказов/времени;
#   * при норме МОЛЧИТ (весь вывод — в журнал /root/.hermes/logs/tuber_x_followers.log);
#   * при отказе/429/блокировке печатает ОДНУ понятную строку ALERT по-русски
#     (её доставит планировщик);
#   * при исчерпании суточного бюджета пишет в журнал `skipped: budget
#     exhausted` и молчит (это не отказ).
#
# Всегда завершается кодом 0: сбой прогона — забота сторожа, а не уведомления
# крона (ТЗ-7).
#
# Переопределения (тесты/приёмка): TUBER_X_PROJECT, TUBER_X_LOG_DIR,
# TUBER_PYTHON, TUBER_DB (копия базы), TUBER_X_SESSION_DAILY_CAP (суточный
# бюджет), TUBER_X_FOLLOWERS_LIMIT (жёсткий потолок обхода за прогон),
# TUBER_X_FOLLOWERS_TIME_CAP_MIN (минуты, потолок времени прогона) и
# TUBER_X_FOLLOWERS_TIME_CAP_SEC (секунды, перебивает минуты — для тестов).
# TUBER_LAUNCHER_DRYRUN=1 — только напечатать план, в сеть не ходить.
set -uo pipefail

# Интерпретатор контура — ЯВНО, не по PATH (ТЗ-3c): в cron PATH урезан, и
# «голый» python3 уходит на системный SQLite. Переопределяется TUBER_PYTHON.
TUBER_PYTHON="${TUBER_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python3}"
if [ ! -x "$TUBER_PYTHON" ]; then
  echo "ошибка: интерпретатор контура не найден: $TUBER_PYTHON" >&2
  echo "  задайте TUBER_PYTHON=/путь/к/python3 (SQLite >= 3.24)" >&2
  exit 4
fi

PROJECT="${TUBER_X_PROJECT:-/root/tuber}"
LOG_DIR="${TUBER_X_LOG_DIR:-/root/.hermes/logs}"
LOG="$LOG_DIR/tuber_x_followers.log"

# Суточный бюджет сессии задаётся средой; по умолчанию — config.X_SESSION_DAILY_CAP.
if [ -n "${TUBER_X_SESSION_DAILY_CAP:-}" ]; then
  export TUBER_X_SESSION_DAILY_CAP
fi
# Жёсткий потолок обхода за прогон (0/пусто = не ограничивать сверх бюджета).
FOLLOWERS_LIMIT="${TUBER_X_FOLLOWERS_LIMIT:-0}"
# Потолок времени прогона (ТЗ-44C): по умолчанию 100 минут.
RUN_TIME_CAP_MIN="${TUBER_X_FOLLOWERS_TIME_CAP_MIN:-100}"
RUN_TIME_CAP_SEC="${TUBER_X_FOLLOWERS_TIME_CAP_SEC:-$((RUN_TIME_CAP_MIN * 60))}"
case "$RUN_TIME_CAP_SEC" in
  ''|*[!0-9]*) RUN_TIME_CAP_SEC=$((RUN_TIME_CAP_MIN * 60)) ;;
esac

cd "$PROJECT" || { printf 'ALERT: подписчики X: нет каталога %s\n' "$PROJECT"; exit 0; }
mkdir -p "$LOG_DIR"

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
now_epoch() { date -u +%s; }

# План прогона одной строкой на поле (только чтение БД, без миграций):
#   total daily_used daily_cap window_used window_cap window_free cooldown blocked pending
# pending — остаток по фактическому состоянию реестра (`subs_at IS NULL`).
plan() {
  "$TUBER_PYTHON" - <<'PY' 2>>"$LOG"
from tuber.platforms.x import config, store
con = store.connect(config.DB_PATH, readonly=True)
try:
    total = con.execute(
        "SELECT COUNT(*) FROM source WHERE platform='x'"
        " AND status IN ('active','provisional','candidate')").fetchone()[0]
    pending = con.execute(
        "SELECT COUNT(*) FROM source WHERE platform='x'"
        " AND status IN ('active','provisional','candidate')"
        " AND subs_at IS NULL").fetchone()[0]
    b = store.session_budget(con)
    st = store.read_session_state(con)
    cd = st.get("cooldown_until")
    print(total)
    print(pending)
    print(b["daily_count"])
    print(b["daily_cap"])
    print(b["window_count"])
    print(b["window_cap"])
    print(b["window_free_at"] or "")
    print(store.iso(cd) if cd is not None else "")
    print(1 if st.get("blocked") else 0)
finally:
    con.close()
PY
}

# Сколько секунд ждать освобождения окна. Не больше остатка бюджета времени
# прогона (ТЗ-44C), не больше 960 с, чтобы не залипнуть.
sleep_until_window() {
  local free="$1" target now wait left
  now="$(now_epoch)"
  left=$((RUN_TIME_CAP_SEC - (now - START_EPOCH)))
  [ "$left" -lt 1 ] && return 0
  if [ -z "$free" ]; then wait=20; else
    target="$(date -u -d "$free" +%s 2>/dev/null)" || target=""
    if [ -z "$target" ]; then wait=20; else wait=$((target - now + 3)); fi
  fi
  [ "$wait" -lt 1 ] && wait=1
  [ "$wait" -gt 960 ] && wait=960
  [ "$wait" -gt "$left" ] && wait="$left"
  sleep "$wait"
}

# Сон до HH:MM:SS UTC из строки CLI «продолжение после … UTC» (ТЗ-44C).
sleep_until_hhmmss() {
  local hhmmss="$1" now target left
  now="$(now_epoch)"
  left=$((RUN_TIME_CAP_SEC - (now - START_EPOCH)))
  [ "$left" -lt 1 ] && return 0
  target="$(date -u -d "today $hhmmss" +%s 2>/dev/null)" || return 0
  # если время уже прошло — значит это завтра
  [ "$target" -le "$now" ] && target=$((target + 86400))
  local wait=$((target - now + 3))
  [ "$wait" -lt 1 ] && wait=1
  [ "$wait" -gt 960 ] && wait=960
  [ "$wait" -gt "$left" ] && wait="$left"
  sleep "$wait"
}

num() { printf '%s\n' "$1" | sed -n "s/.*$2 \([0-9][0-9]*\).*/\1/p" | tail -n 1; }

START_EPOCH="$(now_epoch)"

mapfile -t P < <(plan)
TOTAL="${P[0]:-}"; PENDING="${P[1]:-}"; DAY_USED="${P[2]:-}"; DAY_CAP="${P[3]:-}"
WIN_USED="${P[4]:-}"; WIN_CAP="${P[5]:-}"; WIN_FREE="${P[6]:-}"
COOLDOWN="${P[7]:-}"; BLOCKED="${P[8]:-}"

if [ -z "$TOTAL" ] || [ -z "$DAY_CAP" ]; then
  printf 'ALERT: подписчики X: не удалось прочитать план из config/БД (см. %s)\n' "$LOG"
  printf '=== %s followers ALERT: план не прочитан\n' "$(stamp)" >>"$LOG"
  exit 0
fi

# Остаток по фактическому состоянию реестра (ТЗ-44C). Если план старого формата
# (нет строки pending) — считаем остатком весь реестр.
[ -z "$PENDING" ] && PENDING="$TOTAL"

# Цель обхода: остаток реестра, но не больше остатка суточного бюджета
# (и жёсткого потолка за прогон).
TARGET=$((DAY_CAP - DAY_USED))
[ "$TARGET" -gt "$PENDING" ] && TARGET="$PENDING"
if [ "$FOLLOWERS_LIMIT" -gt 0 ] && [ "$TARGET" -gt "$FOLLOWERS_LIMIT" ]; then
  TARGET="$FOLLOWERS_LIMIT"
fi

if [ "$TARGET" -le 0 ]; then
  printf '=== %s followers skipped: budget exhausted (%s/%s за 24 ч, реестр %s, остаток %s)\n' \
    "$(stamp)" "$DAY_USED" "$DAY_CAP" "$TOTAL" "$PENDING" >>"$LOG"
  exit 0
fi

if [ "${BLOCKED:-0}" = "1" ]; then
  printf 'ALERT: подписчики X: сессия помечена blocked (401/403), обход пропущен\n'
  printf '=== %s followers ALERT: blocked=1, обход пропущен\n' "$(stamp)" >>"$LOG"
  exit 0
fi

# Тестовый режим: показать план и выйти, сеть не трогать.
if [ "${TUBER_LAUNCHER_DRYRUN:-}" = "1" ]; then
  printf '%s\n' "$TUBER_PYTHON -m tuber x followers --limit $TARGET"
  printf '=== %s followers DRYRUN: target=%s (реестр %s, остаток %s, бюджет %s/%s, окно %s/%s)\n' \
    "$(stamp)" "$TARGET" "$TOTAL" "$PENDING" "$DAY_USED" "$DAY_CAP" "$WIN_USED" "$WIN_CAP" >>"$LOG"
  exit 0
fi

printf '=== %s followers start: target=%s реестр=%s остаток=%s бюджет=%s/%s окно=%s/%s потолок=%ss\n' \
  "$(stamp)" "$TARGET" "$TOTAL" "$PENDING" "$DAY_USED" "$DAY_CAP" "$WIN_USED" "$WIN_CAP" \
  "$RUN_TIME_CAP_SEC" >>"$LOG"

ok_total=0; def_total=0; fail_total=0; chunks=0
hard=0; skip_budget=0; cap_hit=0

while :; do
  elapsed=$(( $(now_epoch) - START_EPOCH ))
  if [ "$elapsed" -ge "$RUN_TIME_CAP_SEC" ]; then cap_hit=1; break; fi

  mapfile -t P < <(plan)
  TOTAL="${P[0]:-$TOTAL}"; PENDING="${P[1]:-$PENDING}"
  DAY_USED="${P[2]:-0}"; DAY_CAP="${P[3]:-$DAY_CAP}"
  WIN_USED="${P[4]:-0}"; WIN_CAP="${P[5]:-$WIN_CAP}"; WIN_FREE="${P[6]:-}"
  COOLDOWN="${P[7]:-}"; BLOCKED="${P[8]:-0}"

  [ "$BLOCKED" = "1" ] && { hard=1; break; }

  remain_day=$((DAY_CAP - DAY_USED))
  remain_win=$((WIN_CAP - WIN_USED))
  remain_goal=$((TARGET - ok_total))
  # Остаток по реестру приоритетнее счётчика: если в реестре ничего не осталось,
  # прогон окончен (ТЗ-44C).
  [ "$PENDING" -le 0 ] && break
  [ "$remain_goal" -gt "$PENDING" ] && remain_goal="$PENDING"
  if [ "$FOLLOWERS_LIMIT" -gt 0 ]; then
    hard_left=$((FOLLOWERS_LIMIT - ok_total))
    [ "$hard_left" -le 0 ] && break
    [ "$remain_goal" -gt "$hard_left" ] && remain_goal="$hard_left"
  fi

  if [ "$remain_day" -le 0 ]; then skip_budget=1; break; fi

  # Окно исчерпано, а бюджет ещё есть — ждём и пробуем снова.
  if [ "$remain_win" -le 0 ]; then sleep_until_window "$WIN_FREE"; continue; fi

  chunk="$remain_win"
  [ "$chunk" -gt "$remain_day" ] && chunk="$remain_day"
  [ "$chunk" -gt "$remain_goal" ] && chunk="$remain_goal"
  [ "$chunk" -le 0 ] && break

  out="$("$TUBER_PYTHON" -m tuber x followers --limit "$chunk" 2>>"$LOG")"; rc=$?
  {
    printf '=== %s followers chunk#%s limit=%s rc=%s\n' "$(stamp)" \
      "$((chunks + 1))" "$chunk" "$rc"
    printf '%s\n' "$out"
  } >>"$LOG"
  chunks=$((chunks + 1))

  ok="$(num "$out" 'обновлено')"; df="$(num "$out" 'отложено')"; fl="$(num "$out" 'отказов')"
  ok="${ok:-0}"; df="${df:-0}"; fl="${fl:-0}"
  ok_total=$((ok_total + ok)); def_total=$((def_total + df)); fail_total=$((fail_total + fl))

  case "$out" in
    *Traceback*|*"сессия недоступна"*) hard=1; break ;;
  esac

  if [ "$df" -gt 0 ]; then
    # Бюджет: суточный — по плану видно (remain_day), оконный — ждём окно.
    cont="$(printf '%s\n' "$out" \
      | sed -n 's/.*продолжение после \([0-9][0-9]:[0-9][0-9]:[0-9][0-9]\) UTC.*/\1/p' \
      | tail -n 1)"
    if [ -n "$cont" ]; then sleep_until_hhmmss "$cont"; else sleep_until_window "$WIN_FREE"; fi
    continue
  fi

  # Настоящий отказ транспорта: разбираем причину по состоянию в БД.
  case "$out" in
    *"отказ транспорта"*|*XSessionRateLimited*|*SessionBlocked*|*XSessionError*)
      if [ "$BLOCKED" = "1" ] || [ -n "$COOLDOWN" ]; then hard=1; break; fi
      if [ "$remain_day" -le 0 ]; then skip_budget=1; break; fi
      if [ "$remain_win" -le 0 ]; then sleep_until_window "$WIN_FREE"; continue; fi
      hard=1; break ;;
  esac

  # Нет прироста — дальше крутить нечего (аккаунты без профиля не заполняются).
  if [ "$ok" -eq 0 ]; then break; fi
done

elapsed=$(( $(now_epoch) - START_EPOCH ))

# Отложено на конец прогона — по фактическому состоянию реестра (ТЗ-44C).
mapfile -t PF < <(plan)
final_pending="${PF[1]:-$def_total}"
[ -z "$final_pending" ] && final_pending="$def_total"

done_suffix=""
[ "$skip_budget" = "1" ] && done_suffix="$done_suffix (бюджет исчерпан)"
[ "$cap_hit" = "1" ] && done_suffix="$done_suffix (потолок времени)"
printf '=== %s followers done: обновлено=%s отложено=%s отказов=%s порций=%s время=%ss%s\n' \
  "$(stamp)" "$ok_total" "$final_pending" "$fail_total" "$chunks" "$elapsed" \
  "$done_suffix" >>"$LOG"

# ALERT важнее сводки: при настоящем отказе печатаем только его (ТЗ-7).
if [ "$hard" = "1" ]; then
  if [ -n "$COOLDOWN" ] || [ "$BLOCKED" = "1" ]; then
    printf 'ALERT: подписчики X: сессия в cooldown/blocked (429/401/403), обход остановлен, см. %s\n' "$LOG"
  else
    printf 'ALERT: подписчики X: прогон остановлен отказом транспорта, см. %s\n' "$LOG"
  fi
elif [ "$cap_hit" = "1" ] || [ "$def_total" -gt 0 ] \
     || { [ "$skip_budget" = "1" ] && [ "$chunks" -gt 0 ]; }; then
  # Одна сводка наружу, когда прогон неполный (норма — молчание).
  printf 'подписчики X: обновлено %s, отложено %s, отказов %s, время %ss%s\n' \
    "$ok_total" "$final_pending" "$fail_total" "$elapsed" \
    "$([ "$cap_hit" = "1" ] && printf ' (потолок времени)' || true)"
fi

exit 0
