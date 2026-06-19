#!/bin/bash
set -euo pipefail

LOG_FILE="${TRIM_PKGVAR:-/tmp}/info.log"
PATHS_FILE="${TRIM_PKGVAR:-/tmp}/accessible_paths.env"
REQ_FILE="${TRIM_APPDEST}/server/requirements.txt"
REQ_HASH_FILE="${TRIM_PKGVAR}/requirements.sha256"
VENV_DIR="${TRIM_PKGHOME}/venv"

init_state() {
    mkdir -p "${TRIM_PKGVAR}" "${TRIM_PKGHOME}"
    touch "${LOG_FILE}"
}

log_msg() {
    local msg
    msg="$(date '+%Y-%m-%d %H:%M:%S') - $1"
    echo "${msg}" | tee -a "${LOG_FILE}" >&2
    if [ -n "${TRIM_TEMP_LOGFILE:-}" ]; then
        echo "${msg}" >> "${TRIM_TEMP_LOGFILE}" 2>&1 || true
    fi
}

save_accessible_paths() {
    local paths="${TRIM_DATA_ACCESSIBLE_PATHS:-}"
    printf 'ACCESSIBLE_PATHS=%q\n' "${paths}" > "${PATHS_FILE}"
    log_msg "已同步授权目录: ${paths:-<empty>}"
}

load_accessible_paths() {
    ACCESSIBLE_PATHS=""
    if [ -f "${PATHS_FILE}" ]; then
        # shellcheck disable=SC1090
        source "${PATHS_FILE}"
    fi
    export ACCESSIBLE_PATHS="${ACCESSIBLE_PATHS:-}"
}

requirements_hash() {
    sha256sum "${REQ_FILE}" | awk '{print $1}'
}

prepare_python_runtime() {
    export PATH="/var/apps/python312/target/bin:$PATH"
    local expected_hash current_hash
    expected_hash="$(requirements_hash)"
    current_hash=""

    if [ -x "${VENV_DIR}/bin/python3" ] && [ -f "${REQ_HASH_FILE}" ]; then
        current_hash="$(cat "${REQ_HASH_FILE}")"
        if [ "${current_hash}" = "${expected_hash}" ]; then
            log_msg "Python 运行时已就绪"
            return 0
        fi
    fi

    log_msg "开始准备 Python 运行时..."
    rm -rf "${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"

    if ! "${VENV_DIR}/bin/python3" -m pip install --disable-pip-version-check --no-cache-dir --upgrade pip setuptools wheel; then
        rm -rf "${VENV_DIR}"
        rm -f "${REQ_HASH_FILE}"
        return 1
    fi

    if ! "${VENV_DIR}/bin/python3" -m pip install --disable-pip-version-check --no-cache-dir -r "${REQ_FILE}"; then
        rm -rf "${VENV_DIR}"
        rm -f "${REQ_HASH_FILE}"
        return 1
    fi

    printf '%s' "${expected_hash}" > "${REQ_HASH_FILE}"
    log_msg "Python 运行时准备完成"
}
