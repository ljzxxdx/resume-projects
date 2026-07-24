#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

ensure_runtime_dirs
require_venv
require_env_file

SPIDER_NAME="${1:-${SPIDER_NAME:-mycrawler_redis}}"
exec "${VENV_PYTHON}" "${SCRIPT_DIR}/redis_admin.py" \
    observe --spider "${SPIDER_NAME}"
