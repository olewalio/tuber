#!/usr/bin/env python3
"""Предварительное наполнение: разовый разбор t.me/s по активным каналам.

Модуль сохранён как исторический инструмент (ТЗ-1 проекта tuber-telegram):
проверяет разбор страницы и запись постов до готовности боевого сборщика. В
монорепозитории он пишет в ЕДИНУЮ базу через адаптер
:mod:`tuber.platforms.telegram.store` (legacy-форма таблиц ``channels``/``posts``
поверх ядра), поэтому логика разбора и заливки не менялась — изменился только
слой доступа к БД (ТЗ-4).

Запуск::

    python3 -m tuber tg prelim [--db PATH] [--limit N] [--pages N]
"""
from __future__ import annotations

import argparse
import html as H
import random
import re
import sys
import time
from datetime import datetime, timezone

import httpx

from . import config as _config
from . import store as db

HEAD = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/120 Safari/537.36"}


def strip_tags(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</?div[^>]*>", "\n", s)
    s = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>',
               lambda m: f" {H.unescape(re.sub('<[^>]+>', '', m.group(2)))} ({m.group(1)}) ",
               s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", H.unescape(s)).strip()


def parse(pg: str, handle: str):
    out = []
    for seg in re.split(r'<div class="tgme_widget_message[ ")]', pg)[1:]:
        mp = re.search(r'data-post="([^/]+)/(\d+)"', seg)
        if not mp:
            continue
        mid = int(mp.group(2))
        mt = re.search(r'<time datetime="([^"]+)"', seg)
        date = None
        if mt:
            try:
                date = (datetime.fromisoformat(mt.group(1).replace("Z", "+00:00"))
                        .astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
            except ValueError:
                pass
        mtxt = re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*'
            r'(?:<div class="tgme_widget_message_(?:footer|reply_markup|meta))', seg, re.S)
        text = strip_tags(mtxt.group(1)) if mtxt else ""
        mv = re.search(r'tgme_widget_message_views">([^<]+)<', seg)
        views = None
        if mv:
            v = mv.group(1).strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
            try:
                mult = 1
                if v.endswith("K"):
                    mult, v = 1000, v[:-1]
                elif v.endswith("M"):
                    mult, v = 1000000, v[:-1]
                views = int(float(v) * mult)
            except ValueError:
                pass
        fwd = 1 if "tgme_widget_message_forwarded_from" in seg else 0
        fwdsrc = None
        if fwd:
            mf = re.search(r'tgme_widget_message_forwarded_from[^>]*>.*?<span[^>]*>([^<]+)<',
                           seg, re.S)
            if mf:
                fwdsrc = H.unescape(mf.group(1))[:80]
        media = None
        for kind, pat in (("photo", "tgme_widget_message_photo_wrap"),
                          ("video", "tgme_widget_message_video"),
                          ("doc", "tgme_widget_message_document"),
                          ("poll", "tgme_widget_message_poll"),
                          ("audio", "tgme_widget_message_voice")):
            if pat in seg:
                media = kind
                break
        links = list(dict.fromkeys(re.findall(r'href="(https?://[^"]+)"', seg)))
        links = [x for x in links if "t.me" not in x or "t.me/s/" not in x][:8]
        hashtags = ",".join(re.findall(r"#([\wА-Яа-я_]{2,30})", text))[:200]
        mentions = ",".join(re.findall(r"(?<![\w/])@([A-Za-z0-9_]{4,32})", text))[:200]
        is_ad = 1 if re.search(r"ERID|erid|реклама|промокод", text[:200], re.I) else 0
        if not text and not media:
            continue
        out.append((mid, date, text[:8000], views, fwd, fwdsrc, media,
                    ",".join(links)[:600], hashtags, mentions, is_ad))
    return out


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Telegram: предварительное наполнение t.me/s")
    ap.add_argument("--db", default=None, help="путь к единой базе")
    ap.add_argument("--limit", type=int, default=25, help="сколько активных каналов взять")
    ap.add_argument("--pages", type=int, default=2, help="страниц на канал")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    db_path = _config.resolve_db(args.db)
    con = db.connect(db_path)

    ch = con.execute("""SELECT id, handle FROM channels WHERE status='active'
                        ORDER BY COALESCE(subs,0) DESC LIMIT ?""", (args.limit,)).fetchall()
    tot_new = tot_upd = errs = 0
    for cid, h in ch:
        min_id = con.execute("SELECT COALESCE(MAX(message_id),0) FROM posts WHERE channel_id=?",
                             (cid,)).fetchone()[0]
        before = ""
        for page in range(args.pages):
            url = f"https://t.me/s/{h}" + before
            try:
                r = httpx.get(url, timeout=25, headers=HEAD, follow_redirects=True)
                if r.status_code != 200:
                    errs += 1
                    break
            except Exception:  # noqa: BLE001
                errs += 1
                break
            rows = parse(r.text, h)
            if not rows:
                if "Contact @" in r.text or "tgme_widget_message" not in r.text:
                    con.execute("UPDATE channels SET read_mode='unreadable', status='private'"
                                " WHERE id=?", (cid,))
                break
            oldest = min(x[0] for x in rows)
            for (mid, date, text, views, fwd, fwdsrc, media, links, ht, men, ad) in rows:
                pr = con.execute("SELECT id FROM posts WHERE channel_id=? AND message_id=?",
                                 (cid, mid)).fetchone()
                if pr:
                    con.execute("UPDATE posts SET views=?, views_checked_at=datetime('now')"
                                " WHERE id=?", (views, pr[0]))
                    tot_upd += 1
                else:
                    con.execute(
                        """INSERT INTO posts (channel_id,message_id,date_utc,text,views,forwards,
                           media_kind,links,hashtags,mentions,fwd_from,is_forward,has_own_media,
                           is_ad,views_checked_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
                        (cid, mid, date, text, views, None, media, links, ht, men, fwdsrc, fwd,
                         1 if media else 0, ad))
                    tot_new += 1
            if oldest <= min_id or page == args.pages - 1:
                break
            before = f"?before={oldest}"
            time.sleep(random.uniform(0.6, 1.0))
        con.execute("UPDATE channels SET checked_at=datetime('now') WHERE id=?", (cid,))
        con.commit()
        time.sleep(random.uniform(0.6, 1.0))

    con.execute(
        "INSERT INTO runs (started_at,finished_at,mode,channels_ok,channels_fail,posts_new,"
        "posts_upd,errors,note) VALUES (datetime('now'),datetime('now'),'web-preliminary',"
        "?,?,?,?,?,?)",
        (len(ch) - errs, errs, tot_new, tot_upd, errs, "разовое предварительное наполнение"))
    con.commit()
    print(f"каналов: {len(ch)} | новых постов: {tot_new} | обновлено просмотров: {tot_upd} | "
          f"ошибок: {errs}")
    print("всего постов в базе:", con.execute("SELECT COUNT(*) FROM posts").fetchone()[0])
    print("каналов с постами:",
          con.execute("SELECT COUNT(DISTINCT channel_id) FROM posts").fetchone()[0])
    print("\nсвежие посты (пример):")
    for h, t, d, v in con.execute(
            """SELECT c.handle, substr(p.text,1,90), p.date_utc, p.views FROM posts p
               JOIN channels c ON c.id=p.channel_id WHERE p.text<>''
               ORDER BY p.date_utc DESC LIMIT 10"""):
        print(f"  {str(d)[5:16]} @{h[:20]:20s} views={str(v):>6} | "
              f"{(t or '').replace(chr(10),' ')[:80]}")
    print("\nраспределение по дням (последние 7):")
    for d, n in con.execute("""SELECT substr(date_utc,1,10) d, COUNT(*) FROM posts
                               GROUP BY d ORDER BY d DESC LIMIT 7"""):
        print(f"  {d}: {n}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
