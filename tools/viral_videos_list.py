"""Виральные видео YouTube из боевой базы Tuber: список для человека + .md-файл.

Только чтение базы. Отбор — теми же функциями, что секция 5 полного отчёта
(охват = просмотры ÷ подписчики своего канала; кратность к норме канала = медиана
просмотров канала за окно, печатается при достаточной выборке).
"""
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/root/tuber")
from tuber.analysis import report as R  # noqa: E402

DB = "/root/tuber/data/tuber.db"
OUT = "/root/tuber/reports/viral-videos-2026-10-01.md"
cutoff, now = R._now_iso(10)
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

pool = R._viral_youtube_pool(conn, cutoff)
fresh = [r for r in pool if R._in_window(r["published_at"], cutoff)]
norm = R._norm_map(fresh, lambda r: r["source_id"], lambda r: r["views"])
snap = conn.execute("select max(captured_at) from metric_snapshot").fetchone()[0]

blocks = []
for label, fun in (("Полезное", False), ("Развлекательное и фермы", True)):
    rows, st = R.select_viral_youtube(pool, fun=fun, norm=norm, cutoff=cutoff)
    blocks.append((label, rows, st))

head = (f"Виральные видео YouTube — Tuber, окно 10 суток (с {cutoff} UTC), "
        f"данные замеров до {snap} UTC")
lines = [f"# {head}", ""]
chat = [head, ""]
n = 0
for label, rows, st in blocks:
    lines.append(f"## {label} (рассмотрено {st['considered']}, прошло полы "
                 f"{st['passed']}, показано {len(rows)})")
    lines.append("")
    chat.append(f"{label.upper()} ({len(rows)})")
    for it in rows:
        n += 1
        days = R._age_days(it.published_at)
        age = f"{days} дн." if days is not None else "?"
        if it.ratio is not None and (it.norm_n or 0) >= R.VIRAL_YT_MIN_NORM_SAMPLE:
            krat = f"кратность к норме канала {it.ratio:.1f} (норма {it.norm:,.0f})".replace(",", " ")
        else:
            krat = "норма канала: мало данных"
        lines += [f"{n}. {it.title} — канал {it.channel}",
                  f"   просмотров {it.views:,} (в сутки {it.views_per_day:,}), "
                  f"подписчиков {it.subs:,}, охват на подписчика {it.coverage:.1f}, {krat}; "
                  f"тема: {it.topic or 'без темы'}, опубл. {age}"
                  .replace(",", " "),
                  f"   {it.url}"]
        chat.append(
            f"{n}. {it.title} — {it.channel}")
        chat.append(
            f"   просмотров {it.views:,} (в сутки {it.views_per_day:,}), "
            f"охват {it.coverage:.1f} на подписчика, {krat}, {age} назад".replace(",", " "))
        chat.append(f"   {it.url}")
    lines.append("")
    chat.append("")

open(OUT, "w").write("\n".join(lines) + "\n")
print("файл:", OUT, "строк в файле:", len(lines))
print("\n".join(chat))
