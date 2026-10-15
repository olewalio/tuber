#!/usr/bin/env python3
"""Что осталось доставляться в чаты: по каждому origin-заданию — последний вывод."""
import json, os, glob, time

jobs = json.load(open("/root/.hermes/cron/jobs.json"))
items = jobs["jobs"] if isinstance(jobs, dict) and "jobs" in jobs else jobs
outdir = "/root/.hermes/cron/output"
print(f"{'job_id':14}{'name':44}{'sched':18}{'посл. вывод'}")
for jb in sorted(items, key=lambda x: str(x.get("deliver"))):
    if str(jb.get("deliver")) != "origin":
        continue
    jid = jb.get("job_id") or jb.get("id")
    files = sorted(glob.glob(os.path.join(outdir, jid, "*.md")))
    info = "нет выводов"
    if files:
        last = files[-1]
        mt = time.strftime("%d.%m %H:%M", time.localtime(os.path.getmtime(last)))
        try:
            txt = open(last, encoding="utf-8", errors="replace").read()
        except Exception as e:
            txt = f"<{e}>"
        txt = " ".join(txt.split())[:110]
        info = f"{mt} | {txt}"
    print(f"{jid:14}{(jb.get('name') or '')[:42]:44}{str(jb.get('schedule'))[:16]:18}{info}")
