#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

ensure_runtime_dirs
require_venv
require_env_file

SPIDER_NAME="${1:-${SPIDER_NAME:-mycrawler_redis}}"
REPLAY_LIMIT="${2:-${REPLAY_LIMIT:-100}}"
if ! is_positive_integer "${REPLAY_LIMIT}"; then
    echo "REPLAY_LIMIT must be a positive integer" >&2
    exit 2
fi

exec 9>"${RUN_DIR}/replay-image-${SPIDER_NAME}.lock"
if ! flock -n 9; then
    echo "image replay already running for ${SPIDER_NAME}; skipped"
    exit 0
fi

cd "${PROJECT_ROOT}"
"${VENV_PYTHON}" "${PROJECT_ROOT}/failure_tasks.py" \
    replay image --spider "${SPIDER_NAME}" --all --limit "${REPLAY_LIMIT}"
