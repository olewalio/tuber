#!/usr/bin/env python3
"""Вторая выгрузка: X по темам/спорам, Telegram кратность, YouTube эмоции."""
import sqlite3

con = sqlite3.connect("file:/root/tuber/data/tuber.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
out = []

AI_TG = ('seeallochnaya','denissexy','chatgptv','xor_journal','deeptechnet','data_secrets',
         'ai_machinelearning_big_data','vibecoding_tg','data_analysis_ml','machinelearning_ru',
         'dsproglib','Gewissta','neuro_channel','omggpt','techno_yandex','exploitex','hiaimedia',
         'vibescriptaibest','quant_prune_distill','repo_dev','whackdoor','ru_tech_talk','molyanov_blog',
         'ggreat_of_development','news_engineering','kontext_channel','bloodysx','sharon_al')


def sec(t):
    out.append("\n" + "=" * 100 + f"\n### {t}\n" + "=" * 100)


def rows(sql, args=()):
    try:
        return con.execute(sql, args).fetchall()
    except Exception as e:
        out.append(f"ERR {e}")
        return []


def dump(rs, n=40, w=115):
    if not rs:
        out.append("(пусто)")
        return
    for r in rs[:n]:
        out.append(" | ".join(str(r[k])[:w] for k in r.keys()))
    if len(rs) > n:
        out.append(f"... ещё {len(rs)-n}")


X_SEL = """
 SELECT c.author_handle h, c.published_at pub, l.likes, l.replies, l.reposts, cl.topic,
        substr(replace(replace(c.text,char(10),' '),char(13),' '),1,230) text,
        'https://x.com/'||c.author_handle||'/status/'||c.external_id url
 FROM content c JOIN content_latest l ON l.content_id=c.id
 LEFT JOIN classification cl ON cl.content_id=c.id
 WHERE c.platform='x' AND c.is_repost=0 AND c.deleted_at IS NULL
"""

sec("X: топ по лайкам за 48 ч (тема)")
dump(rows(X_SEL + " AND c.published_at>=datetime('now','-2 day') ORDER BY l.likes DESC LIMIT 35"))

sec("X: топ по репостам за 48 ч")
dump(rows(X_SEL + " AND c.published_at>=datetime('now','-2 day') ORDER BY l.reposts DESC LIMIT 20"))

sec("X: скандалы и риски — 5 суток, по лайкам")
dump(rows(X_SEL + " AND cl.topic='скандалы и риски' AND c.published_at>=datetime('now','-5 day') ORDER BY l.likes DESC LIMIT 25"))

sec("X: исследования и бенчмарки — 5 суток, по лайкам")
dump(rows(X_SEL + " AND cl.topic='исследования и бенчмарки' AND c.published_at>=datetime('now','-5 day') ORDER BY l.likes DESC LIMIT 25"))

sec("X: кейсы внедрения — 5 суток, по лайкам")
dump(rows(X_SEL + " AND cl.topic='кейсы внедрения' AND c.published_at>=datetime('now','-5 day') ORDER BY l.likes DESC LIMIT 20"))

sec("Telegram (профильные): свежие 3 суток, по кратности к своей норме канала")
dump(rows("""
 SELECT s.handle, c.published_at, l.views, l.reactions,
        round(l.views*1.0/(SELECT median_views FROM source_baseline b WHERE b.source_id=s.id),2) krat,
        (SELECT round(median_views) FROM source_baseline b WHERE b.source_id=s.id) base,
        substr(replace(replace(c.text,char(10),' '),char(13),' '),1,190) text, c.url
 FROM content c JOIN source s ON s.id=c.source_id JOIN content_latest l ON l.content_id=c.id
 WHERE c.platform='telegram' AND c.deleted_at IS NULL AND l.views>=1000
   AND c.published_at>=datetime('now','-3 day') AND s.handle IN (%s)
   AND EXISTS (SELECT 1 FROM source_baseline b WHERE b.source_id=s.id)
 ORDER BY krat DESC LIMIT 30""" % ",".join("?" * len(AI_TG)), AI_TG))

sec("Telegram (профильные): свежие 3 суток, по просмотрам")
dump(rows("""
 SELECT s.handle, c.published_at, l.views, l.reactions,
        substr(replace(replace(c.text,char(10),' '),char(13),' '),1,190) text, c.url
 FROM content c JOIN source s ON s.id=c.source_id JOIN content_latest l ON l.content_id=c.id
 WHERE c.platform='telegram' AND c.deleted_at IS NULL AND l.views>=3000
   AND c.published_at>=datetime('now','-3 day') AND s.handle IN (%s)
 ORDER BY l.views DESC LIMIT 40""" % ",".join("?" * len(AI_TG)), AI_TG))

sec("YouTube: эмоциональный отклик — лайков >= 4% от просмотров (просмотров >= 20k), 10 суток")
dump(rows("""
 SELECT c.published_at, cl.topic, cl.title_ru, l.views, l.likes,
        round(l.likes*1.0/l.views,4) like_share, round(l.views_per_day) vpd, c.url
 FROM content c JOIN content_latest l ON l.content_id=c.id LEFT JOIN classification cl ON cl.content_id=c.id
 WHERE c.platform='youtube' AND c.deleted_at IS NULL AND l.views>=20000 AND l.likes IS NOT NULL
   AND c.published_at>=datetime('now','-10 day') AND cl.topic IS NOT NULL
 ORDER BY like_share DESC LIMIT 30"""))

sec("YouTube: самые комментируемые (спор) — comments/views, просмотров >= 50k, 7 суток")
dump(rows("""
 SELECT c.published_at, cl.topic, cl.title_ru, l.views, l.comments,
        round(l.comments*1.0/l.views,5) c_share, round(l.views_per_day) vpd, c.url
 FROM content c JOIN content_latest l ON l.content_id=c.id LEFT JOIN classification cl ON cl.content_id=c.id
 WHERE c.platform='youtube' AND c.deleted_at IS NULL AND l.views>=50000 AND l.comments IS NOT NULL
   AND c.published_at>=datetime('now','-7 day')
 ORDER BY c_share DESC LIMIT 25"""))

sec("YouTube: нетипично длинные/короткие — топ по длительности при высоких просмотрах, 7 суток")
dump(rows("""
 SELECT c.published_at, cl.topic, cl.title_ru, c.duration_seconds, l.views, round(l.views_per_day) vpd, c.url
 FROM content c JOIN content_latest l ON l.content_id=c.id LEFT JOIN classification cl ON cl.content_id=c.id
 WHERE c.platform='youtube' AND c.deleted_at IS NULL AND l.views>=100000
   AND c.published_at>=datetime('now','-7 day') AND c.duration_seconds IS NOT NULL
 ORDER BY c.duration_seconds DESC LIMIT 15"""))

sec("YouTube: темы-редкости за 7 суток (наука, роботы, чипы, дата-центры) — топ по просмотрам")
dump(rows("""
 SELECT cl.topic, cl.title_ru, l.views, l.likes, round(l.views_per_day) vpd, c.published_at, c.url
 FROM content c JOIN content_latest l ON l.content_id=c.id JOIN classification cl ON cl.content_id=c.id
 WHERE c.platform='youtube' AND c.deleted_at IS NULL
   AND cl.topic IN ('роботы и физический ИИ','чипы и железо','наука и медицина','дата-центры и энергия')
   AND c.published_at>=datetime('now','-7 day')
 ORDER BY l.views DESC LIMIT 30"""))

print("\n".join(out))
