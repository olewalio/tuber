#!/usr/bin/env python3
"""Карта крон-задач: кто и куда доставляет сообщения."""
import json
from collections import Counter

j = json.load(open("/root/.hermes/cron/jobs.json"))
jobs = j["jobs"] if isinstance(j, dict) and "jobs" in j else j
print("тип:", type(jobs).__name__, "кол-во:", len(jobs))

rows = []
if isinstance(jobs, dict):
    items = list(jobs.items())
else:
    items = [(x.get("job_id"), x) for x in jobs]

for jid, jb in items:
    d = jb.get("deliver") or jb.get("delivery") or "?"
    rows.append((d, jb.get("enabled", True), jid, jb.get("name"), jb.get("schedule"),
                 jb.get("no_agent"), jb.get("script"),
                 (jb.get("prompt") or "").replace("\n", " ")[:70]))

print("deliver:", dict(Counter(r[0] for r in rows)))
for d in sorted(set(r[0] for r in rows)):
    print(f"\n=== deliver: {d}  ({sum(1 for r in rows if r[0]==d)})")
    for r in sorted([x for x in rows if x[0] == d], key=lambda x: str(x[3])):
        print(f"  {'on ' if r[1] else 'OFF'} {r[2]} | {r[3]} | {r[4]} | script={r[6]} | {r[7][:70]}")
