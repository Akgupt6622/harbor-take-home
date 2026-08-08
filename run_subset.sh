#!/usr/bin/env bash
# Run a subset of retail tasks on Modal, then parse the results into runs/<job>.
#
# Usage: ./run_subset.sh <job-name> <task-list-file> [extra harbor args, e.g. --n-attempts 3]
# Task list: one task name or glob per line; blank lines and #-comments skipped.
# Spends OpenAI + Modal credits. Env vars (e.g. UV_DEFAULT_INDEX) pass through untouched.
set -euo pipefail

job_name="$1"
task_list="$2"
shift 2

include_args=()
while IFS= read -r task; do
  [[ -z "$task" || "$task" == \#* ]] && continue
  include_args+=(--include-task-name "$task")
done < "$task_list"
[[ ${#include_args[@]} -gt 0 ]] || { echo "no tasks in $task_list" >&2; exit 1; }

PYTHONPATH=adapters/tau3-bench uv run harbor run -y \
  -c tau-retail.yaml \
  --job-name "$job_name" \
  --path datasets/tau3-bench \
  "${include_args[@]}" \
  --env-file .env \
  "$@"

uv run python -m analysis.parse_traces "results/tau-retail/$job_name"
uv run python -m analysis.experiments record "results/tau-retail/$job_name" --taskset "$task_list"
