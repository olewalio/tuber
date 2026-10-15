#!/usr/bin/env python3
"""Выгрузка сырья для выкладки Tuber: виральные / необычные / интересные / связанное."""
import sqlite3, json, sys

DB = "/root/tuber/data/tuber.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
out = []


def sec(t):
    out.append("\n" + "=" * 100 + f"\n### {t}\n" + "=" * 100)


def rows(sql, args=()):
    try:
        return con.execute(sql, args).fetchall()
    except Exception as e:
        out.append(f"ERR {e}\nSQL: {sql[:200]}")
        return []


def dump(rs, n=60, width=110):
    if not rs:
        out.append("(пусто)")
        return
    for r in rs[:n]:
        out.append(" | ".join(str(r[k])[:width] for k in r.keys()))
    if len(rs) > n:
        out.append(f"... ещё {len(rs)-n}")


# ---------------------------------------------------------------- X: свежие
sec("X: свежие посты за 48 ч — топ по лайкам (без ретвитов)")
dump(rows("""
 SELECT c.author_handle h, c.published_at pub, c.external_id,
        l.likes, l.replies, l.quotes, l.reposts,
        sc.significance sig, sc.engagement eng, sc.velocity vel, sc.spread spread,
        cl.topic, cl.novelty, cl.title_ru,
        substr(replace(replace(c.text,char(10),' '),char(13),' '),1,220) text
 FROM content c
 JOIN content_latest l ON l.content_id=c.id
 LEFT JOIN classification cl ON cl.content_id=c.id
 LEFT JOIN score sc ON sc.content_id=c.id AND sc.computed_at=(SELECT MAX(computed_at) FROM score WHERE content_id=c.id)
 WHERE c.platform='x' AND c.is_repost=0 AND c.deleted_at IS NULL
   AND c.published_at >= datetime('now','-2 day') AND l.likes IS NOT NULL
 ORDER BY l.likes DESC LIMIT 30"""))

sec("X: лайки в час (свежесть+масштаб), 48 ч, лайков >= 1000")
dump(rows("""
 SELECT c.author_handle h, c.published_at pub,
        l.likes, l.replies,
        round(l.likes*1.0/MAX(1,(julianday('now')-julianday(c.published_at))*24),1) likes_per_h,
        substr(replace(replace(c.text,char(10),' '),char(13),' '),1,180) text,
        'https://x.com/'||c.author_handle||'/status/'||c.external_id url
 FROM content c JOIN content_latest l ON l.content_id=c.id
 WHERE c.platform='x' AND c.is_repost=0 AND c.deleted_at IS NULL
   AND c.published_at >= datetime('now','-2 day') AND l.likes >= 1000
 ORDER BY likes_per_h DESC LIMIT 30"""))

sec("X: спорные — ответов много относительно лайков (лайков >= 1500), 72 ч")
dump(rows("""
 SELECT c.author_handle h, c.published_at pub, l.likes, l.replies,
        round(l.replies*1.0/l.likes,3) rep_per_like,
        substr(replace(replace(c.text,char(10),' '),char(13),' '),1,170) text,
        'https://x.com/'||c.author_handle||'/status/'||c.external_id url
 FROM content c JOIN content_latest l ON l.content_id=c.id
 WHERE c.platform='x' AND c.is_repost=0 AND c.deleted_at IS NULL
   AND c.published_at >= datetime('now','-3 day') AND l.likes >= 1500 AND l.replies IS NOT NULL
 ORDER BY rep_per_like DESC LIMIT 20"""))

sec("X: высокий novelty (новизна темы), свежие 5 суток")
dump(rows("""
 SELECT c.author_handle h, c.published_at pub, cl.novelty, cl.topic, cl.title_ru,
        l.likes, substr(replace(replace(c.text,char(10),' '),char(13),' '),1,170) text,
        'https://x.com/'||c.author_handle||'/status/'||c.external_id url
 FROM content c JOIN classification cl ON cl.content_id=c.id
 LEFT JOIN content_latest l ON l.content_id=c.id
 WHERE c.platform='x' AND c.deleted_at IS NULL AND cl.novelty IS NOT NULL
   AND c.published_at >= datetime('now','-5 day')
 ORDER BY cl.novelty DESC LIMIT 20"""))

sec("X: разрез тем за 48 ч (число постов, сумма лайков)")
dump(rows("""
 SELECT cl.topic, count(*) n, sum(l.likes) likes, max(l.likes) mx
 FROM content c JOIN classification cl ON cl.content_id=c.id
 JOIN content_latest l ON l.content_id=c.id
 WHERE c.platform='x' AND c.deleted_at IS NULL AND c.published_at>=datetime('now','-2 day')
   AND cl.is_ai=1 GROUP BY 1 ORDER BY n DESC"""))

# ------------------------------------------------------------ YouTube: свежие
sec("YouTube: свежие (published <=3 суток) топ по просмотрам/сутки, с темой")
dump(rows("""
 SELECT c.published_at pub, cl.topic, cl.title_ru, c.title,
        l.views, l.likes, round(l.views_per_day) vpd, c.lang, c.duration_seconds dur,
        c.url, (SELECT count(*) FROM metric_snapshot m WHERE m.content_id=c.id) snaps
 FROM content c
 LEFT JOIN classification cl ON cl.content_id=c.id
 JOIN content_latest l ON l.content_id=c.id
 WHERE c.platform='youtube' AND c.deleted_at IS NULL
   AND c.published_at >= datetime('now','-4 day') AND l.views_per_day IS NOT NULL
 ORDER BY l.views_per_day DESC LIMIT 40"""))

sec("YouTube: редкие темы (скандалы/наука/роботы/дата-центры/чипы), 7 суток")
dump(rows("""
 SELECT cl.topic, c.published_at, cl.title_ru, l.views, l.likes, round(l.views_per_day) vpd, c.url
 FROM content c JOIN classification cl ON cl.content_id=c.id JOIN content_latest l ON l.content_id=c.id
 WHERE c.platform='youtube' AND c.deleted_at IS NULL AND l.views_per_day IS NOT NULL
   AND c.published_at >= datetime('now','-7 day')
   AND cl.topic IN ('скандалы и риски','наука и медицина','роботы и физический ИИ',
                    'дата-центры и энергия','чипы и железо','регулирование и безопасность')
 ORDER BY cl.topic, l.views DESC LIMIT 45"""))

sec("YouTube: аномалии замеров (metric_snapshot.is_anomaly=1, 7 суток)")
dump(rows("""
 SELECT m.captured_at, m.views, m.likes, m.delta_views, m.age_hours, c.title, c.url, c.published_at
 FROM metric_snapshot m JOIN content c ON c.id=m.content_id
 WHERE m.is_anomaly=1 AND m.captured_at >= datetime('now','-7 day')
 ORDER BY m.captured_at DESC LIMIT 20"""))

sec("YouTube: свежие RU-видео (lang ru), 4 суток, по просмотрам/сутки")
dump(rows("""
 SELECT cl.title_ru, cl.topic, l.views, round(l.views_per_day) vpd, c.published_at, c.url
 FROM content c LEFT JOIN classification cl ON cl.content_id=c.id JOIN content_latest l ON l.content_id=c.id
 WHERE c.platform='youtube' AND c.deleted_at IS NULL AND l.views_per_day IS NOT NULL
   AND c.published_at >= datetime('now','-4 day') AND (c.lang LIKE 'ru%')
 ORDER BY l.views_per_day DESC LIMIT 25"""))

# ------------------------------------------------------------------- Telegram
sec("Telegram: свежие посты профильных ИИ/тех-каналов за 48 ч, по просмотрам")
dump(rows("""
 SELECT s.handle, c.published_at, l.views, l.reactions, l.forwards,
        round(l.views*1.0/(SELECT median_views FROM source_baseline b WHERE b.source_id=s.id),1) krat,
        substr(replace(replace(c.text,char(10),' '),char(13),' '),1,200) text,
        c.url
 FROM content c JOIN source s ON s.id=c.source_id LEFT JOIN content_latest l ON l.content_id=c.id
 WHERE c.platform='telegram' AND c.deleted_at IS NULL AND l.views IS NOT NULL
   AND c.published_at >= datetime('now','-2 day')
   AND s.status IN ('active')
 ORDER BY l.views DESC LIMIT 40"""))

sec("Telegram: база каналов — активные ИИ/тех (handle, title, подписчики, медиана)")
dump(rows("""
 SELECT s.handle, s.title, s.status, s.subscribers, b.median_views, b.items_in_window
 FROM source s LEFT JOIN source_baseline b ON b.source_id=s.id
 WHERE s.platform='telegram' AND s.status='active' ORDER BY s.subscribers DESC NULLS LAST"""))

sec("Telegram: аномальный ER (реакции/просмотры) за 48 ч, просмотров >= 2000")
dump(rows("""
 SELECT s.handle, c.published_at, l.views, l.reactions, round(l.reactions*1.0/l.views,4) er,
        substr(replace(replace(c.text,char(10),' '),char(13),' '),1,150) text, c.url
 FROM content c JOIN source s ON s.id=c.source_id JOIN content_latest l ON l.content_id=c.id
 WHERE c.platform='telegram' AND c.deleted_at IS NULL AND l.views>=2000 AND l.reactions IS NOT NULL
   AND c.published_at >= datetime('now','-2 day')
 ORDER BY er DESC LIMIT 20"""))

sec("Telegram: свежие посты с #ИИ/#нейро/ИИ в тексте — за 48 ч (профильные каналы)")
dump(rows("""
 SELECT s.handle, c.published_at, l.views, l.reactions,
        substr(replace(replace(c.text,char(10),' '),char(13),' '),1,200) text, c.url
 FROM content c JOIN source s ON s.id=c.source_id JOIN content_latest l ON l.content_id=c.id
 WHERE c.platform='telegram' AND c.deleted_at IS NULL AND c.published_at >= datetime('now','-2 day')
   AND (c.text LIKE '%нейросет%' OR c.text LIKE '%ИИ%' OR c.text LIKE '%GPT%' OR c.text LIKE '%Claude%'
        OR c.text LIKE '%ai-%' OR c.text LIKE '%агент%')
   AND s.status IN ('active','candidate') AND l.views >= 3000
 ORDER BY l.views DESC LIMIT 30"""))

# ------------------------------------------------------------------- Сюжеты
sec("Сюжеты: сквозные (cross) с участниками")
dump(rows("""
 SELECT st.id, st.created_at, st.title, st.topic, st.source_count, st.content_count,
        st.first_pub_at, st.last_pub_at, st.lead_time_min,
        (SELECT group_concat(DISTINCT m.handle) FROM story_member m WHERE m.story_id=st.id) handles,
        (SELECT group_concat(DISTINCT c.platform) FROM story_member m JOIN content c ON c.id=m.content_id WHERE m.story_id=st.id) platforms
 FROM story st ORDER BY st.first_pub_at DESC LIMIT 30"""))

sec("Сюжеты: свежие (first_pub_at за 5 суток) по числу участников")
dump(rows("""
 SELECT st.id, st.title, st.topic, st.source_count, st.content_count, st.first_pub_at,
        (SELECT group_concat(DISTINCT m.handle) FROM story_member m WHERE m.story_id=st.id) handles
 FROM story st WHERE st.first_pub_at >= datetime('now','-5 day')
 ORDER BY st.source_count DESC, st.content_count DESC LIMIT 25"""))

sec("Сюжеты: сюжеты из X-контура (platform='x'), свежие")
dump(rows("""
 SELECT st.id, st.platform, st.title, st.topic, st.source_count, st.first_pub_at, st.last_pub_at,
        st.lead_time_min, st.is_new_entity, st.suspect
 FROM story st WHERE st.platform <> 'cross' AND st.first_pub_at >= datetime('now','-4 day')
 ORDER BY st.source_count DESC LIMIT 25"""))

# --------------------------------------------------------------- Связанное
sec("Тёмные лошадки X: авторы с максимумом новых сюжетов за 7 суток")
dump(rows("""
 SELECT m.handle, count(DISTINCT m.story_id) stories, min(st.first_pub_at) first_at, max(st.first_pub_at) last_at
 FROM story_member m JOIN story st ON st.id=m.story_id JOIN content c ON c.id=m.content_id
 WHERE c.platform='x' AND st.first_pub_at >= datetime('now','-7 day')
 GROUP BY 1 ORDER BY stories DESC LIMIT 20"""))

sec("Топ источников по НОВЫМ материалам за сутки (все платформы)")
dump(rows("""
 SELECT c.platform, COALESCE(s.handle, c.author_handle, '?') src, s.title, count(*) n
 FROM content c LEFT JOIN source s ON s.id=c.source_id
 WHERE c.first_seen_at >= datetime('now','-1 day')
 GROUP BY 1,2 ORDER BY n DESC LIMIT 30"""))

sec("Тематическая панель: классифицированные ИИ-посты за 48 ч по темам")
dump(rows("""
 SELECT cl.topic, count(*) n,
   sum(CASE WHEN c.platform='x' THEN 1 ELSE 0 END) x,
   sum(CASE WHEN c.platform='youtube' THEN 1 ELSE 0 END) yt
 FROM content c JOIN classification cl ON cl.content_id=c.id
 WHERE cl.is_ai=1 AND c.first_seen_at >= datetime('now','-2 day') GROUP BY 1 ORDER BY n DESC"""))

sec("Сущности недели (entities в свежих сюжетах) — топ упоминаний")
dump(rows("""
 SELECT e.value, count(*) n FROM story st, json_each(st.entities) e
 WHERE st.first_pub_at >= datetime('now','-7 day') GROUP BY 1 ORDER BY n DESC LIMIT 40"""))

print("\n".join(out))
