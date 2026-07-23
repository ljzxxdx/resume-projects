#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

ensure_runtime_dirs
require_venv
require_env_file

SPIDER_NAME="${SPIDER_NAME:-mycrawler_redis}"
if [[ "$#" -eq 0 ]]; then
    echo "usage: $0 URL [URL ...]" >&2
    exit 2
fi

exec "${VENV_PYTHON}" "${SCRIPT_DIR}/redis_admin.py" \
    enqueue --spider "${SPIDER_NAME}" "$@"
