"""Разовая выдача: виральные видео YouTube из боевой базы Tuber (только чтение)."""
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/root/tuber")
from tuber.analysis import report as R  # noqa: E402

DB = "/root/tuber/data/tuber.db"
cutoff, now = R._now_iso(10)
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

yt_pool = R._viral_youtube_pool(conn, cutoff)
fresh = [r for r in yt_pool if R._in_window(r["published_at"], cutoff)]
norm = R._norm_map(fresh, lambda r: r["source_id"], lambda r: r["views"])
print(f"# окно с {cutoff} UTC | сформировано {now} UTC | видео-пул {len(yt_pool)}, свежих {len(fresh)}")
print(f"# последний снапшот: "
      f"{conn.execute('select max(captured_at) from metric_snapshot').fetchone()[0]}")

for label, fun in (("ПОЛЕЗНОЕ", False), ("РАЗВЛЕКАТЕЛЬНОЕ", True)):
    rows, st = R.select_viral_youtube(yt_pool, fun=fun, norm=norm, cutoff=cutoff)
    print(f"\n## {label} — рассмотрено {st.get('considered')}, прошло {st.get('passed')}, показано {len(rows)}")
    for r in rows:
        print("\t".join("" if x is None else str(x) for x in (
            r.title, r.channel, r.views, r.views_per_day, r.subs, r.coverage,
            r.ratio, r.likes, r.published_at, r.url, r.topic)))
