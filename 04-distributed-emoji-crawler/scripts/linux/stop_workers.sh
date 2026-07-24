#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

ensure_runtime_dirs
SPIDER_NAME="${SPIDER_NAME:-mycrawler_redis}"
STOP_TIMEOUT="${STOP_TIMEOUT:-20}"

if [[ ! "${SPIDER_NAME}" =~ ^[A-Za-z0-9_]+$ ]]; then
    echo "invalid SPIDER_NAME: ${SPIDER_NAME}" >&2
    exit 2
fi
if ! is_positive_integer "${STOP_TIMEOUT}"; then
    echo "STOP_TIMEOUT must be a positive integer" >&2
    exit 2
fi

process_belongs_to_project() {
    local pid="$1"
    local process_cwd command_line
    [[ -r "/proc/${pid}/cmdline" ]] || return 1
    process_cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
    command_line="$(tr '\0' ' ' <"/proc/${pid}/cmdline")"
    [[ "${process_cwd}" == "${PROJECT_ROOT}" ]] \
        && [[ "${command_line}" == *"start.py"* ]] \
        && [[ "${command_line}" == *"--spider ${SPIDER_NAME}"* ]]
}

exec 9>"${RUN_DIR}/${SPIDER_NAME}.workers.lock"
if ! flock -n 9; then
    echo "another start/stop operation is active for ${SPIDER_NAME}" >&2
    exit 1
fi

shopt -s nullglob
pid_files=("${RUN_DIR}/${SPIDER_NAME}-"*.pid)
if [[ "${#pid_files[@]}" -eq 0 ]]; then
    echo "no PID files found for ${SPIDER_NAME}"
    exit 0
fi

declare -a stopping_pids=()
for pid_file in "${pid_files[@]}"; do
    pid="$(<"${pid_file}")"
    if [[ ! "${pid}" =~ ^[0-9]+$ ]] \
        || ! kill -0 "${pid}" 2>/dev/null \
        || ! process_belongs_to_project "${pid}"; then
        echo "stale or foreign PID file removed without signaling: ${pid_file}"
        rm -f -- "${pid_file}"
        continue
    fi
    kill -TERM "${pid}"
    stopping_pids+=("${pid}:${pid_file}")
    echo "sent SIGTERM pid=${pid}"
done

deadline=$((SECONDS + STOP_TIMEOUT))
for entry in "${stopping_pids[@]}"; do
    pid="${entry%%:*}"
    pid_file="${entry#*:}"
    while kill -0 "${pid}" 2>/dev/null && (( SECONDS < deadline )); do
        sleep 1
    done
    if kill -0 "${pid}" 2>/dev/null && process_belongs_to_project "${pid}"; then
        echo "SIGTERM timeout; sending SIGKILL pid=${pid}" >&2
        kill -KILL "${pid}"
    fi
    rm -f -- "${pid_file}"
    echo "stopped pid=${pid}"
done
