"""ТЗ «свежесть значимости» (17.09.2026): метрики X доходят до ядра и переживают обход.

Диагноз разрыва (только чтение боевой базы + копии): до 12:15 МСК 17.09.2026
шимы планировщика ``~/.hermes/scripts/tuber_x_*.sh`` указывали на легаси-проект
``/root/tuber-x`` со своей базой ``tuber_x.db``. Поэтому ``x enrich`` писал
метрики в легаси (журнал: покрытие 1340/1341 = числа легаси), а единое ядро
``/root/tuber/data/tuber.db`` метрик после 06:00:29Z не получало (покрытие
1320/1361 = числа ядра). После переустановки шрамов монорепо-установщиком
(``scripts/install_hermes_cron.sh``) цепочка снова одна.

Здесь зафиксировано поведение УЖЕ исправленной цепочки, чтобы разрыв не
вернулся:
  1) обогащение РЕАЛЬНО пишет метрики в ядро (обратный тест: без обогащения их
     нет и пост не попадает в рейтинг значимости);
  2) повторный обход реестра (UPDATE той же строки) метрику не теряет;
  3) повторный сбор не плодит дубли ``content`` (одна строка на ``external_id``);
  4) расписание обогащения в обоих установщиках ежечасное (``5 * * * *``), как
     в реестре владельца.

Сеть не используется: роутер каналов подменяется заглушкой.
"""
from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace

import pytest

from tuber.platforms.x import collect, enrich, scoring, store as db

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CDN_FIELDS = {"likes": 7, "replies": 1, "has_quote": 0, "is_long": 0,
              "lang": "en", "author_verified": 0, "screen_name": "alice",
              "text": "hello"}


class StubRouter:
    """Роутер-заглушка канала ``cdn_tweet``: отдаёт заданные ответы по tweet_id."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.cdn = SimpleNamespace(requests_429=0)

    def enrich_tweet(self, tweet_id):
        return self.mapping[str(tweet_id)]


def _add_account(con, handle="alice", tier="A"):
    con.execute("INSERT INTO accounts (handle, tier, status) VALUES (?,?,'active')",
                (handle, tier))
    con.commit()
    return con.execute("SELECT id FROM accounts WHERE handle=?", (handle,)).fetchone()["id"]


def _post(tid, published="2026-09-17T08:00:00", text="hello"):
    return {"tweet_id": str(tid), "published_at_utc": published,
            "published_src": "nitter", "text": text, "text_hash": "h-" + str(tid),
            "owner_handle": "alice", "is_retweet": 0}


def _metrics_in_core(con, tid):
    """Метрики напрямую из ядра ``content.meta_json`` (не через представление)."""
    return con.execute(
        "SELECT json_extract(meta_json, '$.metrics_at') AS metrics_at,"
        "       json_extract(meta_json, '$.likes')     AS likes,"
        "       json_extract(meta_json, '$.replies')   AS replies"
        " FROM content WHERE platform='x' AND external_id=?", (str(tid),)).fetchone()


def _enrich_ok(tid, likes=7, replies=1):
    return {str(tid): ("ok", dict(CDN_FIELDS, likes=likes, replies=replies), 200)}


# --------------------------------------------------------------- 1. запись в ядро
def test_enrich_writes_metrics_into_core_and_ranking(con):
    """Обогащение пишет метрики в ядро; без него пост в рейтинг не попадает.

    Это обратный тест от «метрики сохранились потому, что обогащение вообще
    ничего не пишет»: до прогона ``metrics_at`` пуст и ``scoring.rank`` пуст,
    после — метрики на месте и пост в рейтинге.
    """
    acc = _add_account(con)
    collect.store_posts(con, acc, [_post("111")], text_src="nitter")
    # обратная половина: без обогащения метрик нет и значимости нет
    assert _metrics_in_core(con, "111")["metrics_at"] is None
    assert scoring.coverage(con) == {"total": 1, "enriched": 0, "ratio": 0.0,
                                     "likes_median": None}
    assert scoring.rank(con, limit=10) == []

    s = enrich.enrich_batch(con, StubRouter(_enrich_ok("111", likes=7, replies=1)))
    assert s["selected"] == 1 and s["ok"] == 1 and s["errors"] == 0

    met = _metrics_in_core(con, "111")
    assert met["metrics_at"], "метрика не дошла до ядра content.meta_json!"
    assert met["likes"] == 7 and met["replies"] == 1
    assert scoring.coverage(con)["enriched"] == 1
    ranked = scoring.rank(con, limit=10)
    assert [r["tweet_id"] for r in ranked] == ["111"]


# ------------------------------------------------- 2. повторный обход не теряет
def test_metrics_survive_recollection(con):
    """Повторный сбор того же поста (UPDATE строки) метрику не затирает."""
    acc = _add_account(con)
    collect.store_posts(con, acc, [_post("222")], text_src="nitter")
    enrich.enrich_batch(con, StubRouter(_enrich_ok("222", likes=9, replies=2)))
    before = _metrics_in_core(con, "222")
    assert before["metrics_at"] and before["likes"] == 9

    res = collect.store_posts(con, acc, [_post("222", text="updated body")],
                              text_src="nitter")
    assert res == {"new": 0, "upd": 1, "skipped": 0}

    after = _metrics_in_core(con, "222")
    assert after["metrics_at"] == before["metrics_at"]
    assert after["likes"] == 9 and after["replies"] == 2
    text = con.execute("SELECT text FROM posts WHERE tweet_id='222'").fetchone()["text"]
    assert text == "updated body", "повторный сбор должен обновлять текст"
    assert scoring.coverage(con)["enriched"] == 1


# ------------------------------------------------- 3. повторный сбор без дублей
def test_recollection_does_not_duplicate_content(con):
    """Идемпотентность: два обхода одного поста -> одна строка ядра."""
    acc = _add_account(con)
    collect.store_posts(con, acc, [_post("333")], text_src="nitter")
    enrich.enrich_batch(con, StubRouter(_enrich_ok("333")))
    collect.store_posts(con, acc, [_post("333")], text_src="nitter")
    n = con.execute("SELECT COUNT(*) FROM content WHERE platform='x'"
                    " AND external_id='333'").fetchone()[0]
    assert n == 1
    # история метрик тоже не задваивается (INSERT OR IGNORE по taken_at)
    hist = con.execute("SELECT COUNT(*) FROM post_metrics_history WHERE tweet_id='333'"
                       ).fetchone()[0]
    assert hist == 1


# ------------------------------------------- 4. каденция обогащения — ежечасная
def _installer_enrich_expr(path):
    """Расписание enrich из блока ``JOBS=(...)`` установщика (не из LAUNCHERS)."""
    text = open(path, encoding="utf-8").read()
    m = re.search(r"^[ \t]*JOBS=\([ \t]*$\n(.*?)^[ \t]*\)[ \t]*$", text,
                  re.M | re.S)
    assert m, f"в {path} не найден блок JOBS=(...)"
    exprs = []
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line.startswith('"tuber_x_enrich.sh:'):
            continue
        exprs.append(line[1:-1].split(":", 2)[1].strip())
    assert exprs, f"в {path} нет задания tuber_x_enrich.sh в блоке JOBS"
    return exprs


@pytest.mark.parametrize("rel", ["scripts/install_hermes_cron.sh",
                                 "scripts/x/install_hermes_cron.sh"])
def test_enrich_schedule_is_hourly_in_installers(rel):
    """Требование ТЗ: обогащение X — ``5 * * * *`` (каждый час), не ``5 */2``."""
    exprs = _installer_enrich_expr(os.path.join(ROOT, rel))
    assert exprs == ["5 * * * *"], f"{rel}: ожидалось 5 * * * *, получено {exprs}"
    assert "5 */2 * * *" not in open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_enrich_schedule_matches_owner_registry():
    """Расписание установщика совпадает с реестром владельца (если он доступен)."""
    jobs_json = os.environ.get("TUBER_HERMES_JOBS_JSON", "/root/.hermes/cron/jobs.json")
    if not os.path.isfile(jobs_json):
        pytest.skip(f"реестр планировщика не найден: {jobs_json}")
    data = json.load(open(jobs_json, encoding="utf-8"))
    entries = data.get("jobs") if isinstance(data, dict) else data
    actual = {}
    for job in entries or []:
        if isinstance(job, dict) and job.get("script"):
            base = os.path.basename(job["script"])
            sched = job.get("schedule") or {}
            actual[base] = sched.get("expr") if isinstance(sched, dict) else None
    assert actual.get("tuber_x_enrich.sh") == "5 * * * *"


def test_deployed_x_launchers_do_not_point_at_legacy_project():
    """Первопричина 17.09.2026: устаревшие шимы указывали на /root/tuber-x.

    Если боевые шимы стоят, они обязаны делегировать в монорепо
    ``/root/tuber/scripts/x/``, а не в легаси ``/root/tuber-x/scripts/``. Иначе
    снова разъедутся две базы: обогащение в ``tuber_x.db``, ядро без метрик.
    """
    scripts_dir = os.environ.get("HERMES_SCRIPTS_DIR", "/root/.hermes/scripts")
    shim = os.path.join(scripts_dir, "tuber_x_enrich.sh")
    if not os.path.isfile(shim):
        pytest.skip(f"боевой шим не найден: {shim}")
    text = open(shim, encoding="utf-8").read()
    assert "/root/tuber-x/" not in text, (
        "шим обогащения снова указывает на легаси-проект /root/tuber-x "
        "(метрики уйдут в tuber_x.db, а не в ядро)")
    assert "/root/tuber/scripts/x/tuber_x_enrich.sh" in text, (
        "шим обогащения не делегирует в монорепо-обёртку")

