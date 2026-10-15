#!/usr/bin/env python3
"""Кто и что реально доставлял в чаты за последние 48 часов."""
import json, re, gzip, os
from collections import Counter, defaultdict

jobs = json.load(open("/root/.hermes/cron/jobs.json"))
items = jobs["jobs"] if isinstance(jobs, dict) and "jobs" in jobs else jobs
meta = {}
for jb in items:
    jid = jb.get("job_id") or jb.get("id")
    meta[jid] = (jb.get("name"), jb.get("schedule"), jb.get("deliver"), jb.get("no_agent"), jb.get("script"))

LOG = "/root/.hermes/logs/agent.log"
lines = []
for p in (LOG, LOG + ".1", LOG + ".2", LOG + ".3"):
    if not os.path.exists(p):
        continue
    op = gzip.open if p.endswith(".gz") else open
    try:
        with op(p, "rt", errors="ignore") as f:
            for ln in f:
                lines.append(ln)
    except Exception as e:
        print("skip", p, e)

pat = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*Job '([0-9a-f]+)': delivered to telegram:([-\d]+)")
cnt = Counter()
first_seen = {}
last_seen = {}
for ln in lines:
    m = pat.match(ln)
    if m:
        ts, jid, chat = m.groups()
        cnt[(jid, chat)] += 1
        if ts > "2026-09-17 11:00":
            first_seen.setdefault((jid, chat), ts)
            last_seen[(jid, chat)] = ts

print("=== Доставки в чаты (всего в логах) ===")
for (jid, chat), n in cnt.most_common(60):
    nm, sch, dl, na, sc = meta.get(jid, ("НЕ НАЙДЕН в jobs.json", None, None, None, None))
    per_day = n / 3.0
    print(f"{n:5d} раз (≈{per_day:.0f}/сут) | chat={chat} | {jid} | {nm} | {sch} | deliver={dl} | script={sc}")

print("\n=== Только последние ~48ч (с 17.09 11:00) ===")
for (jid, chat), _ in cnt.most_common(60):
    if (jid, chat) in first_seen:
        nm, sch, dl, na, sc = meta.get(jid, ("?", None, None, None, None))
        print(f"chat={chat} | {jid} | {nm} | {sch} | deliver={dl} | {first_seen[(jid,chat)]} → {last_seen[(jid,chat)]}")
