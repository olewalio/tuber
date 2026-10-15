#!/usr/bin/env python3
import json
j = json.load(open("/root/.hermes/cron/jobs.json"))
it = j["jobs"] if isinstance(j, dict) and "jobs" in j else j
for x in it:
    if (x.get("job_id") or x.get("id")) in {"04535782dfb1", "305942bed7ef", "e9fba8fa03c3", "30d9009932f1", "bbca43d3abf6"}:
        print("---", x.get("name"))
        for k in ("job_id", "deliver", "origin", "origin_chat_id", "origin_thread_id", "created_at", "schedule"):
            if k in x:
                print(f"   {k}: {x[k]}")
