#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

ensure_runtime_dirs
require_venv
require_env_file

SPIDER_NAME="${SPIDER_NAME:-mycrawler_redis}"
WORKER_COUNT="${WORKER_COUNT:-2}"
WORKER_ID_PREFIX="${WORKER_ID_PREFIX:-$(hostname -s)}"

if [[ ! "${SPIDER_NAME}" =~ ^[A-Za-z0-9_]+$ ]]; then
    echo "invalid SPIDER_NAME: ${SPIDER_NAME}" >&2
    exit 2
fi
if ! is_positive_integer "${WORKER_COUNT}"; then
    echo "WORKER_COUNT must be a positive integer" >&2
    exit 2
fi

spider_args=()
if [[ "${SPIDER_NAME}" == "dmoz" ]]; then
    spider_args=(-a "redis_start=true")
fi

WORKER_ID_PREFIX="$(printf '%s' "${WORKER_ID_PREFIX}" | tr -cs 'A-Za-z0-9._-' '_')"
exec 9>"${RUN_DIR}/${SPIDER_NAME}.workers.lock"
if ! flock -n 9; then
    echo "another start/stop operation is active for ${SPIDER_NAME}" >&2
    exit 1
fi

cd "${PROJECT_ROOT}"
for index in $(seq 1 "${WORKER_COUNT}"); do
    pid_file="${RUN_DIR}/${SPIDER_NAME}-${index}.pid"
    log_file="${LOG_DIR}/${SPIDER_NAME}-${index}.log"
    worker_id="${WORKER_ID_PREFIX}-${SPIDER_NAME}-${index}"

    if [[ -f "${pid_file}" ]]; then
        pid="$(<"${pid_file}")"
        if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
            echo "worker ${index} already running pid=${pid} log=${log_file}"
            continue
        fi
        echo "removing stale PID file ${pid_file}"
        rm -f -- "${pid_file}"
    fi

    nohup env WORKER_ID="${worker_id}" \
        "${VENV_PYTHON}" "${PROJECT_ROOT}/start.py" \
        --spider "${SPIDER_NAME}" \
        "${spider_args[@]}" \
        9>&- >>"${log_file}" 2>&1 </dev/null &
    pid=$!
    printf '%s\n' "${pid}" >"${pid_file}.tmp"
    mv -f -- "${pid_file}.tmp" "${pid_file}"

    sleep 1
    if ! kill -0 "${pid}" 2>/dev/null; then
        echo "worker ${index} exited during startup; log tail:" >&2
        tail -n 30 "${log_file}" >&2 || true
        rm -f -- "${pid_file}"
        exit 1
    fi
    echo "started worker=${worker_id} pid=${pid} log=${log_file}"
done
