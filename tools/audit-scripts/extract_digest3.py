#!/usr/bin/env python3
"""Компактная третья выгрузка."""
import sqlite3
con = sqlite3.connect("file:/root/tuber/data/tuber.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
o = []


def q(t, sql, n=12, w=150):
    o.append("\n### " + t)
    try:
        rs = con.execute(sql).fetchall()
    except Exception as e:
        o.append("ERR " + str(e)); return
    if not rs:
        o.append("(пусто)"); return
    for r in rs[:n]:
        o.append(" | ".join(str(r[k])[:w] for k in r.keys()))
    if len(rs) > n:
        o.append(f"... ещё {len(rs)-n}")


q("X: лайки в час за 48ч (топ-12)", """
SELECT c.author_handle h, l.likes, round(l.likes*1.0/(julianday('now')-julianday(c.published_at))/24,0) lph,
 substr(replace(c.text,char(10),' '),1,150) t, 'https://x.com/'||c.author_handle||'/status/'||c.external_id u
FROM content c JOIN content_latest l ON l.content_id=c.id
WHERE c.platform='x' AND c.is_repost=0 AND c.published_at>=datetime('now','-2 day') AND l.likes>=800
ORDER BY lph DESC""")

q("X: аномальные пропорции — ответов больше лайков (спор), лайков>=300, 3 суток", """
SELECT c.author_handle h, l.likes, l.replies, l.reposts,
 round(l.replies*1.0/l.likes,2) rl, substr(replace(c.text,char(10),' '),1,140) t, c.url
FROM content c JOIN content_latest l ON l.content_id=c.id
WHERE c.platform='x' AND c.is_repost=0 AND c.published_at>=datetime('now','-3 day')
 AND l.likes>=300 AND l.replies>l.likes ORDER BY l.replies DESC""")

q("Аномалии метрик (metric_snapshot.is_anomaly=1), 7 суток", """
SELECT c.platform, c.author_handle, c.published_at, m.views, m.likes, m.is_anomaly,
 substr(replace(c.text,char(10),' '),1,120) t, c.url
FROM metric_snapshot m JOIN content c ON c.id=m.content_id
WHERE m.is_anomaly=1 AND m.captured_at>=datetime('now','-7 day') ORDER BY m.captured_at DESC""", 15)

q("YouTube: свежак за 48ч, топ-15 по просмотрам в сутки (все темы)", """
SELECT round(l.views_per_day) vpd, l.views, cl.topic, cl.title_ru, c.published_at, c.url
FROM content c JOIN content_latest l ON l.content_id=c.id JOIN classification cl ON cl.content_id=c.id
WHERE c.platform='youtube' AND c.published_at>=datetime('now','-2 day') AND l.views_per_day IS NOT NULL
ORDER BY l.views_per_day DESC""", 15)

q("Сквозные сюжеты: участники (топ-8 сюжетов)", """
SELECT st.id, st.title, st.platform_count pc, sm.platform, sm.content_id,
 (SELECT COALESCE(s2.handle, c2.author_handle) FROM content c2 LEFT JOIN source s2 ON s2.id=c2.source_id WHERE c2.id=sm.content_id) who,
 substr(replace((SELECT text FROM content c3 WHERE c3.id=sm.content_id),char(10),' '),1,105) t,
 (SELECT url FROM content c4 WHERE c4.id=sm.content_id) u
FROM story_member sm JOIN story st ON st.id=sm.story_id
WHERE st.platform_count>=2
ORDER BY st.platform_count DESC, st.id DESC LIMIT 40""", 40)

q("X: топ-15 лайков за 24ч (эмоциональный разрез)", """
SELECT c.author_handle h, l.likes, l.replies, round(l.likes*1.0/NULLIF(l.replies,0),1) lr,
 substr(replace(c.text,char(10),' '),1,110) t, c.url
FROM content c JOIN content_latest l ON l.content_id=c.id
WHERE c.platform='x' AND c.is_repost=0 AND c.published_at>=datetime('now','-1 day') AND l.likes>=1500
ORDER BY l.likes DESC""", 15)

print("\n".join(o))
