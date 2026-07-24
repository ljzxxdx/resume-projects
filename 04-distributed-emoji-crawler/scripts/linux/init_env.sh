#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

if [[ "${EUID}" -eq 0 ]]; then
    SUDO=()
elif command -v sudo >/dev/null 2>&1; then
    SUDO=(sudo)
else
    echo "sudo is required to install Ubuntu packages" >&2
    exit 1
fi

"${SUDO[@]}" apt-get update
"${SUDO[@]}" apt-get install -y \
    build-essential \
    libffi-dev \
    libjpeg-dev \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    python3 \
    python3-dev \
    python3-venv \
    redis-tools \
    util-linux \
    zlib1g-dev

python3 - <<'PY'
import sys

if sys.version_info < (3, 9):
    raise SystemExit(
        f"Python >= 3.9 is required, found {sys.version.split()[0]}"
    )
print(f"using Python {sys.version.split()[0]}")
PY

python3 -m venv "${PROJECT_ROOT}/.venv"
"${PROJECT_ROOT}/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"${PROJECT_ROOT}/.venv/bin/python" -m pip install -r "${PROJECT_ROOT}/requirements.txt"

mkdir -p \
    "${PROJECT_ROOT}/images" \
    "${PROJECT_ROOT}/logs" \
    "${PROJECT_ROOT}/output" \
    "${PROJECT_ROOT}/run"
chmod +x "${SCRIPT_DIR}"/*.sh

"${PROJECT_ROOT}/.venv/bin/python" -m scrapy version -v
echo "Linux environment initialized at ${PROJECT_ROOT}"
