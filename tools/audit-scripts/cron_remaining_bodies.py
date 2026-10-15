#!/usr/bin/env python3
"""Тело последнего вывода каждого origin-задания (то, что уходит в чат)."""
import json, os, glob

jobs = json.load(open("/root/.hermes/cron/jobs.json"))
items = jobs["jobs"] if isinstance(jobs, dict) and "jobs" in jobs else jobs
outdir = "/root/.hermes/cron/output"
want = {j.get("job_id") or j.get("id"): j.get("name") for j in items if str(j.get("deliver")) == "origin"}
for jid, name in want.items():
    files = sorted(glob.glob(os.path.join(outdir, jid, "*.md")))
    if not files:
        print(f"\n########## {name} ({jid}) — выводов нет")
        continue
    raw = open(files[-1], encoding="utf-8", errors="replace").read()
    # отрезать служебный заголовок вида "# Cron Job: ... **Run Time:** ..."
    body = raw.split("**Mode:**", 1)[-1]
    body = body.split("\n", 1)[-1] if "\n" in body else body
    body = body.strip()[:420]
    print(f"\n########## {name} ({jid}) [{os.path.basename(files[-1])}]\n{body}")
