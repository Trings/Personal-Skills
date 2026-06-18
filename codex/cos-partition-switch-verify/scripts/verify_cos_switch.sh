#!/usr/bin/env bash
set -euo pipefail

PROJECT_REL="${PROJECT_REL:-COS/projects/cc2560a}"
COSA_BRANCH="${COSA_BRANCH:-dev_4.0}"
COSB_BRANCH="${COSB_BRANCH:-dev_4.0_COSB}"
COSC_BRANCH="${COSC_BRANCH:-dev_4.0_COSC}"
JOBS="${JOBS:-8}"
QEMU_APDU_TIMEOUT="${QEMU_APDU_TIMEOUT:-60}"
MODE="${MODE:-ab}"
DRY_RUN=0

usage() {
    cat <<'EOF'
Usage: verify_cos_switch.sh [--mode ab|abc] [--dry-run]

Modes:
  --mode ab        Simple COSA -> COSB partition switch verification. Default.
  --mode abc       COSA -> COSB -> COSC verification, with business suites after
                  the first and second upgrades.

Convenience aliases:
  --ab             Same as --mode ab.
  --abc, --full,
  --two-step       Same as --mode abc.

Options:
  --dry-run        Resolve paths, branches, and presets without cleaning/building.
  -h, --help       Show this help.

Environment overrides:
  MODE             ab or abc. Command-line --mode takes precedence.
  COSA_ROOT        COSA repo root. Defaults to the current oneos-sim git root.
  COSA_BRANCH      Expected COSA branch. Default: dev_4.0.
  COSB_ROOT        COSB worktree. Defaults to an existing git worktree for the
                  COSB branch, then an existing sibling worktree, then sibling
                  ../dev_4.0_COSB.
  COSB_BRANCH      Branch used when creating COSB worktree. Default: dev_4.0_COSB.
  COSC_ROOT        COSC worktree for --mode abc. Defaults to an existing git
                  worktree for the COSC branch, then an existing sibling
                  worktree, then sibling ../dev_4.0_COSC.
  COSC_BRANCH      Branch used when creating COSC worktree. Default: dev_4.0_COSC.
  PROJECT_REL      Project path below repo root. Default: COS/projects/cc2560a.
  PRESET           CMake preset. Default selection: cc2560a-qemu-local, then cc2560a-qemu.
  JOBS             Parallel build jobs. Default: 8.
  COSA_ELF         Override COSA ELF path.
  COSB_ELF         Override COSB ELF path.
  COSC_ELF         Override COSC ELF path.
  PERSIST_PATH     Simple mode QEMU persistent state path.
  SHARED_LOG       Simple mode shared serial log path.
  AB_PERSIST_PATH  Full mode COSA -> COSB persistent state path.
  ABC_PERSIST_PATH Full mode COSA -> COSB -> COSC persistent state path.
  AB_SHARED_LOG    Full mode COSA -> COSB shared serial log path.
  BC_SHARED_LOG    Full mode COSB -> COSC shared serial log path.
  COSUP_CASE_ROOT  Root of COS upgrade business suites. Default:
                  sibling simmaster_auto_lite/testcase/COS升级.
  FIRST_SUITE_DIR  First post-upgrade suite. Default: COSUP_CASE_ROOT/第一次测试测试.
  SECOND_SUITE_DIR Second post-upgrade suite. Default: COSUP_CASE_ROOT/第二次升级测试.
  SUITE1_LOG       First suite runner log path.
  SUITE2_LOG       Second suite runner log path.
  QEMU_APDU_TIMEOUT
                  APDU timeout for business suites. Default: 60.
  ALLOW_BRANCH_MISMATCH=1
                  Continue even when COSA/COSB/COSC are not on expected branches.
EOF
}

log() {
    printf '\n[cos-switch] %s\n' "$*"
}

die() {
    printf '[cos-switch] %s\n' "$*" >&2
    exit 1
}

require_dir() {
    if [[ ! -d "$1" ]]; then
        die "missing directory: $1"
    fi
}

require_file() {
    if [[ ! -f "$1" ]]; then
        die "missing file: $1"
    fi
}

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        die "missing command: $1"
    fi
}

abs_path() {
    realpath -m "$1"
}

is_git_worktree() {
    [[ -d "$1/.git" || -f "$1/.git" ]]
}

find_cosa_root() {
    local start root dir
    start="${1:-$PWD}"

    if root="$(git -C "${start}" rev-parse --show-toplevel 2>/dev/null)"; then
        if [[ -f "${root}/Script/qemu/cos_partition_switch.py" && -d "${root}/${PROJECT_REL}" ]]; then
            abs_path "${root}"
            return 0
        fi
    fi

    dir="$(cd "${start}" && pwd -P)"
    while [[ "${dir}" != "/" ]]; do
        if [[ -f "${dir}/Script/qemu/cos_partition_switch.py" && -d "${dir}/${PROJECT_REL}" ]]; then
            abs_path "${dir}"
            return 0
        fi
        dir="$(dirname "${dir}")"
    done

    return 1
}

current_branch() {
    git -C "$1" branch --show-current 2>/dev/null || true
}

find_worktree_by_branch() {
    local git_root branch list_path list_branch
    git_root="$1"
    branch="$2"

    while IFS=$'\t' read -r list_path list_branch; do
        if [[ "${list_branch}" == "${branch}" ]] && is_git_worktree "${list_path}"; then
            abs_path "${list_path}"
            return 0
        fi
    done < <(git -C "${git_root}" worktree list --porcelain 2>/dev/null | awk '
        /^worktree / { path = substr($0, 10) }
        /^branch refs\/heads\// { print path "\t" substr($0, 19) }
    ')

    return 1
}

normalize_cosa_root() {
    local root branch sibling worktree_root
    root="$1"
    branch="$(current_branch "${root}")"

    if [[ -z "${branch}" || "${branch}" == "${COSB_BRANCH}" || "${branch}" == "${COSC_BRANCH}" ||
          "$(basename "${root}")" == "${COSB_BRANCH}" || "$(basename "${root}")" == "${COSC_BRANCH}" ||
          "$(basename "${root}")" == "oneos-sim-cosup-COSB" || "$(basename "${root}")" == "oneos-sim-cosup-COSC" ]]; then
        if worktree_root="$(find_worktree_by_branch "${root}" "${COSA_BRANCH}")"; then
            printf '%s\n' "${worktree_root}"
            return 0
        fi

        sibling="$(abs_path "${root}/../oneos-sim")"
        if [[ "${sibling}" != "${root}" && -f "${sibling}/Script/qemu/cos_partition_switch.py" && -d "${sibling}/${PROJECT_REL}" ]]; then
            root="${sibling}"
        fi
    fi

    printf '%s\n' "${root}"
}

validate_branch() {
    local label root expected actual
    label="$1"
    root="$2"
    expected="$3"
    actual="$(current_branch "${root}")"

    if [[ -z "${actual}" ]]; then
        log "${label} is detached; expected branch ${expected}"
        return 0
    fi

    if [[ "${actual}" != "${expected}" ]]; then
        if [[ "${ALLOW_BRANCH_MISMATCH:-0}" == "1" ]]; then
            log "${label} branch ${actual}; expected ${expected}; continuing because ALLOW_BRANCH_MISMATCH=1"
        else
            die "${label} branch is ${actual}, expected ${expected}. Set ${label}_ROOT correctly or ALLOW_BRANCH_MISMATCH=1."
        fi
    fi
}

infer_worktree_root() {
    local label branch parent candidate worktree_root
    label="$1"
    branch="$2"
    parent="$(dirname "${COSA_ROOT}")"

    if worktree_root="$(find_worktree_by_branch "${COSA_ROOT}" "${branch}")"; then
        printf '%s\n' "${worktree_root}"
        return 0
    fi

    for candidate in \
        "${parent}/${branch}" \
        "${parent}/oneos-sim-cosup-${label}" \
        "${parent}/oneos-sim-${label}" \
        "${parent}/${label}" \
        "${parent}/oneos-sim_${label}"; do
        if is_git_worktree "${candidate}"; then
            abs_path "${candidate}"
            return 0
        fi
    done

    abs_path "${parent}/${branch}"
}

add_worktree() {
    local root branch
    root="$1"
    branch="$2"

    if git -C "${COSA_ROOT}" show-ref --verify --quiet "refs/heads/${branch}"; then
        git -C "${COSA_ROOT}" worktree add "${root}" "${branch}"
    elif git -C "${COSA_ROOT}" show-ref --verify --quiet "refs/remotes/origin/${branch}"; then
        git -C "${COSA_ROOT}" worktree add -b "${branch}" "${root}" "origin/${branch}"
    else
        git -C "${COSA_ROOT}" worktree add "${root}" "${branch}"
    fi
}

cmake_has_configure_preset() {
    (cd "$1" && cmake --list-presets 2>/dev/null | grep -Fq "\"$2\"")
}

cmake_has_build_preset() {
    (cd "$1" && cmake --build --list-presets 2>/dev/null | grep -Fq "\"$2\"")
}

select_preset() {
    local project_dir candidate
    project_dir="$1"

    if [[ -n "${PRESET:-}" ]]; then
        if ! cmake_has_configure_preset "${project_dir}" "${PRESET}"; then
            die "configure preset not found: ${PRESET}"
        fi
        if ! cmake_has_build_preset "${project_dir}" "${PRESET}"; then
            die "build preset not found: ${PRESET}"
        fi
        printf '%s\n' "${PRESET}"
        return 0
    fi

    for candidate in cc2560a-qemu-local cc2560a-qemu; do
        if cmake_has_configure_preset "${project_dir}" "${candidate}" && cmake_has_build_preset "${project_dir}" "${candidate}"; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done

    die "no usable CMake preset found; create ${project_dir}/CMakeUserPresets.json or set PRESET"
}

scan_logs() {
    local pattern rc target file
    local logs=()
    local dir_logs=()
    local -A seen=()
    pattern="Exception|Assert failed|Load access fault|Store access fault|qzvm_main init error|\\[xc\\] FAIL|failed=[1-9][0-9]*|ERROR|Error|error|Fault|fault"

    for target in "$@"; do
        if [[ -d "${target}" ]]; then
            shopt -s nullglob
            dir_logs=("${target}"/*.log)
            shopt -u nullglob
            for file in "${dir_logs[@]}"; do
                if [[ -z "${seen[${file}]:-}" ]]; then
                    logs+=("${file}")
                    seen["${file}"]=1
                fi
            done
        elif [[ -f "${target}" ]]; then
            if [[ -z "${seen[${target}]:-}" ]]; then
                logs+=("${target}")
                seen["${target}"]=1
            fi
        fi
    done

    if (( ${#logs[@]} == 0 )); then
        die "no log files found to scan"
    fi

    set +e
    if command -v rg >/dev/null 2>&1; then
        rg -n "${pattern}" "${logs[@]}"
    else
        grep -RInE "${pattern}" "${logs[@]}"
    fi
    rc=$?
    set -e

    if [[ "${rc}" -gt 1 ]]; then
        die "log scan failed with exit code ${rc}"
    fi

    return "${rc}"
}

sync_user_preset() {
    local root label
    root="$1"
    label="$2"

    if [[ -f "${COSA_ROOT}/${PROJECT_REL}/CMakeUserPresets.json" ]]; then
        log "sync machine-local CMakeUserPresets.json to ${label}"
        cp "${COSA_ROOT}/${PROJECT_REL}/CMakeUserPresets.json" "${root}/${PROJECT_REL}/CMakeUserPresets.json"
    fi
}

check_selected_preset() {
    local root label
    root="$1"
    label="$2"

    if [[ -n "${PRESET:-}" ]]; then
        select_preset "${root}/${PROJECT_REL}" >/dev/null
    elif ! cmake_has_configure_preset "${root}/${PROJECT_REL}" "${BUILD_PRESET}" || ! cmake_has_build_preset "${root}/${PROJECT_REL}" "${BUILD_PRESET}"; then
        die "selected preset ${BUILD_PRESET} is not available for ${label}"
    fi
}

print_repo_status() {
    local label root
    label="$1"
    root="$2"

    log "${label} branch/commit"
    git -C "${root}" status --short --branch
    git -C "${root}" rev-parse --short HEAD
}

print_path() {
    printf '[cos-switch] %-12s: %s\n' "$1" "$2"
}

build_repo() {
    local label root
    label="$1"
    root="$2"

    log "build ${label}"
    (
        cd "${root}/${PROJECT_REL}"
        cmake --preset "${BUILD_PRESET}"
        cmake --build --preset "${BUILD_PRESET}" --parallel "${JOBS}"
    )
}

run_partition_switch() {
    local label from_slot to_slot cosa_elf cosb_elf persist_path shared_log
    label="$1"
    from_slot="$2"
    to_slot="$3"
    cosa_elf="$4"
    cosb_elf="$5"
    persist_path="$6"
    shared_log="$7"
    shift 7

    log "run ${label} partition switch verification"
    mkdir -p "$(dirname "${persist_path}")"
    mkdir -p "$(dirname "${shared_log}")"
    rm -f "${shared_log}"
    python3 "${COSA_ROOT}/Script/qemu/cos_partition_switch.py" \
        --from "${from_slot}" \
        --to "${to_slot}" \
        --cosa-elf "${cosa_elf}" \
        --cosb-elf "${cosb_elf}" \
        --persist-path "${persist_path}" \
        --shared-log "${shared_log}" \
        "$@"
}

run_business_suite() {
    local label elf persist suite_dir suite_log summary
    label="$1"
    elf="$2"
    persist="$3"
    suite_dir="$4"
    suite_log="$5"

    require_file "${elf}"
    require_file "${persist}"
    require_dir "${suite_dir}"

    log "run ${label} business suite"
    mkdir -p "$(dirname "${suite_log}")"
    set +e
    ONEOS_QEMU_ELF_PATH="${elf}" \
    ONEOS_QEMU_PERSIST_PATH="${persist}" \
    ONEOS_QEMU_KEEP_STATE=1 \
    ONEOS_QEMU_APDU_TIMEOUT="${QEMU_APDU_TIMEOUT}" \
    python3 "${COSA_ROOT}/Script/qemu/run_xc_script.py" --keep-going -i "${suite_dir}" >"${suite_log}" 2>&1
    rc=$?
    set -e

    if [[ "${rc}" -ne 0 ]]; then
        tail -n 80 "${suite_log}" >&2 || true
        die "${label} business suite failed; see ${suite_log}"
    fi

    summary="$(grep -E '^\[xc\] total=[0-9]+ passed=[0-9]+ failed=[0-9]+' "${suite_log}" | tail -n 1 || true)"
    if [[ -z "${summary}" ]]; then
        tail -n 80 "${suite_log}" >&2 || true
        die "${label} business suite did not print a summary; see ${suite_log}"
    fi
    if [[ "${summary}" != *" failed=0" ]]; then
        tail -n 80 "${suite_log}" >&2 || true
        die "${label} business suite has failures: ${summary}"
    fi

    printf '[cos-switch] %s summary: %s\n' "${label}" "${summary}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            [[ $# -ge 2 ]] || die "--mode requires ab or abc"
            MODE="$2"
            shift 2
            ;;
        --mode=*)
            MODE="${1#--mode=}"
            shift
            ;;
        --ab)
            MODE="ab"
            shift
            ;;
        --abc|--full|--two-step)
            MODE="abc"
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done

case "${MODE}" in
    ab|abc)
        ;;
    *)
        die "unknown mode: ${MODE}; expected ab or abc"
        ;;
esac

require_cmd git
require_cmd cmake
require_cmd python3
require_cmd realpath

if [[ -n "${COSA_ROOT:-}" ]]; then
    COSA_ROOT="$(abs_path "${COSA_ROOT}")"
else
    COSA_ROOT="$(find_cosa_root "${PWD}")" || die "cannot infer COSA repo root; run from inside oneos-sim or set COSA_ROOT"
fi
COSA_ROOT="$(normalize_cosa_root "${COSA_ROOT}")"

if [[ -n "${COSB_ROOT:-}" ]]; then
    COSB_ROOT="$(abs_path "${COSB_ROOT}")"
else
    COSB_ROOT="$(infer_worktree_root COSB "${COSB_BRANCH}")"
fi

if [[ "${MODE}" == "abc" ]]; then
    if [[ -n "${COSC_ROOT:-}" ]]; then
        COSC_ROOT="$(abs_path "${COSC_ROOT}")"
    else
        COSC_ROOT="$(infer_worktree_root COSC "${COSC_BRANCH}")"
    fi
fi

LOG_DIR="${COSA_ROOT}/build/cosup"
COSA_ELF="${COSA_ELF:-${COSA_ROOT}/build/cc2560a_qemu/bin/cc2560a_qemu.elf}"
COSB_ELF="${COSB_ELF:-${COSB_ROOT}/build/cc2560a_qemu/bin/cc2560a_qemu.elf}"
PERSIST_PATH="${PERSIST_PATH:-${LOG_DIR}/cos_ab_test.bin}"
SHARED_LOG="${SHARED_LOG:-${LOG_DIR}/cos_ab_test.log}"
AB_PERSIST_PATH="${AB_PERSIST_PATH:-${PERSIST_PATH}}"
ABC_PERSIST_PATH="${ABC_PERSIST_PATH:-${LOG_DIR}/cos_abc_test.bin}"
AB_SHARED_LOG="${AB_SHARED_LOG:-${LOG_DIR}/cosa_to_cosb_test.log}"
BC_SHARED_LOG="${BC_SHARED_LOG:-${LOG_DIR}/cosb_to_cosc_test.log}"
COSUP_CASE_ROOT="${COSUP_CASE_ROOT:-$(abs_path "${COSA_ROOT}/../simmaster_auto_lite/testcase/COS升级")}"
FIRST_SUITE_DIR="${FIRST_SUITE_DIR:-${COSUP_CASE_ROOT}/第一次测试测试}"
SECOND_SUITE_DIR="${SECOND_SUITE_DIR:-${COSUP_CASE_ROOT}/第二次升级测试}"
SUITE1_LOG="${SUITE1_LOG:-${LOG_DIR}/suite1_runner.log}"
SUITE2_LOG="${SUITE2_LOG:-${LOG_DIR}/suite2_runner.log}"

if [[ "${MODE}" == "abc" ]]; then
    COSC_ELF="${COSC_ELF:-${COSC_ROOT}/build/cc2560a_qemu/bin/cc2560a_qemu.elf}"
fi

require_dir "${COSA_ROOT}"
require_dir "${COSA_ROOT}/${PROJECT_REL}"
require_file "${COSA_ROOT}/Script/qemu/cos_partition_switch.py"
if [[ "${MODE}" == "abc" ]]; then
    require_file "${COSA_ROOT}/Script/qemu/run_xc_script.py"
fi
validate_branch COSA "${COSA_ROOT}" "${COSA_BRANCH}"

if ! is_git_worktree "${COSB_ROOT}"; then
    if [[ "${DRY_RUN}" == "1" ]]; then
        log "COSB worktree missing; would create ${COSB_ROOT} from ${COSB_BRANCH}"
    else
        log "creating COSB worktree ${COSB_ROOT} from ${COSB_BRANCH}"
        add_worktree "${COSB_ROOT}" "${COSB_BRANCH}"
    fi
fi

if [[ "${MODE}" == "abc" ]] && ! is_git_worktree "${COSC_ROOT}"; then
    if [[ "${DRY_RUN}" == "1" ]]; then
        log "COSC worktree missing; would create ${COSC_ROOT} from ${COSC_BRANCH}"
    else
        log "creating COSC worktree ${COSC_ROOT} from ${COSC_BRANCH}"
        add_worktree "${COSC_ROOT}" "${COSC_BRANCH}"
    fi
fi

if [[ "${DRY_RUN}" == "0" || -e "${COSB_ROOT}" ]]; then
    require_dir "${COSB_ROOT}"
    require_dir "${COSB_ROOT}/${PROJECT_REL}"
    validate_branch COSB "${COSB_ROOT}" "${COSB_BRANCH}"
fi

if [[ "${MODE}" == "abc" && ( "${DRY_RUN}" == "0" || -e "${COSC_ROOT}" ) ]]; then
    require_dir "${COSC_ROOT}"
    require_dir "${COSC_ROOT}/${PROJECT_REL}"
    validate_branch COSC "${COSC_ROOT}" "${COSC_BRANCH}"
fi

print_repo_status COSA "${COSA_ROOT}"
if is_git_worktree "${COSB_ROOT}"; then
    print_repo_status COSB "${COSB_ROOT}"
fi
if [[ "${MODE}" == "abc" ]] && is_git_worktree "${COSC_ROOT}"; then
    print_repo_status COSC "${COSC_ROOT}"
fi

BUILD_PRESET="$(select_preset "${COSA_ROOT}/${PROJECT_REL}")"
log "using CMake preset ${BUILD_PRESET}"

if [[ "${DRY_RUN}" == "1" ]]; then
    log "resolved paths"
    print_path "mode" "${MODE}"
    print_path "COSA root" "${COSA_ROOT}"
    print_path "COSB root" "${COSB_ROOT}"
    print_path "COSA ELF" "${COSA_ELF}"
    print_path "COSB ELF" "${COSB_ELF}"
    if [[ "${MODE}" == "abc" ]]; then
        print_path "COSC root" "${COSC_ROOT}"
        print_path "COSC ELF" "${COSC_ELF}"
        print_path "AB persist" "${AB_PERSIST_PATH}"
        print_path "ABC persist" "${ABC_PERSIST_PATH}"
        print_path "AB log" "${AB_SHARED_LOG}"
        print_path "BC log" "${BC_SHARED_LOG}"
        print_path "suite1" "${FIRST_SUITE_DIR}"
        print_path "suite2" "${SECOND_SUITE_DIR}"
        print_path "suite1 log" "${SUITE1_LOG}"
        print_path "suite2 log" "${SUITE2_LOG}"
        log "would clean, build COSA/COSB/COSC, run A->B, run suite1, run B->C, run suite2"
    else
        print_path "persist" "${PERSIST_PATH}"
        print_path "log" "${SHARED_LOG}"
        log "would clean, build COSA/COSB, and run A->B"
    fi
    if [[ -f "${COSA_ROOT}/${PROJECT_REL}/CMakeUserPresets.json" ]]; then
        log "would sync machine-local CMakeUserPresets.json to required worktrees"
    else
        log "CMakeUserPresets.json not found; would use shared presets and toolchain from PATH"
    fi
    log "dry run complete; no files cleaned, built, or copied"
    exit 0
fi

if [[ -f "${COSA_ROOT}/${PROJECT_REL}/CMakeUserPresets.json" ]]; then
    sync_user_preset "${COSB_ROOT}" COSB
    if [[ "${MODE}" == "abc" ]]; then
        sync_user_preset "${COSC_ROOT}" COSC
    fi
else
    log "CMakeUserPresets.json not found; using shared presets and toolchain from PATH"
fi

check_selected_preset "${COSB_ROOT}" COSB
if [[ "${MODE}" == "abc" ]]; then
    check_selected_preset "${COSC_ROOT}" COSC
fi

log "resolved paths"
print_path "mode" "${MODE}"
print_path "COSA root" "${COSA_ROOT}"
print_path "COSB root" "${COSB_ROOT}"
print_path "COSA ELF" "${COSA_ELF}"
print_path "COSB ELF" "${COSB_ELF}"
if [[ "${MODE}" == "abc" ]]; then
    print_path "COSC root" "${COSC_ROOT}"
    print_path "COSC ELF" "${COSC_ELF}"
    print_path "AB persist" "${AB_PERSIST_PATH}"
    print_path "ABC persist" "${ABC_PERSIST_PATH}"
    print_path "AB log" "${AB_SHARED_LOG}"
    print_path "BC log" "${BC_SHARED_LOG}"
    print_path "suite1" "${FIRST_SUITE_DIR}"
    print_path "suite2" "${SECOND_SUITE_DIR}"
    print_path "suite1 log" "${SUITE1_LOG}"
    print_path "suite2 log" "${SUITE2_LOG}"
else
    print_path "persist" "${PERSIST_PATH}"
    print_path "log" "${SHARED_LOG}"
fi

log "clean generated build/test directories"
rm -rf "${COSA_ROOT}/build/cc2560a_qemu" "${COSA_ROOT}/build/cosup"
rm -rf "${COSB_ROOT}/build/cc2560a_qemu" "${COSB_ROOT}/build/cosup"
if [[ "${MODE}" == "abc" ]]; then
    rm -rf "${COSC_ROOT}/build/cc2560a_qemu" "${COSC_ROOT}/build/cosup"
fi

build_repo COSA "${COSA_ROOT}"
build_repo COSB "${COSB_ROOT}"
if [[ "${MODE}" == "abc" ]]; then
    build_repo COSC "${COSC_ROOT}"
fi

require_file "${COSA_ELF}"
require_file "${COSB_ELF}"
if [[ "${MODE}" == "abc" ]]; then
    require_file "${COSC_ELF}"
fi

if [[ "${MODE}" == "ab" ]]; then
    rm -f "${PERSIST_PATH}" "${SHARED_LOG}"
    run_partition_switch "A -> B" A B "${COSA_ELF}" "${COSB_ELF}" "${PERSIST_PATH}" "${SHARED_LOG}"
else
    rm -f "${AB_PERSIST_PATH}" "${ABC_PERSIST_PATH}" "${AB_SHARED_LOG}" "${BC_SHARED_LOG}" "${SUITE1_LOG}" "${SUITE2_LOG}"
    run_partition_switch "A -> B" A B "${COSA_ELF}" "${COSB_ELF}" "${AB_PERSIST_PATH}" "${AB_SHARED_LOG}"
    run_business_suite "first post-upgrade" "${COSB_ELF}" "${AB_PERSIST_PATH}" "${FIRST_SUITE_DIR}" "${SUITE1_LOG}"

    log "copy A -> B persistent state for B -> C"
    cp "${AB_PERSIST_PATH}" "${ABC_PERSIST_PATH}"

    run_partition_switch "B -> C" B A "${COSC_ELF}" "${COSB_ELF}" "${ABC_PERSIST_PATH}" "${BC_SHARED_LOG}" --skip-init
    run_business_suite "second post-upgrade" "${COSC_ELF}" "${ABC_PERSIST_PATH}" "${SECOND_SUITE_DIR}" "${SUITE2_LOG}"
fi

log "scan logs for exception/assert/fault keywords"
if [[ "${MODE}" == "abc" ]]; then
    SCAN_TARGETS=("${LOG_DIR}" "${AB_SHARED_LOG}" "${BC_SHARED_LOG}" "${SUITE1_LOG}" "${SUITE2_LOG}")
else
    SCAN_TARGETS=("${LOG_DIR}" "${SHARED_LOG}")
fi

if scan_logs "${SCAN_TARGETS[@]}"; then
    printf '[cos-switch] warning: suspicious log keywords found\n' >&2
    exit 2
fi

log "verification passed"
printf '[cos-switch] COSA ELF: %s\n' "${COSA_ELF}"
printf '[cos-switch] COSB ELF: %s\n' "${COSB_ELF}"
if [[ "${MODE}" == "abc" ]]; then
    printf '[cos-switch] COSC ELF: %s\n' "${COSC_ELF}"
    printf '[cos-switch] AB persist : %s\n' "${AB_PERSIST_PATH}"
    printf '[cos-switch] ABC persist: %s\n' "${ABC_PERSIST_PATH}"
    printf '[cos-switch] AB log     : %s\n' "${AB_SHARED_LOG}"
    printf '[cos-switch] BC log     : %s\n' "${BC_SHARED_LOG}"
    printf '[cos-switch] suite1 log : %s\n' "${SUITE1_LOG}"
    printf '[cos-switch] suite2 log : %s\n' "${SUITE2_LOG}"
else
    printf '[cos-switch] persist : %s\n' "${PERSIST_PATH}"
    printf '[cos-switch] log     : %s\n' "${SHARED_LOG}"
fi
