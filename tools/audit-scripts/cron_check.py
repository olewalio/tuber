#!/usr/bin/env python3
import json, collections
j = json.load(open("/root/.hermes/cron/jobs.json"))
it = j["jobs"] if isinstance(j, dict) and "jobs" in j else j
print("deliver:", collections.Counter(str(x.get("deliver")) for x in it))
print()
print("ОСТАЛОСЬ в чаты (origin):")
for x in it:
    if str(x.get("deliver")) == "origin":
        print(f"  {x.get('job_id') or x.get('id')}  {x.get('name')}")
