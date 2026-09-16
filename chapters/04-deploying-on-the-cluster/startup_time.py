"""Seconds from pod creation to Ready, for the vllm Deployment."""
import json
import subprocess
from datetime import datetime

out = subprocess.check_output(
    ["kubectl", "get", "pod", "-l", "app=vllm", "-o", "json"]
)
pod = json.loads(out)["items"][0]
ts = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
created = ts(pod["metadata"]["creationTimestamp"])
ready = ts(next(c for c in pod["status"]["conditions"] if c["type"] == "Ready")["lastTransitionTime"])
print(f"{pod['metadata']['name']}: created {created:%H:%M:%S}, ready {ready:%H:%M:%S}, {(ready - created).total_seconds():.0f} s")