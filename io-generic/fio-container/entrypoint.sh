#!/bin/bash
set -euo pipefail

CONFIG="${CONFIG:-/work/fio-config.yaml}"

# --- Kubeconfig check (required for --virtctl-only) ---
KUBECONFIG="${KUBECONFIG:-/root/.kube/config}"
export KUBECONFIG
if [[ ! -s "${KUBECONFIG}" ]]; then
    echo "ERROR: kubeconfig missing or empty at ${KUBECONFIG}" >&2
    echo "Mount the bastion kubeconfig, e.g.:" >&2
    echo "  -v /root/.kube:/root/.kube:ro,Z -e KUBECONFIG=/root/.kube/config" >&2
    ls -la /root/.kube 2>/dev/null || echo "( /root/.kube not present )" >&2
    exit 1
fi
echo "Using kubeconfig: ${KUBECONFIG}"
if ! oc whoami 2>/dev/null; then
    echo "ERROR: oc cannot authenticate with ${KUBECONFIG}" >&2
    echo "On the bastion verify: ls -la /root/.kube/config && oc whoami" >&2
    echo "If SELinux is enforcing, remount with :Z (e.g. -v /root/.kube:/root/.kube:ro,Z)" >&2
    oc whoami 2>&1 || true
    exit 1
fi
echo "Cluster identity: $(oc whoami 2>/dev/null)  server=$(oc whoami --show-server 2>/dev/null || true)"

# --- OC login (optional) ---
if [[ -n "${KUBEADMIN_PASSWORD:-}" ]]; then
    API_URL="${API_URL:-https://api.ocp.example.com:6443}"
    echo "Logging in to ${API_URL} ..."
    oc login "${API_URL}" \
        --username=kubeadmin \
        --password="${KUBEADMIN_PASSWORD}" \
        --insecure-skip-tls-verify=true
fi

# --- Mode detection ---
if [[ -f "${CONFIG}" ]]; then
    # Mode 1: config file provided -- use it as-is
    echo "Using config: ${CONFIG}"
else
    # Mode 2/3: no config file -- generate from env vars
    echo "No config file at ${CONFIG}, generating from env vars..."

    NAMESPACE="${NAMESPACE:-default}"
    DESCRIPTION="${DESCRIPTION:-}"
    MIGRATE_WORKLOADS="${MIGRATE_WORKLOADS:-}"
    MIGRATE_INTERVAL="${MIGRATE_INTERVAL:-0}"
    RETRY_INTERVAL="${RETRY_INTERVAL:-30}"
    MAX_RETRIES="${MAX_RETRIES:-10}"
    MONITOR_INTERVAL="${MONITOR_INTERVAL:-60}"
    MONITOR_VM="${MONITOR_VM:-false}"
    MONITOR_VM_INTERVAL="${MONITOR_VM_INTERVAL:-10}"
    MAX_WORKERS="${MAX_WORKERS:-}"

    # Detect what's requested
    HAS_LINUX=false
    HAS_WINDOWS=false

    if [[ -n "${HOSTS:-}" || -n "${HOST_PATTERN:-}" || -n "${HOST_LABELS:-}" ]]; then
        HAS_LINUX=true
    fi
    if [[ -n "${WIN_HOSTS:-}" || -n "${WIN_HOST_PATTERN:-}" ]]; then
        HAS_WINDOWS=true
    fi

    if [[ "${HAS_LINUX}" == "false" && "${HAS_WINDOWS}" == "false" ]]; then
        echo "ERROR: No config file and no host selection." >&2
        echo "Either mount a config file:" >&2
        echo "  -v ./fio-config.yaml:/work/fio-config.yaml" >&2
        echo "Or set one of:" >&2
        echo "  Linux:   -e HOSTS=... / -e HOST_PATTERN=... / -e HOST_LABELS=..." >&2
        echo "  Windows: -e WIN_HOSTS=... / -e WIN_HOST_PATTERN=..." >&2
        exit 1
    fi

    # ---- Build Linux sections (if Linux hosts requested) ----
    LINUX_BLOCK=""
    if [[ "${HAS_LINUX}" == "true" ]]; then
        TEST_SIZE="${TEST_SIZE:-1G}"
        # Unset → 300 (legacy default). Empty/null/0 → omit runtime (size-based, full --size).
        RUNTIME="${RUNTIME-300}"
        BLOCK_SIZES="${BLOCK_SIZES:-4k 8k 128k}"
        IO_PATTERNS="${IO_PATTERNS:-read write randread randwrite}"
        NUMJOBS="${NUMJOBS:-4}"
        IODEPTH="${IODEPTH:-16}"
        IOENGINE="${IOENGINE:-libaio}"
        DIRECT_IO="${DIRECT_IO:-1}"
        FIO_INSTALLED="${FIO_INSTALLED:-false}"
        MOUNT_POINT="${MOUNT_POINT:-/root/tests/data}"
        FILESYSTEM="${FILESYSTEM:-xfs}"
        PERSISTENT="${PERSISTENT:-}"
        OUTPUT_DIR="${OUTPUT_DIR:-/root/fio-results}"
        OUTPUT_FORMAT="${OUTPUT_FORMAT:-json+}"

        HOST_BLOCK=""
        if [[ -n "${HOSTS:-}" ]]; then
            HOST_BLOCK="  hosts: \"${HOSTS}\""
        elif [[ -n "${HOST_PATTERN:-}" ]]; then
            HOST_BLOCK="  host_pattern: \"${HOST_PATTERN}\""
        elif [[ -n "${HOST_LABELS:-}" ]]; then
            HOST_BLOCK="  host_labels: \"${HOST_LABELS}\""
        fi

        if [[ -z "${DEVICES:-}" ]]; then
            echo "ERROR: DEVICES is required for Linux hosts (e.g. DEVICES=\"vm-{1..10}=vdc\")" >&2
            exit 1
        fi

        DEVICE_BLOCK=""
        IFS=',' read -ra DEVICE_PAIRS <<< "${DEVICES}"
        for pair in "${DEVICE_PAIRS[@]}"; do
            pattern="${pair%%=*}"
            device="${pair#*=}"
            pattern="$(echo "${pattern}" | xargs)"
            device="$(echo "${device}" | xargs)"
            DEVICE_BLOCK="${DEVICE_BLOCK}    \"${pattern}\": \"${device}\"
"
        done

        case "${FIO_INSTALLED,,}" in
            true|1|yes) FIO_INSTALLED_VALUE=true ;;
            *) FIO_INSTALLED_VALUE=false ;;
        esac

        RATE_IOPS_LINE=""
        if [[ -n "${RATE_IOPS:-}" ]]; then
            RATE_IOPS_LINE=$'\n'"  rate_iops: \"${RATE_IOPS}\""
        fi

        RUNTIME_LINE=""
        if [[ -n "${RUNTIME}" && "${RUNTIME}" != "null" && "${RUNTIME}" != "0" ]]; then
            RUNTIME_LINE=$'\n'"  runtime: \"${RUNTIME}\""
        else
            echo "Linux RUNTIME empty/null/0 — size-based mode (write full --size, no --time_based)"
        fi

        LINUX_BLOCK="vm:
${HOST_BLOCK}
  namespace: \"${NAMESPACE}\"

storage:
  devices:
${DEVICE_BLOCK}  mount_point: \"${MOUNT_POINT}\"
  filesystem: \"${FILESYSTEM}\"
  persistent: \"${PERSISTENT}\"

fio:
  test_size: \"${TEST_SIZE}\"${RUNTIME_LINE}
  block_sizes: \"${BLOCK_SIZES}\"
  io_patterns: \"${IO_PATTERNS}\"
  numjobs: \"${NUMJOBS}\"
  iodepth: \"${IODEPTH}\"
  ioengine: \"${IOENGINE}\"
  direct_io: \"${DIRECT_IO}\"
  fio_installed: ${FIO_INSTALLED_VALUE}${RATE_IOPS_LINE}

output:
  directory: \"${OUTPUT_DIR}\"
  format: \"${OUTPUT_FORMAT}\""
    fi

    # ---- Build Windows section (if Windows hosts requested) ----
    WINDOWS_BLOCK=""
    if [[ "${HAS_WINDOWS}" == "true" ]]; then
        WIN_RUN_DIR="${WIN_RUN_DIR:-d:/fio}"
        WIN_TEST_SIZE="${WIN_TEST_SIZE:-10GB}"
        # Unset → 600 (legacy default). Empty/null/0 → omit runtime (size-based).
        WIN_RUNTIME="${WIN_RUNTIME-600}"
        WIN_BLOCK_SIZES="${WIN_BLOCK_SIZES:-4k 8k 128k}"
        WIN_IO_PATTERNS="${WIN_IO_PATTERNS:-randread randwrite read write}"
        WIN_NUMJOBS="${WIN_NUMJOBS:-8}"
        WIN_IODEPTH="${WIN_IODEPTH:-16}"
        WIN_DIRECT_IO="${WIN_DIRECT_IO:-1}"
        WIN_MOUNT_POINT="${WIN_MOUNT_POINT:-d\\:/fio/data}"
        WIN_OUTPUT_DIR="${WIN_OUTPUT_DIR:-d:/fio/results}"
        WIN_OUTPUT_FORMAT="${WIN_OUTPUT_FORMAT:-json+}"

        WIN_HOST_BLOCK=""
        if [[ -n "${WIN_HOSTS:-}" ]]; then
            WIN_HOST_BLOCK="  hosts: \"${WIN_HOSTS}\""
        elif [[ -n "${WIN_HOST_PATTERN:-}" ]]; then
            WIN_HOST_BLOCK="  host_pattern: \"${WIN_HOST_PATTERN}\""
        fi

        if [[ -z "${WIN_DEVICES:-}" ]]; then
            echo "ERROR: WIN_DEVICES is required for Windows hosts (e.g. WIN_DEVICES=\"win-vm-{1..10}=1\")" >&2
            exit 1
        fi

        WIN_DEVICE_BLOCK=""
        IFS=',' read -ra WIN_DEVICE_PAIRS <<< "${WIN_DEVICES}"
        for pair in "${WIN_DEVICE_PAIRS[@]}"; do
            pattern="${pair%%=*}"
            device="${pair#*=}"
            pattern="$(echo "${pattern}" | xargs)"
            device="$(echo "${device}" | xargs)"
            WIN_DEVICE_BLOCK="${WIN_DEVICE_BLOCK}      \"${pattern}\": \"${device}\"
"
        done

        WIN_RATE_IOPS_LINE=""
        if [[ -n "${WIN_RATE_IOPS:-}" ]]; then
            WIN_RATE_IOPS_LINE=$'\n'"    rate_iops: ${WIN_RATE_IOPS}"
        fi

        WIN_RUNTIME_LINE=""
        if [[ -n "${WIN_RUNTIME}" && "${WIN_RUNTIME}" != "null" && "${WIN_RUNTIME}" != "0" ]]; then
            WIN_RUNTIME_LINE=$'\n'"    runtime: ${WIN_RUNTIME}"
        else
            echo "Windows WIN_RUNTIME empty/null/0 — size-based mode (write full --size, no --time_based)"
        fi

        WINDOWS_BLOCK="
windows:
${WIN_HOST_BLOCK}

  storage_win:
    devices:
${WIN_DEVICE_BLOCK}    mount_point: '${WIN_MOUNT_POINT}'

  fio_win:
    run_dir: '${WIN_RUN_DIR}'
    test_size: '${WIN_TEST_SIZE}'${WIN_RUNTIME_LINE}
    block_sizes: '${WIN_BLOCK_SIZES}'
    io_patterns: '${WIN_IO_PATTERNS}'
    numjobs: ${WIN_NUMJOBS}
    iodepth: ${WIN_IODEPTH}
    direct_io: ${WIN_DIRECT_IO}${WIN_RATE_IOPS_LINE}

  output_win:
    directory: '${WIN_OUTPUT_DIR}'
    format: '${WIN_OUTPUT_FORMAT}'"
    fi

    # ---- Assemble the config ----
    # Windows-only needs a minimal vm section for namespace
    VM_NAMESPACE_BLOCK=""
    if [[ "${HAS_LINUX}" == "false" && "${HAS_WINDOWS}" == "true" ]]; then
        VM_NAMESPACE_BLOCK="vm:
  namespace: \"${NAMESPACE}\""
    fi

    cat > "${CONFIG}" <<EOF
description: "${DESCRIPTION}"

${VM_NAMESPACE_BLOCK}
${LINUX_BLOCK}

retry:
  interval: ${RETRY_INTERVAL}
  max_retries: ${MAX_RETRIES}
  skip_connectivity_test: false

monitoring:
  task_monitor_interval: ${MONITOR_INTERVAL}

migrate:
  workloads: "${MIGRATE_WORKLOADS}"
  interval: ${MIGRATE_INTERVAL}
${WINDOWS_BLOCK}
EOF

    echo "Generated config:"
fi

cat "${CONFIG}"
echo "---"

# --- Run fio-tests.py from /work/results so output lands there ---
cd /work/results

EXTRA_ARGS=""
if [[ "${MONITOR_VM}" == "true" ]]; then
    EXTRA_ARGS="${EXTRA_ARGS} --monitor-vm --monitor-vm-interval ${MONITOR_VM_INTERVAL}"
fi
if [[ -n "${MAX_WORKERS:-}" ]]; then
    EXTRA_ARGS="${EXTRA_ARGS} --max-workers ${MAX_WORKERS}"
fi

exec python3 /work/fio-tests.py \
    -c "${CONFIG}" \
    --virtctl-only \
    --yes-i-mean-it \
    ${EXTRA_ARGS} \
    "$@"
