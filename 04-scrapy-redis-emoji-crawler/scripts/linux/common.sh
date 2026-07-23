#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
VENV_PYTHON="${PROJECT_ROOT}/.venv/bin/python"
RUN_DIR="${PROJECT_ROOT}/run"
LOG_DIR="${PROJECT_ROOT}/logs"

ensure_runtime_dirs() {
    mkdir -p \
        "${RUN_DIR}" \
        "${LOG_DIR}" \
        "${PROJECT_ROOT}/output" \
        "${PROJECT_ROOT}/images"
}

require_venv() {
    if [[ ! -x "${VENV_PYTHON}" ]]; then
        echo "virtualenv missing: run bash scripts/linux/init_env.sh first" >&2
        exit 1
    fi
}

require_env_file() {
    if [[ ! -f "${PROJECT_ROOT}/.env" && -z "${REDIS_URL:-}" ]]; then
        echo "Redis configuration missing: create .env or export REDIS_URL" >&2
        exit 1
    fi
}

is_positive_integer() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]]
}
