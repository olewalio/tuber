#!/usr/bin/env python3
"""Плоский список заданий: id | deliver | script | name | prompt(40)."""
import json
jobs = json.load(open("/root/.hermes/cron/jobs.json"))
items = jobs["jobs"] if isinstance(jobs, dict) and "jobs" in jobs else jobs
rows = []
for jb in items:
    rows.append((
        jb.get("job_id") or jb.get("id"),
        str(jb.get("deliver")),
        str(jb.get("script")),
        jb.get("name"),
        (jb.get("prompt") or "").replace("\n", " ")[:50],
        str(jb.get("enabled")),
    ))
rows.sort(key=lambda r: (r[1], r[2]))
print(f"{'job_id':14}{'en':3}{'deliver':26}{'script':34}{'name'}")
for jid, dl, sc, nm, pr, en in rows:
    print(f"{jid:14}{'1' if en=='True' else '0':3}{dl:26}{sc:34}{nm}")
print("ИТОГО:", len(rows))
