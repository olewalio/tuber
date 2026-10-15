#!/usr/bin/env python3
"""Проверка: все URL из выкладки существуют в tuber.db; печатаем платформу, дату, метрики, заголовок."""
import re, sqlite3

con = sqlite3.connect("file:/root/tuber/data/tuber.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
md = open("/root/tuber/reports/vykladka-2026-09-19.md", encoding="utf-8").read()
urls = []
for m in re.finditer(r"\]\((https?://[^)]+)\)", md):
    u = m.group(1).rstrip(".,;")
    if u not in urls:
        urls.append(u)
print(f"urls: {len(urls)}")
missing = []
for u in urls:
    row = con.execute("""select c.platform, c.published_at, c.author_handle, cl.title_ru,
      l.views, l.likes, l.replies, l.views_per_day from content c
      left join classification cl on cl.content_id=c.id left join content_latest l on l.content_id=c.id
      where c.url=?""", (u,)).fetchone()
    if row is None:
        row = con.execute("select platform, published_at, author_handle, text from content where url like ?", (u + "%",)).fetchone()
    if row is None:
        missing.append(u)
        print("НЕТ В БАЗЕ:", u)
    else:
        d = dict(row)
        t = (d.get("title_ru") or d.get("text") or "")[:80].replace("\n", " ")
        print(f"OK {d['platform']} | {d['published_at']} | {d.get('author_handle')} | {d.get('views')}/{d.get('likes')} | {t} | {u}")
print("\nОТСУТСТВУЮТ:", len(missing))
for u in missing:
    print("  -", u)
