"""Сторож Tuber-x (ТЗ-3 Р5 + ТЗ-4 Р6): свежесть, отказы, даты, квоты, реестр,
доставка сводки владельцу (ТЗ-13 Р1, долг D-06).

Запускается каждые 30 минут, ничего не собирает: только читает БД, пишет
события в `run_log` и отдаёт список проверок. Все пороги — в config.

Р5.1: в норме сторож молчит. `python3 health.py` печатает только строки
ALERT (одна строка на проблему, по-русски, без эмодзи) и всегда выходит с кодом
0. Проверки уровня WARN пишутся в `run_log`, но stdout не засоряют.

Р5.2: `python3 health.py --json` (и `cli health --json`) — машинный
вывод для внешних панелей.
"""
from __future__ import annotations

import json as _json
import os as _os
import re as _re
import sys
from datetime import datetime, timedelta, timezone

try:  # обычный импорт пакета
    from . import config, store as db
except ImportError:  # запуск как скрипта: python3 health.py (П6)
    sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(
        _os.path.dirname(_os.path.abspath(__file__))))))
    from tuber.platforms.x import config, store as db


# ------------------------------- ТЗ-13 Р1: факт доставки сводки владельцу (D-06)
# Пути и имена — ПАРАМЕТРЫ МОДУЛЯ с разумными значениями по умолчанию (Р1.2):
# логика их не хардкодит. Приёмка подсовывает свои файлы либо аргументами
# `check_report_delivery(..., config_path=..., log_dir=...)`, либо переменными
# окружения TUBER_X_HERMES_JOBS_JSON / TUBER_X_HERMES_LOG_DIR.
HERMES_JOBS_JSON = _os.environ.get("TUBER_X_HERMES_JOBS_JSON",
                                   "/root/.hermes/cron/jobs.json")
HERMES_LOG_DIR = _os.environ.get("TUBER_X_HERMES_LOG_DIR", "/root/.hermes/logs")
REPORT_JOB_SCRIPT = ("tuber_report.sh", "tuber_x_report.sh")
# ТЗ-C: проверка следит за доставкой ВЛАДЕЛЬЦУ. Владельческая сводка собирается
# обёрткой `tuber_report.sh` (задание 08:00); `tuber_x_report.sh` (07:00) сдаёт
# отчёт только в локальный файл (deliver=local) и в Telegram не доставляется —
# он оставлен вторым именем для совместимости, основное — первое.
REPORT_DELIVERY_HOURS = 30               # окно свежести доставки, ч (Р1.1)

_LOG_TS_RE = _re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
_DELIVERED_MARK = "delivered to telegram"
_DELIVERY_ERR_MARK = "delivery error"


def _iso_before(hours):
    return db.iso(datetime.now(timezone.utc) - timedelta(hours=hours))


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _res(name, alert, msg, *, severity="ALERT", **extra):
    out = {"name": name, "alert": bool(alert), "severity": severity, "msg": msg}
    out.update(extra)
    return out


# --------------------------------------------------------------- свежесть
def check_report_data(con, hours=None):
    """Задача 6 ТЗ виральности + согласование с окном зрелости метрики.

    За окно нет постов ИЛИ нет свежих оценок -> ALERT. Согласовано с
    `cli report` (код возврата 4) и обёрткой `tuber_x_report.sh`: пустые сутки —
    это не «успешная» работа, а сигнал о сломанном сборе.

    Живость отчёта измеряется свежестью САМИХ оценок (`scores.computed_at`), а
    не оценками постов, опубликованных в окне. Оценка скорости (`velocity_6h`,
    `VELOCITY_TARGET_HOURS`) требует снимка метрик в возрасте поста не меньше
    6 ч, а обогащение метриками идёт раз в 2 ч, поэтому самый свежий оцениваемый
    пост всегда 6-8 ч: строк значимости у постов младше 6 ч нет по построению,
    и требовать их — заведомо невыполнимое условие. Смысл тревоги сохранён:
    если оценки не пересчитывались дольше окна — ALERT.
    """
    hours = int(hours if hours is not None else config.HEALTH_STALE_HOURS)
    since = db.iso(datetime.now(timezone.utc) - timedelta(hours=hours))
    posts = con.execute("SELECT COUNT(*) FROM posts WHERE published_at_utc >= ?",
                        (since,)).fetchone()[0]
    scores_total = con.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    scores_fresh = con.execute("SELECT COUNT(*) FROM scores WHERE computed_at >= ?",
                               (since,)).fetchone()[0]
    maturity = float(config.VELOCITY_TARGET_HOURS)
    extra = {"value": posts, "scores_total": scores_total, "scores_fresh": scores_fresh}
    if posts == 0 or scores_fresh == 0:
        return _res("report_data", True,
                    f"за {hours} ч нет данных для отчёта: постов {posts},"
                    f" оценок всего {scores_total}, свежих оценок {scores_fresh}"
                    f" (зрелость метрики {maturity:.0f} ч — посты младше"
                    f" в оценку не попадают) — проверь сбор", **extra)
    return _res("report_data", False,
                f"за {hours} ч постов {posts}, оценок всего {scores_total},"
                f" свежих оценок {scores_fresh}"
                f" (зрелость метрики {maturity:.0f} ч)", **extra)


def check_stale(con, hours=None):
    """Свежесть всей базы: нет постов дольше порога -> ALERT (Р5)."""
    hours = config.HEALTH_STALE_HOURS if hours is None else hours
    row = con.execute("SELECT MAX(first_seen_at) AS m FROM posts").fetchone()
    last = db.parse_iso(row["m"]) if row and row["m"] else None
    if last is None:
        return _res("stale", True, "новых постов нет вообще", value=None)
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    alert = age_h > hours
    return _res("stale", alert,
                f"нет новых постов {age_h:.1f} ч (порог {hours} ч)",
                value=round(age_h, 2))


def check_tier_a_freshness(con, hours=None):
    """Свежесть TIER-A: последний пост тира A старше 3 ч -> ALERT (Р5)."""
    hours = config.HEALTH_TIER_A_STALE_HOURS if hours is None else hours
    row = con.execute(
        "SELECT MAX(p.published_at_utc) m, COUNT(*) n FROM posts p"
        " JOIN accounts a ON a.id=p.account_id WHERE a.tier='A'").fetchone()
    if not row or not row["n"]:
        return _res("tier_a_freshness", False,
                    "постов тира A в базе нет — проверка свежести тира A пропущена",
                    severity="WARN", value=None)
    last = db.parse_iso(row["m"])
    if last is None:
        return _res("tier_a_freshness", False, "у постов тира A нет валидной даты",
                    severity="WARN", value=None)
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    alert = age_h > hours
    return _res("tier_a_freshness", alert,
                f"последний пост TIER-A {age_h:.1f} ч назад (порог {hours} ч)",
                value=round(age_h, 2))


# ----------------------------------------------------------------- отказы
def check_fail_rate(con, hours=1):
    """Доля отказов сбора за прогон > 20% -> ALERT (Р5)."""
    total = con.execute("SELECT COUNT(*) FROM requests WHERE ts >= ?",
                        (_iso_before(hours),)).fetchone()[0]
    bad = con.execute("SELECT COUNT(*) FROM requests WHERE ts >= ? AND status != 200",
                      (_iso_before(hours),)).fetchone()[0]
    rate = (bad / total) if total else 0.0
    alert = total >= config.HEALTH_MIN_REQUESTS_FAIL and rate > config.HEALTH_FAIL_RATE_MAX
    return _res("fail_rate", alert,
                f"доля отказов {rate:.0%} за {hours} ч ({bad}/{total})",
                value=round(rate, 4), requests=total, failed=bad)


def check_cdn_429(con, hours=1):
    n = con.execute("SELECT COUNT(*) FROM requests WHERE kind='cdn_tweet'"
                    " AND status=429 AND ts >= ?", (_iso_before(hours),)).fetchone()[0]
    alert = n > config.HEALTH_CDN_429_PER_HOUR
    return _res("cdn_429", alert,
                f"cdn_tweet 429 за {hours} ч: {n}"
                f" (порог {config.HEALTH_CDN_429_PER_HOUR})", value=n)


def check_synd_budget(con, day=None):
    day = day or _today()
    row = con.execute("SELECT requests_today, day FROM instances WHERE host=?",
                      (config.SYND_HOST,)).fetchone()
    used = (row["requests_today"] or 0) if (row and row["day"] == day) else 0
    n429 = con.execute("SELECT COUNT(*) FROM requests WHERE kind='synd_timeline'"
                       " AND status=429 AND ts LIKE ?", (day + "%",)).fetchone()[0]
    exhausted = used >= config.SYND_DAILY_BUDGET
    alert = exhausted and n429 >= config.SYND_DAILY_BUDGET
    return _res("synd_budget", alert,
                f"ленточный канал: {used}/{config.SYND_DAILY_BUDGET} запросов"
                f" за сутки, из них 429: {n429}", value=used)


def check_long_text_gap(con):
    total = con.execute("SELECT COUNT(*) FROM posts WHERE is_long=1").fetchone()[0]
    bad = con.execute("SELECT COUNT(*) FROM posts WHERE is_long=1"
                      " AND COALESCE(length(text),0) < ?",
                      (config.ENRICH_LONG_TEXT_MIN,)).fetchone()[0]
    ratio = (bad / total) if total else 0.0
    alert = total > 0 and ratio > config.HEALTH_LONG_TEXT_GAP_RATIO
    return _res("long_text_gap", alert,
                f"длинных постов с обрезанным текстом {ratio:.0%}"
                f" ({bad}/{total}) — выдача врёт по содержанию",
                value=round(ratio, 4), long_total=total, long_short=bad)


# ------------------------------------------------- ТЗ-5: провенанс текста за сутки
def check_text_short_daily(con, day=None):
    """ТЗ-5 задача 1: доля постов за сутки, у которых текст подозрительно
    короткий при флаге длинного (is_long=1 и длина <= FULLTEXT_CDN_LIMIT)."""
    day = day or _today()
    total = con.execute("SELECT COUNT(*) FROM posts WHERE first_seen_at LIKE ?",
                        (day + "%",)).fetchone()[0]
    short = con.execute(
        "SELECT COUNT(*) FROM posts WHERE first_seen_at LIKE ? AND is_long=1"
        " AND COALESCE(length(text),0) <= ?",
        (day + "%", int(config.FULLTEXT_CDN_LIMIT))).fetchone()[0]
    ratio = (short / total) if total else 0.0
    alert = total > 0 and ratio > config.HEALTH_TEXT_SHORT_RATIO_MAX
    return _res("text_short_daily", alert,
                f"длинных постов с коротким текстом за сутки {ratio:.1%}"
                f" ({short}/{total}, порог {config.HEALTH_TEXT_SHORT_RATIO_MAX:.0%})",
                value=round(ratio, 6), posts=total, short=short)


def check_cdn_text_ratio(con, day=None):
    """ТЗ-5 задача 1: доля постов за сутки, у которых text_src='cdn'."""
    day = day or _today()
    total = con.execute("SELECT COUNT(*) FROM posts WHERE first_seen_at LIKE ?",
                        (day + "%",)).fetchone()[0]
    cdn = con.execute("SELECT COUNT(*) FROM posts WHERE first_seen_at LIKE ?"
                      " AND text_src='cdn'", (day + "%",)).fetchone()[0]
    ratio = (cdn / total) if total else 0.0
    alert = total > 0 and ratio > config.HEALTH_CDN_TEXT_RATIO_MAX
    return _res("cdn_text_ratio", alert,
                f"постов с текстом из CDN за сутки {ratio:.1%}"
                f" ({cdn}/{total}, порог {config.HEALTH_CDN_TEXT_RATIO_MAX:.0%})",
                value=round(ratio, 6), posts=total, cdn=cdn)


# ----------------------------------------------------------- инстансы: cooldown
def check_instance_cooldown(con):
    """ТЗ-5 задача 2: инстанс Nitter в cooldown дольше порога -> ALERT."""
    now = datetime.now(timezone.utc)
    rows = con.execute("SELECT host, cooldown_until FROM instances"
                       " WHERE cooldown_until IS NOT NULL").fetchall()
    worst_host, worst_min = None, 0.0
    n = 0
    for r in rows:
        cd = db.parse_iso(r["cooldown_until"])
        if not cd:
            continue
        left_min = (cd - now).total_seconds() / 60.0
        if left_min > config.HEALTH_COOLDOWN_MAX_MIN:
            n += 1
            if left_min > worst_min:
                worst_min, worst_host = left_min, r["host"]
    alert = n > 0
    if alert:
        msg = (f"инстансов Nitter в долгом cooldown (>{config.HEALTH_COOLDOWN_MAX_MIN}"
               f" мин): {n}; максимум {worst_min:.0f} мин у {worst_host}")
    else:
        msg = f"инстансов Nitter в долгом cooldown: 0 (порог {config.HEALTH_COOLDOWN_MAX_MIN} мин)"
    return _res("instance_cooldown", alert, msg, value=n,
                max_minutes=round(worst_min, 1), host=worst_host)


# ------------------------------------------- ТЗ-10: резерв и отказы сбора
#: Окно «фактического» снятия флага резерва (ТЗ-Tuber ч.3 §12–13): если за
#: столько минут был успешный фид Nitter (status=200, items>0) или не было
#: обращений к x_ssr при живом зеркале — резерв не активен.
RESERVE_RECOVERY_MINUTES = 30


def _nitter_feed_recovered(con, minutes=None):
    """Есть ли успешный фид Nitter за последние ``minutes`` минут (ТЗ-Tuber ч.3).

    Признак фактического восстановления: ``kind='feed'`` (запросы Nitter, не
    резерва), ``status=200`` и ``items>0``. Считается по журналу ``requests``.
    """
    minutes = RESERVE_RECOVERY_MINUTES if minutes is None else minutes
    since = _iso_before(minutes / 60.0)
    row = con.execute(
        "SELECT COUNT(*) FROM requests WHERE ts >= ? AND kind = 'feed'"
        " AND status = 200 AND items > 0", (since,)).fetchone()
    return bool(row[0])


def _nitter_instance_alive(con):
    """Есть ли хотя бы один живой инстанс Nitter (``instances.healthy=1``)."""
    row = con.execute(
        "SELECT COUNT(*) FROM instances WHERE healthy = 1").fetchone()
    return bool(row[0])


def _ssr_called_recently(con, minutes=None):
    """Обращались ли к резерву x_ssr за последние ``minutes`` минут."""
    minutes = RESERVE_RECOVERY_MINUTES if minutes is None else minutes
    since = _iso_before(minutes / 60.0)
    row = con.execute(
        "SELECT COUNT(*) FROM requests WHERE ts >= ? AND kind = 'x_ssr'",
        (since,)).fetchone()
    return bool(row[0])


def check_reserve_active(con, hours=None):
    """ТЗ-10 2.3: аварийный резерв x_ssr активен дольше 6 ч -> ALERT.

    «Активен» = есть непрерывное обращение к дублёру с момента `reserve_since`
    (снимается первым успешным фидом Nitter). В норме — тишина.

    ТЗ-Tuber ч.3: флаг ставит один процесс (x_ssr-брокер), а снимает успешный
    фид в другом процессе — из-за этого `reserve_since` залипал, хотя Nitter
    работает (факт боя: 15 ч ALERT при живых зеркалах). Сторож снимает флаг ПО
    ФАКТУ: если за :data:`RESERVE_RECOVERY_MINUTES` был успешный фид Nitter
    (status=200, items>0), либо к резерву не обращались при живом зеркале, —
    `reserve_since` сбрасывается и ALERT не печатается.
    """
    hours = config.HEALTH_XSSR_ACTIVE_HOURS if hours is None else hours
    since = db.reserve_active_since(con)
    if since is None:
        return _res("reserve_active", False, "резерв x_ssr не активен", value=None)
    recovered = _nitter_feed_recovered(con)
    if not recovered and _nitter_instance_alive(con) and not _ssr_called_recently(con):
        recovered = True
    if recovered:
        try:
            db.set_reserve_since(con, None)
        except Exception:
            pass
        return _res("reserve_active", False,
                    "резерв x_ssr не активен: Nitter отдаёт фиды"
                    " (флаг reserve_since снят по факту)", value=None)
    age_h = (datetime.now(timezone.utc) - since).total_seconds() / 3600.0
    alert = age_h > hours
    return _res("reserve_active", alert,
                f"резерв x_ssr активен {age_h:.1f} ч (порог {hours} ч):"
                f" Nitter недоступен долго", value=round(age_h, 2))


def check_collect_failures(con, hours=1):
    """ТЗ-10 2.3: фактических отказов сбора Nitter за час больше порога -> ALERT.

    Считаются транспортные отказы и 5xx по каналам сбора (feed/backfill/search);
    404 и пустые ленты отказом не являются и здесь не учитываются.
    """
    since = _iso_before(hours)
    n = con.execute(
        "SELECT COUNT(*) FROM requests WHERE ts >= ? AND kind IN"
        " ('feed','backfill','search') AND (status=0 OR status>=500)",
        (since,)).fetchone()[0]
    alert = n > config.HEALTH_COLLECT_FAIL_PER_HOUR
    return _res("collect_failures", alert,
                f"фактических отказов сбора Nitter за {hours} ч: {n}"
                f" (порог {config.HEALTH_COLLECT_FAIL_PER_HOUR})", value=n)


# ------------------------------------------------------------------- даты
def check_date_validity(con, day=None):
    """Валидность дат за сутки < 99% -> ALERT (Р5)."""
    day = day or _today()
    total = con.execute("SELECT COUNT(*) FROM posts WHERE first_seen_at LIKE ?",
                        (day + "%",)).fetchone()[0]
    bad = con.execute(
        "SELECT COUNT(*) FROM posts WHERE first_seen_at LIKE ? AND"
        " (published_at_utc IS NULL OR published_at_utc = ''"
        "  OR published_at_utc LIKE '1970%' OR published_src IN ('dup','invalid'))",
        (day + "%",)).fetchone()[0]
    ratio = 1.0 if total == 0 else (1.0 - bad / total)
    alert = total > 0 and ratio < config.HEALTH_DATE_VALID_MIN
    return _res("date_validity", alert,
                f"валидность дат за сутки {ratio:.2%} ({bad} брака из {total})",
                value=round(ratio, 6), posts=total, bad=bad)


# ---------------------------------------------------------------- инстансы
def check_instances_alive(con):
    """Живые инстансы Nitter: 0 живых -> ALERT (Р5)."""
    alive = con.execute("SELECT COUNT(*) FROM instances WHERE healthy=1").fetchone()[0]
    known = con.execute("SELECT COUNT(*) FROM instances WHERE healthy IS NOT NULL"
                        ).fetchone()[0]
    alert = known > 0 and alive == 0
    return _res("instances_alive", alert,
                f"живых инстансов Nitter: {alive} (проверенных: {known})",
                value=alive, known=known)


def check_quota(con, day=None):
    """Расход квоты > 80% суточного потолка -> ALERT (Р5)."""
    day = day or _today()
    spent = con.execute("SELECT COUNT(*) FROM requests WHERE ts LIKE ?",
                        (day + "%",)).fetchone()[0]
    cap = config.DAILY_REQUEST_CAP * max(1, len(config.INSTANCES))
    ratio = spent / cap if cap else 0.0
    alert = ratio > config.HEALTH_QUOTA_MAX
    return _res("quota", alert,
                f"расход квоты Nitter {spent}/{cap} = {ratio:.1%} за сутки",
                value=round(ratio, 6), spent=spent, cap=cap)


# ------------------------------------------------------------------ реестр
def check_discover_freshness(con, hours=None):
    """ТЗ-11 задача 3: дискавери не запускался / прогон завершился отказом -> ALERT.

    Расширение реестра живо ровно тогда, когда регулярно отрабатывает дискавери.
    Поэтому проверяем не «сколько новых active», а сам факт прогона:

      * в `runs` нет ни одной записи дискавери (mode LIKE 'discover%') -> ALERT;
      * последний прогон стартовал дольше порога (по умолчанию 36 ч) назад -> ALERT;
      * последний прогон в окне не завершился (нет `finished_at` — падение
        процесса) или завершился с ошибками (`errors > 0`) -> ALERT.

    В норме (свежий успешный прогон) молчит.
    """
    hours = config.HEALTH_DISCOVER_STALE_HOURS if hours is None else hours
    row = con.execute(
        "SELECT id, started_at, finished_at, errors FROM runs"
        " WHERE mode LIKE 'discover%' ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return _res("discover_freshness", True,
                    f"дискавери не запускался ни разу (порог {hours} ч)",
                    value=None, run_id=None)
    last = db.parse_iso(row["started_at"])
    age_h = ((datetime.now(timezone.utc) - last).total_seconds() / 3600.0
             if last else None)
    if age_h is None or age_h > hours:
        age_msg = "нет" if age_h is None else f"{age_h:.1f} ч"
        return _res("discover_freshness", True,
                    f"дискавери не запускался {age_msg} (порог {hours} ч)",
                    value=round(age_h, 2) if age_h is not None else None,
                    run_id=row["id"])
    errors = row["errors"] or 0
    if row["finished_at"] is None or errors > 0:
        detail = "не завершился (нет finished_at)" if row["finished_at"] is None \
            else f"errors={errors}"
        return _res("discover_freshness", True,
                    f"последний прогон дискавери завершился отказом: {detail}"
                    f" (run={row['id']}, {age_h:.1f} ч назад)",
                    value=round(age_h, 2), run_id=row["id"], errors=errors)
    return _res("discover_freshness", False,
                f"дискавери: последний прогон {age_h:.1f} ч назад, отказов нет",
                value=round(age_h, 2), run_id=row["id"])


def check_registry_growth(con, days=None):
    """ТЗ-11 задача 3: реестр действительно стоит -> WARN, иначе молчать.

    «Стоит» = за окно (по умолчанию 7 суток) не появилось ни одного нового
    кандидата И ни одного нового provisional. Это реальная поломка расширения,
    а не нормальный темп трёхступенчатой схемы.

    Прежняя проверка «новых active за 48 ч: 0» убрана из WARN: путь
    candidate -> provisional -> active занимает до 14 суток (ТЗ-2 Р2-БИС),
    поэтому ноль новых active — нормальное состояние. Её число остаётся в
    сообщении как справочное и в машинном выводе (`active_48h`), без тревоги.
    """
    days = config.HEALTH_REGISTRY_STALL_DAYS if days is None else days
    since = _iso_before(days * 24)
    new_cand = con.execute(
        "SELECT COUNT(*) FROM candidates WHERE first_seen_at >= ?", (since,)).fetchone()[0]
    new_prov = con.execute(
        "SELECT COUNT(*) FROM candidates WHERE validated='provisional'"
        " AND COALESCE(verified_at, first_seen_at) >= ?", (since,)).fetchone()[0]
    active_48h = con.execute(
        "SELECT COUNT(*) FROM accounts WHERE status='active' AND"
        " COALESCE(verified_at, added_at) >= ?",
        (_iso_before(config.HEALTH_REGISTRY_GROWTH_HOURS),)).fetchone()[0]
    stalled = (new_cand == 0 and new_prov == 0)
    return _res("registry_growth", stalled,
                f"реестр стоит: за {days} сут новых кандидатов {new_cand},"
                f" новых provisional {new_prov}"
                f" (справочно: новых active за {config.HEALTH_REGISTRY_GROWTH_HOURS} ч"
                f" {active_48h})",
                severity="WARN", value=new_cand + new_prov,
                candidates_new=new_cand, provisional_new=new_prov,
                active_48h=active_48h)


# ------------------------------------------------------- подозрительное (Р5)
def check_suspect(con):
    """xconf>=5 при всех авторах с dup_ratio>0.5 -> WARN (Р5)."""
    try:
        rows = con.execute(
            "SELECT COUNT(*) FROM stories WHERE suspect=1 AND xconf >= ?",
            (config.HEALTH_SUSPECT_MIN_XCONF,)).fetchone()[0]
    except Exception:
        rows = 0
    return _res("suspect", rows > 0,
                f"подозрительных сюжетов (xconf>={config.HEALTH_SUSPECT_MIN_XCONF},"
                f" все авторы дубли): {rows}", severity="WARN", value=rows)


# ------------------------------------ ТЗ-13 Р1: доставка сводки владельцу (D-06)
def _report_job_ids(config_path, script):
    """ID заданий отчёта из конфигурации планировщика (Р1.1).

    Идентификатор не хардкодится: задание ищется по имени скрипта. `script` —
    одно имя или кортеж имён (ТЗ-C: владельческая сводка + legacy-сборщик).
    Возвращает список id (обычно один, без повторов).
    """
    if isinstance(script, str):
        scripts = (script,)
    else:
        scripts = tuple(script)
    names = {_os.path.basename(s) for s in scripts if s}
    with open(config_path, encoding="utf-8") as fh:
        data = _json.load(fh)
    jobs = data.get("jobs") if isinstance(data, dict) else data
    ids = []
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        field = job.get("script")
        if isinstance(field, str) and _os.path.basename(field) in names:
            jid = job.get("id")
            if jid is not None and str(jid) not in ids:
                ids.append(str(jid))
    return ids


def _log_files(log_dir):
    """Файлы журналов планировщика: *.log и ротации *.log.N (без сети)."""
    out = []
    for name in sorted(_os.listdir(log_dir)):
        if name.endswith(".log") or _re.match(r"^.*\.log\.\d+$", name):
            out.append(_os.path.join(log_dir, name))
    return out


def _log_ts(line):
    """Локальная метка времени из начала строки журнала или None."""
    m = _LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _scan_report_delivery(log_dir, job_ids, cutoff):
    """(последний успех, последняя ошибка) доставки по журналам планировщика.

    Журналы планировщика пишутся в ЛОКАЛЬНОМ времени, поэтому окно считается от
    `datetime.now()` (наивная локальная метка). Признак успеха — `delivered to
    telegram` вместе с id задания; ошибки — `delivery error`.
    """
    last_ok = None
    last_err = None
    markers = tuple(f"Job '{jid}':" for jid in job_ids)
    for path in _log_files(log_dir):
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                ok = _DELIVERED_MARK in line
                err = _DELIVERY_ERR_MARK in line
                if not (ok or err):
                    continue
                if not any(mk in line for mk in markers):
                    continue
                ts = _log_ts(line)
                if ts is None:
                    continue
                if ok:
                    if ts >= cutoff and (last_ok is None or ts > last_ok):
                        last_ok = ts
                elif last_err is None or ts > last_err:
                    last_err = ts
    return last_ok, last_err


def check_report_delivery(con=None, hours=None, *, log_dir=None, config_path=None,
                          script=None):
    """ТЗ-13 Р1 (долг D-06): сводка отчёта ДОСТАВЛЕНА владельцу, не только собрана.

    Проверка читает конфигурацию заданий планировщика (id задания отчёта ищем по
    имени скрипта `tuber_x_report.sh`), затем журналы планировщика. Уровни (Р1.3):

      * нет строки успешной доставки за окно (по умолчанию 30 ч) -> ALERT;
      * доставка есть, но последняя попытка была с ошибкой -> WARN;
      * всё в норме -> проверка молчит.

    Устойчивость (Р1.4): нет конфигурации, нет журнала, в конфигурации нет
    задания — WARN с честной причиной, а не падение и не молчание.

    Сеть и Telegram не используются (Р1.5): только чтение файлов.
    """
    hours = REPORT_DELIVERY_HOURS if hours is None else hours
    log_dir = HERMES_LOG_DIR if log_dir is None else log_dir
    config_path = HERMES_JOBS_JSON if config_path is None else config_path
    script = REPORT_JOB_SCRIPT if script is None else script
    script_label = script if isinstance(script, str) else ", ".join(script)

    if not _os.path.isfile(config_path):
        return _res("report_delivery", True,
                    f"факт доставки сводки не проверить: нет конфигурации"
                    f" планировщика {config_path}",
                    severity="WARN", value=None, reason="no_config",
                    config_path=config_path)

    try:
        job_ids = _report_job_ids(config_path, script)
    except Exception as exc:  # noqa: BLE001 — честный WARN вместо падения
        return _res("report_delivery", True,
                    f"факт доставки сводки не проверить: конфигурация планировщика"
                    f" {config_path} не читается ({exc!r})",
                    severity="WARN", value=None, reason="bad_config",
                    config_path=config_path)

    if not job_ids:
        return _res("report_delivery", True,
                    f"факт доставки сводки не проверить: в конфигурации"
                    f" планировщика нет задания со скриптом {script_label}",
                    severity="WARN", value=None, reason="no_job",
                    config_path=config_path)

    if not _os.path.isdir(log_dir):
        return _res("report_delivery", True,
                    f"факт доставки сводки не проверить: нет каталога журналов"
                    f" планировщика {log_dir}",
                    severity="WARN", value=None, reason="no_logs",
                    log_dir=log_dir, job_ids=job_ids)

    try:
        last_ok, last_err = _scan_report_delivery(log_dir, job_ids,
                                                  datetime.now() - timedelta(hours=hours))
    except OSError as exc:
        return _res("report_delivery", True,
                    f"факт доставки сводки не проверить: журналы планировщика"
                    f" {log_dir} не читаются ({exc!r})",
                    severity="WARN", value=None, reason="bad_logs",
                    log_dir=log_dir, job_ids=job_ids)

    extra = {
        "job_ids": job_ids,
        "last_success": last_ok.isoformat(sep=" ") if last_ok else None,
        "last_error": last_err.isoformat(sep=" ") if last_err else None,
    }

    if last_ok is None:
        tail = (f", последняя ошибка {last_err:%Y-%m-%d %H:%M}"
                if last_err else ", ошибок доставки в журнале нет")
        return _res("report_delivery", True,
                    f"успешной доставки сводки в Telegram нет за {hours} ч"
                    f" (задание {', '.join(job_ids)}{tail})",
                    value=None, **extra)

    if last_err is not None and last_err > last_ok:
        return _res("report_delivery", True,
                    f"сводка доставлена {last_ok:%Y-%m-%d %H:%M}, но последняя"
                    f" попытка была с ошибкой ({last_err:%Y-%m-%d %H:%M})",
                    severity="WARN", value=None, **extra)

    age_h = (datetime.now() - last_ok).total_seconds() / 3600.0
    return _res("report_delivery", False,
                f"сводка доставлена в Telegram {last_ok:%Y-%m-%d %H:%M}"
                f" ({age_h:.1f} ч назад, окно {hours} ч)",
                value=round(age_h, 2), **extra)


# --------------------------------------------------------------------- run
def run(con, run_id=None):
    checks = [
        check_stale(con),
        check_report_data(con),
        check_tier_a_freshness(con),
        check_fail_rate(con),
        check_date_validity(con),
        check_instances_alive(con),
        check_quota(con),
        check_cdn_429(con),
        check_synd_budget(con),
        check_long_text_gap(con),
        check_text_short_daily(con),
        check_cdn_text_ratio(con),
        check_instance_cooldown(con),
        check_reserve_active(con),
        check_collect_failures(con),
        check_discover_freshness(con),
        check_registry_growth(con),
        check_suspect(con),
        check_report_delivery(con),
    ]
    alerts = [c for c in checks if c["alert"] and c["severity"] == "ALERT"]
    warns = [c for c in checks if c["alert"] and c["severity"] != "ALERT"]
    for c in checks:
        level = "ALERT" if c["alert"] and c["severity"] == "ALERT" else (
            "WARN" if c["alert"] else "INFO")
        try:
            db.log_run(con, level, f"health: {c['msg']}", run_id=run_id)
        except Exception:
            pass
    try:
        con.commit()
    except Exception:
        pass
    return {"checks": checks, "alerts": alerts, "warns": warns, "ok": not alerts}


def format_alerts(result):
    """Строки только для ALERT (пусто в норме) — stdout сторожа (Р5.1)."""
    return "\n".join(f"ALERT: {c['msg']}" for c in result["alerts"])


def format_report(result):
    lines = ["=== health (сторож ТЗ-4 Р6 / ТЗ-3 Р5)"]
    for c in result["checks"]:
        tag = ("ALERT" if c["alert"] and c["severity"] == "ALERT"
               else ("WARN " if c["alert"] else "ok   "))
        lines.append(f"  {tag} {c['name']:18s} {c['msg']}")
    lines.append(f"итог: {'ЕСТЬ АЛЕРТЫ' if result['alerts'] else 'норма'}")
    return "\n".join(lines)


# -------------------------------------------------------------------- main
def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    con = db.init_db()
    try:
        run_id = db.start_run(con, "health")
        res = run(con, run_id=run_id)
        db.finish_run(con, run_id, errors=len(res["alerts"]), note="health")
        if "--json" in argv:
            print(_json.dumps(res, ensure_ascii=False, indent=2, default=str))
        else:
            out = format_alerts(res)
            if out:
                print(out)
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
