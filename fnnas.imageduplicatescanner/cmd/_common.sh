#!/bin/sh

set -u

if [ -n "${SCRIPT_DIR:-}" ]; then
    PACKAGE_ROOT="${PACKAGE_ROOT:-$(cd "${SCRIPT_DIR}/.." 2>/dev/null && pwd)}"
else
    PACKAGE_ROOT="${PACKAGE_ROOT:-}"
fi

if [ -n "${TRIM_APPDEST:-}" ]; then
    APPDEST_ROOT="${TRIM_APPDEST}"
elif [ -n "${PACKAGE_ROOT}" ]; then
    APPDEST_ROOT="${PACKAGE_ROOT}/app"
else
    APPDEST_ROOT=""
fi

if [ -n "${TRIM_PKGVAR:-}" ]; then
    PKGVAR_ROOT="${TRIM_PKGVAR}"
elif [ -n "${PACKAGE_ROOT}" ]; then
    PKGVAR_ROOT="${PACKAGE_ROOT}/var"
else
    PKGVAR_ROOT="/tmp/fnnas.imageduplicatescanner"
fi

if [ -n "${TRIM_PKGHOME:-}" ]; then
    PKGHOME_ROOT="${TRIM_PKGHOME}"
else
    PKGHOME_ROOT="${PKGVAR_ROOT}"
fi

LOG_FILE="${PKGVAR_ROOT}/info.log"
PATHS_FILE="${PKGVAR_ROOT}/accessible_paths.env"
PID_FILE="${PKGVAR_ROOT}/app.pid"
REQ_FILE="${APPDEST_ROOT}/server/requirements.txt"
REQ_HASH_FILE="${PKGVAR_ROOT}/requirements.sha256"
RUNTIME_INFO_FILE="${PKGVAR_ROOT}/python-runtime.txt"
VENV_DIR="${PKGHOME_ROOT}/venv"
PYTHON_MINOR_REQUIRED="3.11"
PYTHON_DEP_APP="python311"
PYTHON_BIN=""

init_state() {
    mkdir -p "${PKGVAR_ROOT}" "${PKGHOME_ROOT}" 2>/dev/null || return 1
    touch "${LOG_FILE}" 2>/dev/null || return 1
    return 0
}

log_msg() {
    msg="$1"
    timestamp="$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || printf '%s' unknown-time)"
    printf '%s - %s\n' "${timestamp}" "${msg}" >> "${LOG_FILE}" 2>/dev/null || true
    printf '%s - %s\n' "${timestamp}" "${msg}" >&2
    if [ -n "${TRIM_TEMP_LOGFILE:-}" ]; then
        printf '%s - %s\n' "${timestamp}" "${msg}" >> "${TRIM_TEMP_LOGFILE}" 2>/dev/null || true
    fi
}

save_accessible_paths() {
    paths="${TRIM_DATA_ACCESSIBLE_PATHS:-}"
    escaped="$(printf '%s' "${paths}" | sed "s/'/'\\\\''/g")"
    printf "ACCESSIBLE_PATHS='%s'\n" "${escaped}" > "${PATHS_FILE}" 2>/dev/null || return 1
    log_msg "已同步授权目录: ${paths:-<empty>}"
    return 0
}

load_accessible_paths() {
    ACCESSIBLE_PATHS=""
    if [ -f "${PATHS_FILE}" ]; then
        # shellcheck disable=SC1090
        . "${PATHS_FILE}"
    fi
    export ACCESSIBLE_PATHS="${ACCESSIBLE_PATHS:-}"
}

requirements_hash() {
    sha256sum "${REQ_FILE}" | awk '{print $1}'
}

python_version() {
    python_bin="$1"
    "${python_bin}" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'
}

python_supports_venv() {
    python_bin="$1"
    "${python_bin}" -m venv --help >/dev/null 2>&1
}

is_python311() {
    python_bin="$1"
    version="$(python_version "${python_bin}" 2>/dev/null || true)"
    case "${version}" in
        "${PYTHON_MINOR_REQUIRED}".*)
            ;;
        *)
            return 1
            ;;
    esac
    python_supports_venv "${python_bin}"
}

set_python_path() {
    python_bin="$1"
    PYTHON_BIN="${python_bin}"
    export PATH="$(dirname "${python_bin}"):${PATH}"
}

find_python311_runtime() {
    for candidate in \
        /usr/bin/python3.11 \
        /usr/bin/python3 \
        /usr/local/bin/python3.11 \
        /usr/local/bin/python3
    do
        if [ -x "${candidate}" ] && is_python311 "${candidate}"; then
            set_python_path "${candidate}"
            return 0
        fi
    done

    for candidate in python3.11 python3 python; do
        if command -v "${candidate}" >/dev/null 2>&1; then
            resolved="$(command -v "${candidate}")"
            if is_python311 "${resolved}"; then
                set_python_path "${resolved}"
                return 0
            fi
        fi
    done

    candidate="/var/apps/${PYTHON_DEP_APP}/target/bin/python3"
    if [ -x "${candidate}" ] && is_python311 "${candidate}"; then
        set_python_path "${candidate}"
        return 0
    fi

    return 1
}

python_runtime_fingerprint() {
    python_bin="$1"
    printf '%s|%s' "${python_bin}" "$(python_version "${python_bin}")"
}

log_missing_python311() {
    log_msg "未找到可用的 Python ${PYTHON_MINOR_REQUIRED} 运行时。"
    log_msg "本应用会优先使用飞牛系统自带 Python 3.11.2。"
    log_msg "如果你的系统没有内置 Python 3.11，请先安装依赖包 ${PYTHON_DEP_APP}，再重新启动本应用。"
}

ensure_python311_runtime() {
    if find_python311_runtime; then
        log_msg "检测到 Python 运行时: ${PYTHON_BIN} ($(python_version "${PYTHON_BIN}"))"
        return 0
    fi
    log_missing_python311
    return 1
}

prepare_python_runtime() {
    ensure_python311_runtime || return 1

    expected_hash="$(requirements_hash)"
    current_hash=""
    current_runtime=""
    runtime_version="$(python_version "${PYTHON_BIN}")"
    runtime_fingerprint="$(python_runtime_fingerprint "${PYTHON_BIN}")"

    if [ -x "${VENV_DIR}/bin/python3" ] && [ -f "${REQ_HASH_FILE}" ] && [ -f "${RUNTIME_INFO_FILE}" ]; then
        current_hash="$(cat "${REQ_HASH_FILE}" 2>/dev/null || true)"
        current_runtime="$(cat "${RUNTIME_INFO_FILE}" 2>/dev/null || true)"
        if [ "${current_hash}" = "${expected_hash}" ] && [ "${current_runtime}" = "${runtime_fingerprint}" ]; then
            log_msg "Python 运行时已就绪 (${runtime_version})"
            return 0
        fi
    fi

    log_msg "开始准备 Python 运行时 (${runtime_version})..."
    rm -rf "${VENV_DIR}" 2>/dev/null || true
    rm -f "${REQ_HASH_FILE}" "${RUNTIME_INFO_FILE}" 2>/dev/null || true

    "${PYTHON_BIN}" -m venv "${VENV_DIR}" || {
        rm -rf "${VENV_DIR}" 2>/dev/null || true
        return 1
    }

    if ! "${VENV_DIR}/bin/python3" -m pip install --disable-pip-version-check --no-cache-dir --upgrade pip setuptools wheel; then
        rm -rf "${VENV_DIR}" 2>/dev/null || true
        rm -f "${REQ_HASH_FILE}" "${RUNTIME_INFO_FILE}" 2>/dev/null || true
        return 1
    fi

    if ! "${VENV_DIR}/bin/python3" -m pip install --disable-pip-version-check --no-cache-dir -r "${REQ_FILE}"; then
        rm -rf "${VENV_DIR}" 2>/dev/null || true
        rm -f "${REQ_HASH_FILE}" "${RUNTIME_INFO_FILE}" 2>/dev/null || true
        return 1
    fi

    printf '%s' "${expected_hash}" > "${REQ_HASH_FILE}" 2>/dev/null || return 1
    printf '%s' "${runtime_fingerprint}" > "${RUNTIME_INFO_FILE}" 2>/dev/null || return 1
    log_msg "Python 运行时准备完成"
    return 0
}
