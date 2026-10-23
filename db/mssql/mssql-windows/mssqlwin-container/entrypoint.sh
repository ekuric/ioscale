#!/bin/bash
set -euo pipefail

CONFIG="${CONFIG:-/work/mssql-configwin.yaml}"
TEMPLATES="/work/templates"

# --- OC login (optional) ---
if [[ -n "${KUBEADMIN_PASSWORD:-}" ]]; then
    API_URL="${API_URL:-https://api.ocp.example.com:6443}"
    echo "Logging in to ${API_URL} ..."
    oc login "${API_URL}" \
        --username=kubeadmin \
        --password="${KUBEADMIN_PASSWORD}" \
        --insecure-skip-tls-verify=true
fi

# Normalize OSdisk mode early (env or CLI --osdisk forwarded to mssqlwin)
_osdisk_raw="${USE_OSDISK:-false}"
case "${_osdisk_raw,,}" in
    true|1|yes|on) USE_OSDISK=true ;;
    *) USE_OSDISK=false ;;
esac
for _arg in "$@"; do
    if [[ "${_arg}" == "--osdisk" ]]; then
        USE_OSDISK=true
        break
    fi
done
export USE_OSDISK

# CREATE_VMS / --create-vms
_createvms_raw="${CREATE_VMS:-false}"
case "${_createvms_raw,,}" in
    true|1|yes|on) CREATE_VMS=true ;;
    *) CREATE_VMS=false ;;
esac
for _arg in "$@"; do
    if [[ "${_arg}" == "--create-vms" ]]; then
        CREATE_VMS=true
        break
    fi
done
export CREATE_VMS

CREATE_DB_TEMPLATE="${TEMPLATES}/create_db.sql"
OSDISK_ARGS=()
if [[ "${USE_OSDISK}" == "true" ]]; then
    CREATE_DB_TEMPLATE="${TEMPLATES}/create_dbC.sql"
    OSDISK_ARGS=(--osdisk)
    echo "USE_OSDISK/osdisk active — TPCC on C:\\mssql\\data (skip D: provision)"
fi

CREATE_VMS_ARGS=()
if [[ "${CREATE_VMS}" == "true" ]]; then
    CREATE_VMS_ARGS=(--create-vms)
    echo "CREATE_VMS active — will snapshot/clone Windows VMs before HammerDB"
    if [[ -n "${CLEANUP:-}" ]]; then
        case "${CLEANUP,,}" in
            true|1|yes|on) CREATE_VMS_ARGS+=(--cleanup) ;;
        esac
    fi
    [[ -n "${VMS:-}" ]] && CREATE_VMS_ARGS+=(--vms "${VMS}")
    [[ -n "${VM_PREFIX:-}" ]] && CREATE_VMS_ARGS+=(--vm-prefix "${VM_PREFIX}")
    [[ -n "${VM_CPU:-}" ]] && CREATE_VMS_ARGS+=(--vm-cpu "${VM_CPU}")
    [[ -n "${VM_MEMORY:-}" ]] && CREATE_VMS_ARGS+=(--vm-memory "${VM_MEMORY}")
    [[ -n "${STORAGE_CLASS:-}" ]] && CREATE_VMS_ARGS+=(--storage-class "${STORAGE_CLASS}")
    [[ -n "${ACCESS_MODE:-}" ]] && CREATE_VMS_ARGS+=(--access-mode "${ACCESS_MODE}")
    [[ -n "${WIN_VM_IMAGE:-}" ]] && CREATE_VMS_ARGS+=(--win-vm-image "${WIN_VM_IMAGE}")
    [[ -n "${WIN_ROOT_SIZE:-}" ]] && CREATE_VMS_ARGS+=(--win-root-size "${WIN_ROOT_SIZE}")
    [[ -n "${WIN_DATA_SIZE:-}" ]] && CREATE_VMS_ARGS+=(--win-data-size "${WIN_DATA_SIZE}")
    [[ -n "${GOLDEN_SNAPSHOT:-}" ]] && CREATE_VMS_ARGS+=(--golden-snapshot "${GOLDEN_SNAPSHOT}")
    [[ -n "${ORIG_VM:-}" ]] && CREATE_VMS_ARGS+=(--orig-vm "${ORIG_VM}")
    [[ -n "${SNAPSHOT_CLASS:-}" ]] && CREATE_VMS_ARGS+=(--snapshot-class "${SNAPSHOT_CLASS}")
fi

# Sweep iterations / sleeps / parallelism (optional; CLI overrides YAML)
SWEEP_ARGS=()
[[ -n "${ITERATIONS:-}" ]] && SWEEP_ARGS+=(--iterations "${ITERATIONS}")
[[ -n "${ITERATION_SLEEP:-}" ]] && SWEEP_ARGS+=(--iteration-sleep "${ITERATION_SLEEP}")
[[ -n "${VU_SLEEP:-}" ]] && SWEEP_ARGS+=(--vu-sleep "${VU_SLEEP}")
[[ -n "${MAX_PARALLEL:-}" ]] && SWEEP_ARGS+=(--max-parallel "${MAX_PARALLEL}")
if [[ ${#SWEEP_ARGS[@]} -gt 0 ]]; then
    echo "Sweep/parallel: ${SWEEP_ARGS[*]}"
fi

# PerfMon collectors during HammerDB (optional)
PERFMON_ARGS=()
_perfmon_raw="${PERFMON:-false}"
case "${_perfmon_raw,,}" in
    true|1|yes|on) PERFMON_ARGS+=(--perfmon) ;;
esac
for _arg in "$@"; do
    if [[ "${_arg}" == "--perfmon" ]]; then
        PERFMON_ARGS=(--perfmon)
        break
    fi
done
[[ -n "${PERFMON_COUNTERS_FILE:-}" ]] && PERFMON_ARGS+=(--perfmon-counters-file "${PERFMON_COUNTERS_FILE}")
[[ -n "${PERFMON_SAMPLE_INTERVAL:-}" ]] && PERFMON_ARGS+=(--perfmon-sample-interval "${PERFMON_SAMPLE_INTERVAL}")
[[ -n "${PERFMON_UNTIL_ITERATION:-}" ]] && PERFMON_ARGS+=(--perfmon-until-iteration "${PERFMON_UNTIL_ITERATION}")
_perfmon_no_csv="${PERFMON_NO_EXPORT_CSV:-false}"
case "${_perfmon_no_csv,,}" in
    true|1|yes|on) PERFMON_ARGS+=(--perfmon-no-export-csv) ;;
esac
if [[ ${#PERFMON_ARGS[@]} -gt 0 ]]; then
    echo "PerfMon active: ${PERFMON_ARGS[*]}"
fi

# --- Mode detection ---
if [[ -f "${CONFIG}" ]]; then
    echo "Using config: ${CONFIG}"
else
    echo "No config file at ${CONFIG}, generating from env vars..."

    NAMESPACE="${NAMESPACE:-default}"
    WAREHOUSE_COUNT="${WAREHOUSE_COUNT:-50}"
    BUILD_USERS="${BUILD_USERS:-50}"
    USER_COUNT="${USER_COUNT:-1 10 20 50 100}"
    TEST_DURATION="${TEST_DURATION:-15}"
    MSSQL_TOTAL_ITERATIONS="${MSSQL_TOTAL_ITERATIONS:-10000000}"
    HAMMERDB_PATH="${HAMMERDB_PATH:-C:\\tools\\Hammerdb-4.12}"
    DISK_ID="${DISK_ID:-1}"
    SSH_USER="${SSH_USER:-Administrator}"
    REBUILDDB="${REBUILDDB:-true}"
    REBUILD_ONLY="${REBUILD_ONLY:-false}"
    TEST_ONLY="${TEST_ONLY:-false}"
    REBUILD_ALWAYS="${REBUILD_ALWAYS:-false}"
    DESCRIPTION="${DESCRIPTION:-}"

    HOST_BLOCK=""
    if [[ "${CREATE_VMS}" == "true" ]]; then
        # Hosts come from provision; still need namespace
        HOST_BLOCK="  # hosts filled by --create-vms"
    elif [[ -n "${HOSTS:-}" ]]; then
        HOST_BLOCK="  hosts: \"${HOSTS}\""
    elif [[ -n "${HOST_PATTERN:-}" ]]; then
        HOST_BLOCK="  host_pattern: \"${HOST_PATTERN}\""
    elif [[ -n "${HOST_LABELS:-}" ]]; then
        HOST_BLOCK="  host_labels: \"${HOST_LABELS}\""
    elif [[ -n "${PIN_NODES:-}" ]]; then
        HOST_BLOCK="  host_labels: \"${PIN_NODES}\""
    else
        echo "ERROR: No config file and no host selection." >&2
        echo "Either mount a config file, set HOSTS/HOST_PATTERN/HOST_LABELS," >&2
        echo "or use CREATE_VMS=true with STORAGE_CLASS and WIN_VM_IMAGE/GOLDEN_SNAPSHOT/ORIG_VM." >&2
        exit 1
    fi

    RAMPUP_LINE=""
    if [[ -n "${RAMPUP_TIME:-}" ]]; then
        RAMPUP_LINE=$'\n'"  rampup_time: ${RAMPUP_TIME}"
    fi

    MSSQL_PASS_LINE=""
    if [[ -n "${MSSQL_PASS:-}" ]]; then
        MSSQL_PASS_LINE=$'\n'"  mssql_pass: \"${MSSQL_PASS}\""
    fi

    MAX_SERVER_MEMORY_LINE=""
    if [[ -n "${MAX_SERVER_MEMORY_MB:-}" ]]; then
        MAX_SERVER_MEMORY_LINE=$'\n'"  max_server_memory_mb: ${MAX_SERVER_MEMORY_MB}"
    fi

    ITERATIONS_LINE=""
    if [[ -n "${ITERATIONS:-}" ]]; then
        ITERATIONS_LINE=$'\n'"  iterations: ${ITERATIONS}"
    fi
    ITERATION_SLEEP_LINE=""
    if [[ -n "${ITERATION_SLEEP:-}" ]]; then
        ITERATION_SLEEP_LINE=$'\n'"  iteration_sleep_sec: ${ITERATION_SLEEP}"
    fi
    VU_SLEEP_LINE=""
    if [[ -n "${VU_SLEEP:-}" ]]; then
        VU_SLEEP_LINE=$'\n'"  vu_sleep_sec: ${VU_SLEEP}"
    fi
    MAX_PARALLEL_LINE=""
    if [[ -n "${MAX_PARALLEL:-}" ]]; then
        MAX_PARALLEL_LINE=$'\n'"max_parallel_workers: ${MAX_PARALLEL}"
    fi

    PROVISION_BLOCK=""
    if [[ "${CREATE_VMS}" == "true" ]]; then
        PROVISION_BLOCK=$(cat <<PROVEOF

provision:
  create_vms: true
  vms: ${VMS:-1}
  vm_prefix: "${VM_PREFIX:-mssql-win}"
  vm_cpu: "${VM_CPU:-8}"
  vm_memory: "${VM_MEMORY:-16Gi}"
  storage_class: "${STORAGE_CLASS:-}"
  access_mode: "${ACCESS_MODE:-ReadWriteMany}"
  win_vm_image: "${WIN_VM_IMAGE:-}"
  win_root_volume_size: "${WIN_ROOT_SIZE:-100Gi}"
  win_data_disk_size: "${WIN_DATA_SIZE:-40Gi}"
  golden_snapshot: "${GOLDEN_SNAPSHOT:-}"
  orig_vm: "${ORIG_VM:-}"
  snapshot_class: "${SNAPSHOT_CLASS:-}"
  cleanup: ${CLEANUP:-false}
PROVEOF
)
    fi

    cat > "${CONFIG}" <<EOF
description: "${DESCRIPTION}"

database:
${HOST_BLOCK}
  namespace: "${NAMESPACE}"
  warehouse_count: ${WAREHOUSE_COUNT}
  build_users: ${BUILD_USERS}
  mssql_total_iterations: ${MSSQL_TOTAL_ITERATIONS}
  test_duration: ${TEST_DURATION}${RAMPUP_LINE}
  user_count: "${USER_COUNT}"${MSSQL_PASS_LINE}${MAX_SERVER_MEMORY_LINE}${ITERATIONS_LINE}${ITERATION_SLEEP_LINE}${VU_SLEEP_LINE}
${MAX_PARALLEL_LINE}
windows:
  hammerdb_path: '${HAMMERDB_PATH}'
  test_script: '${HAMMERDB_PATH}\\hammertest.ps1'
  hammerdb_test_script: '${HAMMERDB_PATH}\\mssqls_tprocc_run10.tcl'
  build_schema_file: '${HAMMERDB_PATH}\\mssqls_tprocc_buildschema.tcl'
  result_dir: '${HAMMERDB_PATH}\\results'
  rebuild_script: '${HAMMERDB_PATH}\\rebuild-db.ps1'
  create_db_sql: '${HAMMERDB_PATH}\\create_db.sql'
  disk_id: "${DISK_ID}"
  use_osdisk: ${USE_OSDISK}
  ssh_user: "${SSH_USER}"
  rebuilddb: ${REBUILDDB}
  rebuild_always: ${REBUILD_ALWAYS}
  rebuild_only: ${REBUILD_ONLY}
  test_only: ${TEST_ONLY}
${PROVISION_BLOCK}
EOF

    echo "Generated config:"
fi

cat "${CONFIG}"
echo "---"

# --- Run modular package from /work/results (shim resolves mssqlwin_tests under /work) ---
cd /work/results

exec python3 /work/mssqlwin.py \
    -c "${CONFIG}" \
    --virtctl-only \
    --test-script "${TEMPLATES}/hammerdb-sa-test.ps1" \
    --hammerdb-test-script "${TEMPLATES}/mssqls_tprocc_run.tcl" \
    --build-schema-file "${TEMPLATES}/mssqls_tprocc_buildschema.tcl" \
    --rebuild-script "${TEMPLATES}/rebuild-db.ps1" \
    --create-db "${CREATE_DB_TEMPLATE}" \
    "${OSDISK_ARGS[@]}" \
    "${CREATE_VMS_ARGS[@]}" \
    "${SWEEP_ARGS[@]}" \
    "${PERFMON_ARGS[@]}" \
    "$@"
