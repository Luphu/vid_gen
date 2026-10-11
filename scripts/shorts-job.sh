#!/usr/bin/env bash
# Разовая команда shorts в кластере: Job из шаблона CronJob shorts-pipeline (тот же образ, env, лимиты),
# но с другими аргументами. Ждёт завершения и печатает лог.
#
#   scripts/shorts-job.sh doctor --live
#   scripts/shorts-job.sh run --dry-run
#   scripts/shorts-job.sh status
#   scripts/shorts-job.sh retry <job> --stage upload
set -euo pipefail
NS=${NS:-shorts}
[[ $# -ge 1 ]] || { echo "usage: $0 <shorts args...>" >&2; exit 1; }
name="shorts-$(printf '%s' "$1" | tr -cd 'a-z0-9-' | cut -c1-20)-$(date +%s)"

kubectl -n "$NS" get cronjob shorts-pipeline -o json | python3 -c '
import json, sys
cj = json.load(sys.stdin)
name, args = sys.argv[1], sys.argv[2:]
spec = cj["spec"]["jobTemplate"]["spec"]
spec["template"]["spec"]["containers"][0]["args"] = ["--json-logs", *args]
spec["ttlSecondsAfterFinished"] = 3600
print(json.dumps({"apiVersion": "batch/v1", "kind": "Job",
                  "metadata": {"name": name, "labels": {"app.kubernetes.io/name": "shorts",
                                                       "app.kubernetes.io/component": "adhoc"}},
                  "spec": spec}))
' "$name" "$@" | kubectl -n "$NS" apply -f - >/dev/null

echo "job/$name" >&2
kubectl -n "$NS" wait --for=condition=Ready "pod" -l "job-name=$name" --timeout=180s >/dev/null 2>&1 || true
kubectl -n "$NS" logs -f "job/$name"
kubectl -n "$NS" wait --for=condition=Complete "job/$name" --timeout=10s >/dev/null 2>&1 \
  || { echo "job $name did not complete successfully (exit code 2 = blocked/needs attention)" >&2; exit 2; }
