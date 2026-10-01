#!/bin/bash

# Windows VM creation for FIO --testdir mode (OS disk only — no separate data PVC).
# FIO uses c:/testdir and c:/fio-results on the root disk; see fio-tests.py --testdir.
# Cloud-init (via Secret secretRef — inline limit 2048 B) installs expand-os-disk.ps1,
# runs it once, and registers scheduled task ExpandOsDiskC (AtStartup) to grow C: to PVC size.
# Enhanced VM creation script with customizable prefix, range, CPU, memory, and root storage.
# Usage: ./winCdisk.sh [OPTIONS]
# Options:
#   -p, --prefix PREFIX    VM name prefix (default: vm)
#   -s, --start NUMBER     Starting VM number (default: 1)
#   -e, --end NUMBER       Ending VM number (default: 1)
#   -c, --count NUMBER     Number of VMs to create (alternative to --end)
#   --clone NUMBER         Alias for --count; use with --origvm
#   --origvm NAME          Clone OS disk from existing/running VM via VolumeSnapshot
#   --cores NUMBER         CPU cores per socket (default: 32); VM gets cores × sockets × threads
#   --sockets NUMBER       Number of CPU sockets (default: 1)
#   --threads NUMBER       CPU threads per core (default: 1)
#   --memory SIZE          Memory the VM gets, with unit (default: 32Gi)
#   --storageclass NAME    Storage class name (default: ocs-storagecluster-ceph-rbd-virtualization)
#   --imageurl URL         Image URL (default: Win2k25 gold image)
#   --ssh-pubkey FILE      SSH public key to inject for Administrator (default: ~/.ssh/id_ed25519.pub or id_rsa.pub)
#   -h, --help            Show this help message
#
# Examples:
#   ./winCdisk.sh -p test -s 1 -e 10                    # Creates test-1 to test-10
#   ./winCdisk.sh -p worker -c 5                        # Creates worker-1 to worker-5
#   ./winCdisk.sh -p db --cores 4 --memory 16Gi -c 3    # Creates db-1 to db-3 with 4 cores, 16Gi RAM
#   ./winCdisk.sh --origvm winx-1 --clone 10            # 10 clones: winx-2 .. winx-11
#   ./winCdisk.sh -p vm -c 1 --ssh-pubkey ~/.ssh/id_ed25519.pub

# Default values
PREFIX="vm"
PREFIX_SET=0
START=1
START_SET=0
END=1
COUNT=""
CPU_CORES=32
CPU_CORES_SET=0
CPU_SOCKETS=1
CPU_SOCKETS_SET=0
CPU_THREADS=1
CPU_THREADS_SET=0
MEMORY="32Gi"
MEMORY_SET=0
STORAGECLASS="ocs-storagecluster-ceph-rbd-virtualization"
STORAGECLASS_SET=0
# lvms-nvme-vg
ROOTDISK_SIZE="75Gi"
IMAGEURL="http://perfscale.perf.eng.bos2.dc.redhat.com/pub/daschmidt/rhocpv-images/win2k25-rootdisk-qemu-guest-agent.qcow2"
GOLDEN_DV=""
GOLDEN_SNAPSHOT=""
ORIGVM=""
SNAPSHOT_CLASS=""
KEEP_ORIG_SNAPSHOT=1
NAMESPACE="ekuric"
SSH_PUBKEY_FILE="${SSH_PUBKEY_FILE:-}"

# Function to show usage
show_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -p, --prefix PREFIX    VM name prefix (default: vm; with --origvm: derived from source name)"
    echo "  -s, --start NUMBER     Starting VM number (default: 1; with --origvm: source number + 1)"
    echo "  -e, --end NUMBER       Ending VM number (default: 1)"
    echo "  -c, --count NUMBER     Number of VMs to create (alternative to --end)"
    echo "  --clone NUMBER         Same as -c/--count; intended with --origvm"
    echo "  --origvm NAME          Clone root disk from a running/existing VM (VolumeSnapshot of its OS PVC)"
    echo "  --snapshot-class NAME  VolumeSnapshotClass for --origvm (auto-detected if omitted)"
    echo "  --delete-snapshot      Delete the VolumeSnapshot created from --origvm after VM create"
    echo "                         (default: keep it — safer while CDI clones from the snapshot)"
    echo "  --cores NUMBER         CPU cores per socket (default: 32); VM gets cores × sockets × threads"
    echo "  --sockets NUMBER       Number of CPU sockets (default: 1)"
    echo "  --threads NUMBER       CPU threads per core (default: 1)"
    echo "  --memory SIZE          Memory the VM gets, with unit (default: 32Gi)"
    echo "  --storageclass NAME    Storage class name (default: ocs-storagecluster-ceph-rbd-virtualization;"
    echo "                         with --origvm: taken from the source PVC unless set here)"
    echo "  --opstorage SIZE       Root/OS disk size (default: 75Gi; FIO --testdir uses this disk)"
    echo "  --imageurl URL         Image URL for HTTP import (default: Win2k25 gold image)"
    echo "  --golden-dv NAME       Clone root disk from a pre-imported golden DataVolume instead of HTTP import (much faster)"
    echo "                         OS disk size is taken from the golden DV (ignores --opstorage)"
    echo "  --golden-snapshot NAME Clone root disk from a pre-created VolumeSnapshot (best for large scale)"
    echo "                         OS disk size is taken from the snapshot (ignores --opstorage)"
    echo "  --namespace NAME       Namespace to create VMs in (default: ekuric)"
    echo "  --ssh-pubkey FILE      Public key for Administrator passwordless SSH"
    echo "                         (default: \$SSH_PUBKEY_FILE, else ~/.ssh/id_ed25519.pub or id_rsa.pub)"
    echo "  -h, --help            Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 -p test -s 1 -e 10                    # Creates test-1 to test-10"
    echo "  $0 -p worker -c 5                        # Creates worker-1 to worker-5"
    echo "  $0 -p db --cores 4 --memory 16Gi -c 3    # Creates db-1 to db-3 with 4 cores, 16Gi RAM"
    echo "  $0 -p app --sockets 1 --cores 8 -c 2     # Creates app-1 to app-2 with 1 socket, 8 cores"
    echo "  $0 -p web --storageclass fast-ssd -c 3   # Creates web-1 to web-3 with custom storage class"
    echo "  $0 -p vm -c 500 --golden-snapshot windv-snap   # Large-scale clone from VolumeSnapshot (OS disk only)"
    echo "  $0 --origvm winx-1 --clone 10            # Snapshot winx-1 OS disk; create winx-2 .. winx-11"
    echo "  $0 --origvm vm-1 --clone 10              # Creates vm-2 .. vm-11 (successive numbers after source)"
    echo "  $0 --origvm winx-1 --clone 10 -p other -s 100  # Override: other-100 .. other-109"
    echo ""
    echo "Clone from running VM (--origvm):"
    echo "  Creates a VolumeSnapshot of the source VM root/OS PVC (crash-consistent if VM is running),"
    echo "  then creates N new VMs cloning from that snapshot (same path as --golden-snapshot)."
    echo "  Naming: if source is <prefix>-<N> (e.g. vm-1, winc-3), clones are <prefix>-(N+1) .. <prefix>-(N+count)"
    echo "  unless -p/--prefix and/or -s/--start are set explicitly."
    echo "  If source has no trailing -<number>, falls back to <origvm>-clone-1 .. -count."
    echo "  CPU/memory: inherited from the source VM (guest cores/sockets/threads + memory.guest,"
    echo "  and domain resource requests/limits when present) unless --cores/--sockets/--threads/--memory are set."
    echo "  Prefer a quiet/idle source VM; for a fully consistent image, stop the VM before cloning."
    echo ""
    echo "CPU Configuration:"
    echo "  Total vCPUs = cores × sockets × threads (guest CPU topology + resource requests/limits)"
    echo "  Default: 32 cores × 1 socket × 1 thread = 32 vCPUs"
    echo "  --cores / --memory set what each VM actually gets (requests = limits = guest)"
    echo ""
    echo "Memory Examples:"
    echo "  8Gi, 12Gi, 16Gi, 32Gi, 64Gi"
    echo ""
    echo "Note: You must specify at least one VM to create using -c/--clone, -e, or -s options"
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -p|--prefix)
            PREFIX="$2"
            PREFIX_SET=1
            shift 2
            ;;
        -s|--start)
            START="$2"
            START_SET=1
            shift 2
            ;;
        -e|--end)
            END="$2"
            shift 2
            ;;
        -c|--count|--clone)
            COUNT="$2"
            shift 2
            ;;
        --origvm)
            ORIGVM="$2"
            shift 2
            ;;
        --snapshot-class)
            SNAPSHOT_CLASS="$2"
            shift 2
            ;;
        --delete-snapshot)
            KEEP_ORIG_SNAPSHOT=0
            shift
            ;;
        --keep-snapshot)
            # retained for compatibility; keeping is now the default
            KEEP_ORIG_SNAPSHOT=1
            shift
            ;;
        --cores)
            CPU_CORES="$2"
            CPU_CORES_SET=1
            shift 2
            ;;
        --sockets)
            CPU_SOCKETS="$2"
            CPU_SOCKETS_SET=1
            shift 2
            ;;
        --threads)
            CPU_THREADS="$2"
            CPU_THREADS_SET=1
            shift 2
            ;;
        --memory)
            MEMORY="$2"
            MEMORY_SET=1
            shift 2
            ;;
        --storageclass)
            STORAGECLASS="$2"
            STORAGECLASS_SET=1
            shift 2
            ;;
        --opstorage)
            ROOTDISK_SIZE="$2"
            shift 2
            ;;
        --imageurl)
            IMAGEURL="$2"
            shift 2
            ;;
        --golden-dv)
            GOLDEN_DV="$2"
            shift 2
            ;;
        --golden-snapshot)
            GOLDEN_SNAPSHOT="$2"
            shift 2
            ;;
        --namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        --ssh-pubkey)
            SSH_PUBKEY_FILE="$2"
            shift 2
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        -*)
            echo "Unknown option $1"
            show_usage
            exit 1
            ;;
        *)
            echo "Invalid argument: $1"
            show_usage
            exit 1
            ;;
    esac
done

if [[ -n "$GOLDEN_DV" ]] && [[ -n "$GOLDEN_SNAPSHOT" ]]; then
    echo "Error: Use only one of --golden-dv or --golden-snapshot for the root disk."
    exit 1
fi

if [[ -n "$ORIGVM" ]] && { [[ -n "$GOLDEN_DV" ]] || [[ -n "$GOLDEN_SNAPSHOT" ]]; }; then
    echo "Error: --origvm cannot be combined with --golden-dv or --golden-snapshot."
    echo "  --origvm creates a VolumeSnapshot of the source VM and clones from that."
    exit 1
fi

if [[ -z "$ORIGVM" ]] && [[ "$KEEP_ORIG_SNAPSHOT" -eq 0 ]]; then
    echo "Error: --delete-snapshot only applies with --origvm."
    exit 1
fi

# Default naming for clones of an existing VM:
#   --origvm vm-1 --clone 10  →  vm-2 .. vm-11  (same prefix, successive numbers)
# Override with -p/--prefix and/or -s/--start. If origvm has no -<digits> suffix,
# fall back to <origvm>-clone-1 .. -N.
if [[ -n "$ORIGVM" ]]; then
    if [[ "$ORIGVM" =~ ^(.+)-([0-9]+)$ ]]; then
        orig_prefix="${BASH_REMATCH[1]}"
        orig_num="${BASH_REMATCH[2]}"
        # Strip leading zeros for arithmetic (08 → 8)
        orig_num=$((10#$orig_num))
        if [[ "$PREFIX_SET" -eq 0 ]]; then
            PREFIX="$orig_prefix"
        fi
        if [[ "$START_SET" -eq 0 ]]; then
            START=$((orig_num + 1))
        fi
        echo "Clone naming from --origvm '$ORIGVM': prefix='$PREFIX' start=$START (successive after source)"
    elif [[ "$PREFIX_SET" -eq 0 ]]; then
        PREFIX="${ORIGVM}-clone"
        echo "Clone naming: source '$ORIGVM' has no -<number> suffix; using prefix='$PREFIX' start=$START"
    fi
fi

# Inherit guest CPU topology + memory (and domain resources when set) from --origvm.
# Explicit --cores/--sockets/--threads/--memory always win. Called before validation.
inherit_origvm_compute() {
    local cores sockets threads mem req_cpu lim_cpu req_mem lim_mem
    local mi gi

    if ! oc get vm "$ORIGVM" -n "$NAMESPACE" >/dev/null 2>&1; then
        echo "Warning: cannot get VM '$ORIGVM' in $NAMESPACE — keeping CPU/memory script defaults"
        return 0
    fi

    cores=$(oc get vm "$ORIGVM" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.domain.cpu.cores}' 2>/dev/null || true)
    sockets=$(oc get vm "$ORIGVM" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.domain.cpu.sockets}' 2>/dev/null || true)
    threads=$(oc get vm "$ORIGVM" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.domain.cpu.threads}' 2>/dev/null || true)
    mem=$(oc get vm "$ORIGVM" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.domain.memory.guest}' 2>/dev/null || true)
    req_cpu=$(oc get vm "$ORIGVM" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.domain.resources.requests.cpu}' 2>/dev/null || true)
    lim_cpu=$(oc get vm "$ORIGVM" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.domain.resources.limits.cpu}' 2>/dev/null || true)
    req_mem=$(oc get vm "$ORIGVM" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.domain.resources.requests.memory}' 2>/dev/null || true)
    lim_mem=$(oc get vm "$ORIGVM" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.domain.resources.limits.memory}' 2>/dev/null || true)

    if [[ "$CPU_CORES_SET" -eq 0 && -n "$cores" && "$cores" =~ ^[0-9]+$ ]]; then
        CPU_CORES="$cores"
    fi
    if [[ "$CPU_SOCKETS_SET" -eq 0 ]]; then
        if [[ -n "$sockets" && "$sockets" =~ ^[0-9]+$ ]]; then
            CPU_SOCKETS="$sockets"
        else
            CPU_SOCKETS=1
        fi
    fi
    if [[ "$CPU_THREADS_SET" -eq 0 ]]; then
        if [[ -n "$threads" && "$threads" =~ ^[0-9]+$ ]]; then
            CPU_THREADS="$threads"
        else
            CPU_THREADS=1
        fi
    fi

    # Normalize guest memory to NGi when possible (script templates use Gi).
    if [[ "$MEMORY_SET" -eq 0 && -n "$mem" ]]; then
        if [[ "$mem" =~ ^([0-9]+)Gi$ ]]; then
            MEMORY="$mem"
        elif [[ "$mem" =~ ^([0-9]+)Mi$ ]]; then
            mi="${BASH_REMATCH[1]}"
            if [[ $((mi % 1024)) -eq 0 ]]; then
                gi=$((mi / 1024))
                MEMORY="${gi}Gi"
            else
                MEMORY="$mem"
            fi
        elif [[ "$mem" =~ ^([0-9]+)G$ ]]; then
            MEMORY="${BASH_REMATCH[1]}Gi"
        else
            MEMORY="$mem"
        fi
    fi

    # Stash optional domain resource overrides from source (applied after TOTAL_VCPUS calc).
    ORIG_REQ_CPU="$req_cpu"
    ORIG_LIM_CPU="$lim_cpu"
    ORIG_REQ_MEM="$req_mem"
    ORIG_LIM_MEM="$lim_mem"

    echo "Inherited compute from VM '$ORIGVM':"
    echo "  guest CPU:     ${CPU_CORES} cores × ${CPU_SOCKETS} sockets × ${CPU_THREADS} threads"
    echo "  guest memory:  ${MEMORY}"
    if [[ -n "$req_cpu$lim_cpu$req_mem$lim_mem" ]]; then
        echo "  resources:     requests cpu=${req_cpu:-n/a} mem=${req_mem:-n/a} ; limits cpu=${lim_cpu:-n/a} mem=${lim_mem:-n/a}"
    fi
    if [[ "$CPU_CORES_SET" -eq 1 || "$CPU_SOCKETS_SET" -eq 1 || "$CPU_THREADS_SET" -eq 1 || "$MEMORY_SET" -eq 1 ]]; then
        echo "  (CLI --cores/--sockets/--threads/--memory overrides applied where set)"
    fi
}

ORIG_REQ_CPU=""
ORIG_LIM_CPU=""
ORIG_REQ_MEM=""
ORIG_LIM_MEM=""
if [[ -n "$ORIGVM" ]]; then
    inherit_origvm_compute
fi

# Check if any meaningful options were provided
if [[ -z "$COUNT" ]] && [[ "$START" -eq 1 ]] && [[ "$END" -eq 1 ]]; then
    if [[ -n "$ORIGVM" ]]; then
        echo "Error: --origvm '$ORIGVM' requires --clone N (or -c/-e) to set how many clones to create."
        echo "  Example: $0 --origvm $ORIGVM --clone 10"
    else
        echo "Error: No options provided. You must specify at least one VM to create."
        echo ""
        show_usage
    fi
    exit 1
fi

# Calculate END if COUNT is provided
if [[ -n "$COUNT" ]]; then
    if [[ ! "$COUNT" =~ ^[0-9]+$ ]] || [[ "$COUNT" -lt 1 ]]; then
        echo "Error: --clone/-c/--count must be a positive integer"
        exit 1
    fi
    END=$((START + COUNT - 1))
fi

# Validate inputs
if [[ ! "$START" =~ ^[0-9]+$ ]] || [[ ! "$END" =~ ^[0-9]+$ ]]; then
    echo "Error: Start and end must be positive integers"
    exit 1
fi

if [[ "$START" -gt "$END" ]]; then
    echo "Error: Start number ($START) cannot be greater than end number ($END)"
    exit 1
fi

# Validate CPU parameters
if [[ ! "$CPU_CORES" =~ ^[0-9]+$ ]] || [[ ! "$CPU_SOCKETS" =~ ^[0-9]+$ ]] || [[ ! "$CPU_THREADS" =~ ^[0-9]+$ ]]; then
    echo "Error: CPU cores, sockets, and threads must be positive integers"
    exit 1
fi

if [[ "$CPU_CORES" -lt 1 ]] || [[ "$CPU_SOCKETS" -lt 1 ]] || [[ "$CPU_THREADS" -lt 1 ]]; then
    echo "Error: CPU cores, sockets, and threads must be at least 1"
    exit 1
fi

# Validate memory format (Gi preferred; Mi allowed when inherited from source)
if [[ ! "$MEMORY" =~ ^[0-9]+(Gi|Mi)$ ]]; then
    echo "Error: Memory must be specified with Gi or Mi suffix (e.g., 8Gi, 12Gi, 16Gi, 32768Mi)"
    exit 1
fi

# Read storage size from a golden DataVolume's PVC spec (clone target must be >= source)
get_dv_storage_size() {
    local dv_name=$1
    local size
    size=$(oc get dv "$dv_name" -n "$NAMESPACE" -o jsonpath='{.spec.pvc.resources.requests.storage}' 2>/dev/null)
    if [[ -z "$size" ]]; then
        local claim_name
        claim_name=$(oc get dv "$dv_name" -n "$NAMESPACE" -o jsonpath='{.status.claimName}' 2>/dev/null)
        if [[ -n "$claim_name" ]]; then
            size=$(oc get pvc "$claim_name" -n "$NAMESPACE" -o jsonpath='{.spec.resources.requests.storage}' 2>/dev/null)
        fi
    fi
    echo "$size"
}

# Read storage size from a VolumeSnapshot (clone target must be >= source)
get_snapshot_storage_size() {
    local snap_name=$1
    local size
    size=$(oc get volumesnapshot "$snap_name" -n "$NAMESPACE" -o jsonpath='{.status.restoreSize}' 2>/dev/null)
    if [[ -z "$size" ]]; then
        local src_pvc
        src_pvc=$(oc get volumesnapshot "$snap_name" -n "$NAMESPACE" -o jsonpath='{.spec.source.persistentVolumeClaimName}' 2>/dev/null)
        if [[ -n "$src_pvc" ]]; then
            size=$(oc get pvc "$src_pvc" -n "$NAMESPACE" -o jsonpath='{.spec.resources.requests.storage}' 2>/dev/null)
        fi
    fi
    echo "$size"
}

# Resolve root/OS PVC name for an existing VM (bootOrder=1 disk, else first DV/PVC volume)
resolve_origvm_root_pvc() {
    local vm_name=$1
    local boot_vol pvc claim

    if ! oc get vm "$vm_name" -n "$NAMESPACE" >/dev/null 2>&1; then
        echo "Error: VirtualMachine '$vm_name' not found in namespace $NAMESPACE." >&2
        return 1
    fi

    boot_vol=$(oc get vm "$vm_name" -n "$NAMESPACE" -o jsonpath='{range .spec.template.spec.domain.devices.disks[?(@.bootOrder==1)]}{.name}{end}' 2>/dev/null)

    pvc=$(oc get vm "$vm_name" -n "$NAMESPACE" -o json | BOOT_VOL="$boot_vol" python3 -c '
import json, os, sys
vm = json.load(sys.stdin)
boot = os.environ.get("BOOT_VOL", "")
vols = {v.get("name"): v for v in vm.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])}

def claim_of(v):
    if not v:
        return ""
    if "dataVolume" in v:
        return v["dataVolume"].get("name") or ""
    if "persistentVolumeClaim" in v:
        return v["persistentVolumeClaim"].get("claimName") or ""
    return ""

if boot and boot in vols:
    c = claim_of(vols[boot])
    if c:
        print(c)
        sys.exit(0)

dvs = vm.get("spec", {}).get("dataVolumeTemplates") or []
if dvs:
    name = (dvs[0].get("metadata") or {}).get("name") or ""
    if name:
        print(name)
        sys.exit(0)

for v in vols.values():
    c = claim_of(v)
    if c:
        print(c)
        sys.exit(0)
sys.exit(1)
') || true

    if [[ -z "$pvc" ]]; then
        echo "Error: Could not resolve root/OS PVC for VM '$vm_name'." >&2
        return 1
    fi

    if oc get pvc "$pvc" -n "$NAMESPACE" >/dev/null 2>&1; then
        echo "$pvc"
        return 0
    fi
    claim=$(oc get dv "$pvc" -n "$NAMESPACE" -o jsonpath='{.status.claimName}' 2>/dev/null)
    if [[ -n "$claim" ]] && oc get pvc "$claim" -n "$NAMESPACE" >/dev/null 2>&1; then
        echo "$claim"
        return 0
    fi
    echo "Error: PVC/DV '$pvc' for VM '$vm_name' not found in $NAMESPACE." >&2
    return 1
}

# Pick VolumeSnapshotClass matching the PVC's CSI driver / storage class
resolve_snapshot_class() {
    local pvc_name=$1
    local sc provisioner snap

    if [[ -n "$SNAPSHOT_CLASS" ]]; then
        if ! oc get volumesnapshotclass "$SNAPSHOT_CLASS" >/dev/null 2>&1; then
            echo "Error: VolumeSnapshotClass '$SNAPSHOT_CLASS' not found." >&2
            return 1
        fi
        echo "$SNAPSHOT_CLASS"
        return 0
    fi

    sc=$(oc get pvc "$pvc_name" -n "$NAMESPACE" -o jsonpath='{.spec.storageClassName}' 2>/dev/null)
    provisioner=$(oc get sc "$sc" -o jsonpath='{.provisioner}' 2>/dev/null)

    # Prefer annotation on StorageClass if present (snapshot class name)
    snap=$(oc get sc "$sc" -o json 2>/dev/null | python3 -c '
import json, sys
sc = json.load(sys.stdin)
ann = sc.get("metadata", {}).get("annotations") or {}
for k, v in ann.items():
    if "snapshot" in k.lower() and v and "/" not in v and v.lower() not in ("true", "false"):
        print(v)
        break
' 2>/dev/null || true)

    if [[ -n "$snap" ]] && oc get volumesnapshotclass "$snap" >/dev/null 2>&1; then
        echo "$snap"
        return 0
    fi

    # Match VolumeSnapshotClass.driver to StorageClass.provisioner
    if [[ -n "$provisioner" ]]; then
        snap=$(oc get volumesnapshotclass -o json | python3 -c '
import json, sys
want = "'"$provisioner"'"
data = json.load(sys.stdin)
# Prefer non-default? Prefer exact driver match; favor rbd / ceph names
cands = []
for item in data.get("items", []):
    if item.get("driver") == want:
        name = item["metadata"]["name"]
        cands.append(name)
if not cands:
    sys.exit(1)
# Prefer names containing rbd / ceph / virt
def score(n):
    n = n.lower()
    s = 0
    if "rbd" in n: s += 3
    if "ceph" in n: s += 2
    if "virt" in n: s += 1
    return s
cands.sort(key=score, reverse=True)
print(cands[0])
' 2>/dev/null) || true
    fi

    if [[ -n "$snap" ]]; then
        echo "$snap"
        return 0
    fi

    echo "Error: Could not auto-detect VolumeSnapshotClass for PVC '$pvc_name' (sc=$sc provisioner=$provisioner)." >&2
    echo "  Pass --snapshot-class NAME (oc get volumesnapshotclass)." >&2
    return 1
}

# Snapshot the orig VM root PVC and set GOLDEN_SNAPSHOT for the create loop
prepare_origvm_snapshot() {
    local pvc snap_class snap_name ready i sc_from_pvc size phase

    echo "Resolving root/OS disk for source VM '$ORIGVM' in $NAMESPACE..."
    pvc=$(resolve_origvm_root_pvc "$ORIGVM") || exit 1
    echo "Source root PVC: $pvc"

    phase=$(oc get vmi "$ORIGVM" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)
    if [[ -n "$phase" ]]; then
        echo "Source VMI phase: $phase (snapshot is crash-consistent if Running; stop VM for cleaner image)"
    else
        echo "Source VMI not found (VM may be stopped) — snapshotting PVC '$pvc'"
    fi

    sc_from_pvc=$(oc get pvc "$pvc" -n "$NAMESPACE" -o jsonpath='{.spec.storageClassName}' 2>/dev/null)
    if [[ "$STORAGECLASS_SET" -eq 0 ]] && [[ -n "$sc_from_pvc" ]]; then
        STORAGECLASS="$sc_from_pvc"
        echo "Storage class from source PVC: $STORAGECLASS"
    fi

    size=$(oc get pvc "$pvc" -n "$NAMESPACE" -o jsonpath='{.spec.resources.requests.storage}' 2>/dev/null)
    if [[ -z "$size" ]]; then
        echo "Error: Could not read storage size from PVC '$pvc'."
        exit 1
    fi
    ROOTDISK_SIZE="$size"
    echo "Root disk size from source PVC: $ROOTDISK_SIZE"

    snap_class=$(resolve_snapshot_class "$pvc") || exit 1
    echo "VolumeSnapshotClass: $snap_class"

    snap_name="${ORIGVM}-wincdisk-$(date +%Y%m%d%H%M%S)"
    # DNS-1123: lowercase, truncate
    snap_name=$(echo "$snap_name" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9-]/-/g' | cut -c1-63 | sed 's/-$//')

    echo "Creating VolumeSnapshot '$snap_name' from PVC '$pvc'..."
    cat <<EOF | oc apply -f -
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: ${snap_name}
  namespace: ${NAMESPACE}
  labels:
    app: winCdisk
    winCdisk.origvm: ${ORIGVM}
spec:
  volumeSnapshotClassName: ${snap_class}
  source:
    persistentVolumeClaimName: ${pvc}
EOF

    echo "Waiting for VolumeSnapshot '$snap_name' to become readyToUse..."
    for i in $(seq 1 120); do
        ready=$(oc get volumesnapshot "$snap_name" -n "$NAMESPACE" -o jsonpath='{.status.readyToUse}' 2>/dev/null || true)
        if [[ "$ready" == "true" ]]; then
            echo "VolumeSnapshot '$snap_name' is ready."
            GOLDEN_SNAPSHOT="$snap_name"
            ORIG_SNAPSHOT_CREATED="$snap_name"
            return 0
        fi
        if [[ $((i % 6)) -eq 0 ]]; then
            echo "  still waiting... (${i}0s) readyToUse=${ready:-unknown}"
        fi
        sleep 10
    done
    echo "Error: timed out waiting for VolumeSnapshot '$snap_name' (10 minutes)."
    oc get volumesnapshot "$snap_name" -n "$NAMESPACE" -o yaml | tail -n 40 || true
    exit 1
}

ORIG_SNAPSHOT_CREATED=""
if [[ -n "$ORIGVM" ]]; then
    prepare_origvm_snapshot
fi

# If --golden-snapshot is specified, verify the VolumeSnapshot exists and is ready
if [[ -n "$GOLDEN_SNAPSHOT" ]]; then
    echo "Checking golden VolumeSnapshot '$GOLDEN_SNAPSHOT' in namespace $NAMESPACE..."
    SNAP_READY=$(oc get volumesnapshot "$GOLDEN_SNAPSHOT" -n "$NAMESPACE" -o jsonpath='{.status.readyToUse}' 2>/dev/null)
    if [[ -z "$SNAP_READY" ]]; then
        echo "Error: VolumeSnapshot '$GOLDEN_SNAPSHOT' not found in namespace $NAMESPACE."
        echo "Create it first, e.g.: oc apply -f golden-win-snapshot.yaml"
        exit 1
    fi
    if [[ "$SNAP_READY" != "true" ]]; then
        echo "Error: VolumeSnapshot '$GOLDEN_SNAPSHOT' is not ready (readyToUse: $SNAP_READY)."
        echo "Wait for it: oc get volumesnapshot $GOLDEN_SNAPSHOT -n $NAMESPACE -w"
        exit 1
    fi
    GOLDEN_SNAPSHOT_SIZE=$(get_snapshot_storage_size "$GOLDEN_SNAPSHOT")
    if [[ -z "$GOLDEN_SNAPSHOT_SIZE" ]]; then
        echo "Error: Could not determine storage size for VolumeSnapshot '$GOLDEN_SNAPSHOT'."
        exit 1
    fi
    echo "VolumeSnapshot '$GOLDEN_SNAPSHOT' is ready (restore size: $GOLDEN_SNAPSHOT_SIZE)"
    ROOTDISK_SIZE="$GOLDEN_SNAPSHOT_SIZE"
fi

# If --golden-dv is specified, verify the golden DataVolume exists and is ready
if [[ -n "$GOLDEN_DV" ]]; then
    echo "Checking golden DataVolume '$GOLDEN_DV' in namespace $NAMESPACE..."
    DV_PHASE=$(oc get dv "$GOLDEN_DV" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null)
    if [[ -z "$DV_PHASE" ]]; then
        echo "Error: Golden DataVolume '$GOLDEN_DV' not found in namespace $NAMESPACE."
        echo "Create it first with: oc apply -f golden-win-dv.yaml"
        exit 1
    fi
    if [[ "$DV_PHASE" != "Succeeded" ]]; then
        echo "Error: Golden DataVolume '$GOLDEN_DV' is not ready (phase: $DV_PHASE)."
        echo "Wait for import to complete: oc get dv $GOLDEN_DV -n $NAMESPACE -w"
        exit 1
    fi
    GOLDEN_DV_SIZE=$(get_dv_storage_size "$GOLDEN_DV")
    if [[ -z "$GOLDEN_DV_SIZE" ]]; then
        echo "Error: Could not determine storage size for golden DataVolume '$GOLDEN_DV'."
        exit 1
    fi
    echo "Golden DataVolume '$GOLDEN_DV' is ready (phase: Succeeded, size: $GOLDEN_DV_SIZE)"
    ROOTDISK_SIZE="$GOLDEN_DV_SIZE"
fi

# Validate storage format (basic check for Gi/Ti suffix)
if [[ ! "$ROOTDISK_SIZE" =~ ^[0-9]+(Gi|Ti)$ ]]; then
    echo "Error: Root storage must be specified with Gi or Ti suffix (e.g., 10Gi, 50Gi, 1Ti)"
    exit 1
fi

# Calculate total VMs and CPU/memory the guest actually gets.
# --cores/--sockets/--threads and --memory map 1:1 to guest topology and
# Kubernetes requests/limits (no under-request overcommit), unless --origvm
# provided explicit domain.resources that we should preserve.
TOTAL_VMS=$((END - START + 1))
TOTAL_VCPUS=$((CPU_CORES * CPU_SOCKETS * CPU_THREADS))
CPU_LIMIT=$TOTAL_VCPUS
CPU_REQUEST=$TOTAL_VCPUS
MEMORY_LIMIT="$MEMORY"
MEMORY_REQUEST="$MEMORY"

# Prefer source VM domain.resources when present and user did not override that side.
# CPU: only adopt integer (or millicpu that converts cleanly to whole vCPUs).
_adopt_cpu_resource() {
    local val="$1"
    if [[ -z "$val" ]]; then
        return 1
    fi
    if [[ "$val" =~ ^[0-9]+$ ]]; then
        echo "$val"
        return 0
    fi
    if [[ "$val" =~ ^([0-9]+)m$ ]]; then
        local milli="${BASH_REMATCH[1]}"
        if [[ $((milli % 1000)) -eq 0 ]]; then
            echo $((milli / 1000))
            return 0
        fi
    fi
    return 1
}
if [[ -n "$ORIGVM" ]]; then
    if [[ "$CPU_CORES_SET" -eq 0 && "$CPU_SOCKETS_SET" -eq 0 && "$CPU_THREADS_SET" -eq 0 ]]; then
        if adopted=$(_adopt_cpu_resource "$ORIG_REQ_CPU"); then
            CPU_REQUEST="$adopted"
        fi
        if adopted=$(_adopt_cpu_resource "$ORIG_LIM_CPU"); then
            CPU_LIMIT="$adopted"
        fi
    fi
    if [[ "$MEMORY_SET" -eq 0 ]]; then
        [[ -n "$ORIG_REQ_MEM" ]] && MEMORY_REQUEST="$ORIG_REQ_MEM"
        [[ -n "$ORIG_LIM_MEM" ]] && MEMORY_LIMIT="$ORIG_LIM_MEM"
    fi
fi

# Resolve SSH public key for Administrator (passwordless login).
# Order: --ssh-pubkey / $SSH_PUBKEY_FILE, then $HOME/.ssh, then $SUDO_USER home.
resolve_ssh_pubkey() {
    local candidates=()
    [[ -n "$SSH_PUBKEY_FILE" ]] && candidates+=("$SSH_PUBKEY_FILE")
    candidates+=("$HOME/.ssh/id_ed25519.pub" "$HOME/.ssh/id_rsa.pub")
    if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
        local sudo_home
        sudo_home=$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6)
        [[ -n "$sudo_home" ]] && candidates+=("$sudo_home/.ssh/id_ed25519.pub" "$sudo_home/.ssh/id_rsa.pub")
    fi

    local f
    for f in "${candidates[@]}"; do
        if [[ -n "$f" && -f "$f" ]]; then
            SSH_KEY=$(tr -d '\r\n' < "$f")
            SSH_KEY_SOURCE="$f"
            return 0
        fi
    done
    SSH_KEY=""
    SSH_KEY_SOURCE=""
    return 1
}

if ! resolve_ssh_pubkey; then
    echo "Error: No SSH public key found for passwordless Administrator login."
    echo "  Pass one of:"
    echo "    --ssh-pubkey /path/to/id_ed25519.pub"
    echo "    SSH_PUBKEY_FILE=/path/to/id_ed25519.pub $0 ..."
    echo "  Or place a key at \$HOME/.ssh/id_ed25519.pub or id_rsa.pub"
    exit 1
fi
echo "SSH public key: $SSH_KEY_SOURCE"
if [[ ! "$SSH_KEY" =~ ^(ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|sk-ssh-ed25519|sk-ecdsa-sha2-nistp256)[[:space:]] ]]; then
    echo "Error: SSH public key does not look valid (from $SSH_KEY_SOURCE)"
    exit 1
fi
SSH_KEY_COMMENT=$(echo "$SSH_KEY" | awk '{print $NF}')
echo "SSH key type/comment: $(echo "$SSH_KEY" | awk '{print $1}') ${SSH_KEY_COMMENT}"
echo ""

echo "=========================================="
echo "    VM Creation Summary"
echo "=========================================="
echo "Prefix:        $PREFIX"
echo "Range:         $PREFIX-$START to $PREFIX-$END"
echo "Total VMs:     $TOTAL_VMS"
if [[ -n "$ORIGVM" ]]; then
    echo "Clone source:  VM '$ORIGVM' (via VolumeSnapshot '$GOLDEN_SNAPSHOT')"
fi
echo "CPU Config:    $CPU_CORES cores × $CPU_SOCKETS sockets × $CPU_THREADS threads = $TOTAL_VCPUS vCPUs"
echo "CPU Requests:  $CPU_REQUEST"
echo "CPU Limits:    $CPU_LIMIT"
echo "Memory:        $MEMORY per VM"
echo "Mem Requests:  $MEMORY_REQUEST"
echo "Mem Limits:    $MEMORY_LIMIT"
echo "Namespace:     $NAMESPACE"
echo "Storage Class: $STORAGECLASS"
echo "PVC:           Root/OS only ${ROOTDISK_SIZE} (no data disk — FIO --testdir on C:)"
echo "SSH pubkey:    $SSH_KEY_SOURCE"
if [[ -n "$GOLDEN_SNAPSHOT" ]]; then
    if [[ -n "$ORIGVM" ]]; then
        echo "Root source:   VolumeSnapshot '$GOLDEN_SNAPSHOT' (from running/existing VM '$ORIGVM')"
    else
        echo "Root source:   VolumeSnapshot '$GOLDEN_SNAPSHOT' (smart-clone at scale)"
    fi
elif [[ -n "$GOLDEN_DV" ]]; then
    echo "Root source:   CLONE from golden DV '$GOLDEN_DV'"
else
    echo "Root source:   HTTP import from $IMAGEURL"
fi
echo ""
echo "VM Specifications:"
echo "  • CPU: $TOTAL_VCPUS vCPUs ($CPU_CORES cores × $CPU_SOCKETS sockets × $CPU_THREADS threads)"
echo "  • CPU requests/limits: $CPU_REQUEST / $CPU_LIMIT"
echo "  • Memory requests/limits: $MEMORY_REQUEST / $MEMORY_LIMIT"
if [[ -n "$GOLDEN_SNAPSHOT" ]]; then
    echo "  • OS Disk: $ROOTDISK_SIZE (from snapshot $GOLDEN_SNAPSHOT)"
elif [[ -n "$GOLDEN_DV" ]]; then
    echo "  • OS Disk: $ROOTDISK_SIZE (cloned from $GOLDEN_DV)"
else
    echo "  • OS Disk: $ROOTDISK_SIZE ($IMAGEURL)"
fi
echo "  • OS volume: cloud-init extends C: on first boot and on every boot (PVC resize)"
echo ""
echo "Starting VM creation in 3 seconds..."
echo "Press Ctrl+C to cancel..."
sleep 3

# Idempotent C: extend (gold image / PVC often larger than initial Windows partition)
read -r -d '' EXPAND_DISK_SCRIPT <<'EXPANDSCRIPTEOF' || true
$log = 'C:\ProgramData\expand-os-disk.log'
function Log($m) { "$(Get-Date -Format o) $m" | Out-File -FilePath $log -Append -Encoding utf8 }
Log 'expand-os-disk.ps1 start'
Get-Disk | ForEach-Object { Update-Disk -Number $_.Number -ErrorAction SilentlyContinue }
Update-HostStorageCache
$part = Get-Partition -DriveLetter C -ErrorAction SilentlyContinue
if (-not $part) { Log 'no C: partition'; exit 1 }
$disk = Get-Disk -Number $part.DiskNumber
Log ("disk {0} size={1} allocated={2}" -f $disk.Number, $disk.Size, $disk.AllocatedSize)
try {
  $max = (Get-PartitionSupportedSize -DriveLetter C).SizeMax
  Log ("C: size={0} max={1}" -f $part.Size, $max)
  if ($part.Size -ge $max -and ($disk.Size - $disk.AllocatedSize) -gt 1GB) {
    $afterC = Get-Partition -DiskNumber $disk.Number |
      Where-Object { $_.PartitionNumber -gt $part.PartitionNumber -and -not $_.DriveLetter } |
      Sort-Object PartitionNumber
    foreach ($p in $afterC) {
      if ($p.Size -gt 5GB) { continue }
      Log ("remove blocking partition {0} type={1}" -f $p.PartitionNumber, $p.Type)
      Remove-Partition -InputObject $p -Confirm:$false
      Update-HostStorageCache
      $max = (Get-PartitionSupportedSize -DriveLetter C).SizeMax
      Log ("C: max after removal={0}" -f $max)
    }
    $part = Get-Partition -DriveLetter C
  }
  if ($part.Size -lt $max) {
    Resize-Partition -DriveLetter C -Size $max
    Log 'Resize-Partition succeeded'
  } elseif (($disk.Size - $disk.AllocatedSize) -le 1GB) {
    Log 'C: already uses full disk'
  } else {
    Log 'C: still blocked; manual partition layout change required'
    exit 1
  }
} catch {
  Log ("Resize-Partition failed: {0}" -f $_.Exception.Message)
  exit 1
}
EXPANDSCRIPTEOF
EXPAND_DISK_B64=$(printf '%s' "$EXPAND_DISK_SCRIPT" | base64 -w0 2>/dev/null || printf '%s' "$EXPAND_DISK_SCRIPT" | base64 | tr -d '\n')

# Cloudbase-Init on Windows: prefer #ps1_sysnative over #cloud-config runcmd.
# Nested powershell -Command quoting in runcmd often fails silently; a native
# PowerShell userdata script reliably writes administrators_authorized_keys.
# KubeVirt inline userData limit is 2048 bytes — use a Secret.
CLOUDINIT_SECRET="${PREFIX}-cloudinit-${START}-${END}"
CLOUDINIT_FILE=$(mktemp)
cat > "$CLOUDINIT_FILE" <<CLOUDINITEOF
#ps1_sysnative
\$ErrorActionPreference = 'Continue'
\$log = 'C:\\ProgramData\\winCdisk-cloudinit.log'
function Log(\$m) { "\$(Get-Date -Format o) \$m" | Out-File -FilePath \$log -Append -Encoding utf8 }
Log 'winCdisk cloud-init start'

\$sshKey = @'
$SSH_KEY
'@
\$sshKey = \$sshKey.Trim()
if (-not \$sshKey) { Log 'ERROR: empty SSH key'; exit 1 }

New-Item -ItemType Directory -Force -Path 'C:\\ProgramData\\ssh' | Out-Null
New-Item -ItemType Directory -Force -Path 'C:\\Users\\Administrator\\.ssh' | Out-Null

\$utf8NoBom = New-Object System.Text.UTF8Encoding \$false
\$adminKeys = 'C:\\ProgramData\\ssh\\administrators_authorized_keys'
\$userKeys = 'C:\\Users\\Administrator\\.ssh\\authorized_keys'
\$userSshDir = 'C:\\Users\\Administrator\\.ssh'

# Admin SSH uses ProgramData keys (not per-user authorized_keys).
[System.IO.File]::WriteAllText(\$adminKeys, (\$sshKey + [Environment]::NewLine), \$utf8NoBom)
icacls \$adminKeys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
Log 'administrators_authorized_keys written'

# Per-user authorized_keys is optional; gold images often lock this path.
try {
  if (Test-Path -LiteralPath \$userSshDir) {
    & takeown.exe /f \$userSshDir /r /d y 2>\$null | Out-Null
    & icacls.exe \$userSshDir /grant 'Everyone:F' /T 2>\$null | Out-Null
  }
  if (Test-Path -LiteralPath \$userKeys) {
    & takeown.exe /f \$userKeys 2>\$null | Out-Null
    & icacls.exe \$userKeys /grant 'Everyone:F' 2>\$null | Out-Null
    Remove-Item -Force -LiteralPath \$userKeys -ErrorAction SilentlyContinue
  }
  [System.IO.File]::WriteAllText(\$userKeys, (\$sshKey + [Environment]::NewLine), \$utf8NoBom)
  & icacls.exe \$userKeys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' 2>\$null | Out-Null
  & icacls.exe \$userSshDir /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' 2>\$null | Out-Null
  Log 'Administrator .ssh authorized_keys written'
} catch {
  Log ('Administrator .ssh authorized_keys skipped: ' + \$_.Exception.Message)
}

\$sshd = Get-Service sshd -ErrorAction SilentlyContinue
if (\$sshd) {
  if (\$sshd.StartType -eq 'Disabled') { Set-Service -Name sshd -StartupType Automatic }
  Start-Service sshd -ErrorAction SilentlyContinue
  Restart-Service sshd -Force -ErrorAction SilentlyContinue
  Log ("sshd status=" + (Get-Service sshd).Status)
} else {
  Log 'WARNING: sshd service not found'
}

\$b64 = @'
$EXPAND_DISK_B64
'@
\$expandPath = 'C:\\ProgramData\\expand-os-disk.ps1'
[System.IO.File]::WriteAllText(\$expandPath, [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(\$b64.Trim())), \$utf8NoBom)
try {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File \$expandPath
  Log 'expand-os-disk.ps1 finished'
} catch {
  Log ("expand-os-disk.ps1 failed: " + \$_.Exception.Message)
}
\$tr = New-ScheduledTaskTrigger -AtStartup
\$ac = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\\ProgramData\\expand-os-disk.ps1'
Register-ScheduledTask -TaskName 'ExpandOsDiskC' -Action \$ac -Trigger \$tr -RunLevel Highest -User 'SYSTEM' -Force | Out-Null
Log 'winCdisk cloud-init done'
CLOUDINITEOF
echo "Creating cloud-init Secret ${CLOUDINIT_SECRET} in namespace ${NAMESPACE}..."
oc create secret generic "$CLOUDINIT_SECRET" \
  --from-file=userdata="$CLOUDINIT_FILE" \
  -n "$NAMESPACE" \
  --dry-run=client -o yaml | oc apply -f -
rm -f "$CLOUDINIT_FILE"

for vm in $(seq "$START" "$END"); do

if [[ -n "$GOLDEN_SNAPSHOT" ]]; then
ROOTDISK_SOURCE="source:
          snapshot:
            name: $GOLDEN_SNAPSHOT
            namespace: $NAMESPACE"
elif [[ -n "$GOLDEN_DV" ]]; then
ROOTDISK_SOURCE="source:
          pvc:
            name: $GOLDEN_DV
            namespace: $NAMESPACE"
else
ROOTDISK_SOURCE="source:
          http:
            url: >-
              $IMAGEURL"
fi

	cat << EOF | oc create -f -
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  annotations:
  name: $PREFIX-$vm
  namespace: $NAMESPACE
spec:
  dataVolumeTemplates:
    - metadata:
        name: $PREFIX-rootdisk-$vm
      spec:
        pvc:
          accessModes:
            - ReadWriteMany
          resources:
            requests:
              storage: $ROOTDISK_SIZE
          storageClassName: $STORAGECLASS
          volumeMode: Block
        $ROOTDISK_SOURCE
  runStrategy: Always  
  template:
    metadata:
      annotations:
        vm.kubevirt.io/flavor: medium
        vm.kubevirt.io/os: windows2k25
        vm.kubefirt.io/workload: server
      labels:
        flavor.template.kubevirt.io/large: 'true'
        kubevirt.io/domain: $PREFIX-vmroot
        kubevirt.io/size: large
        vm.kubevirt.io/name: $PREFIX-vmroot-$vm 
    spec:
      domain:
        clock:
          timer:
            hpet:
              present: false
            hyperv: {}
            pit:
              tickPolicy: delay
            rtc:
              tickPolicy: catchup
          timezone: America/Chicago
          utc: {}
        cpu:
          model: host-passthrough
          cores: $CPU_CORES
          sockets: $CPU_SOCKETS
          threads: $CPU_THREADS
        memory:
          guest: $MEMORY
        devices:
          autoattachMemBalloon: false
          blockMultiQueue: true
          disks:
            - name: $PREFIX-rootdisk-$vm
              disk:
                bus: virtio
              bootOrder: 1 
            - name: cloudinitdisk
              disk:
                bus: virtio
          inputs:
            - bus: virtio
              name: tablet
              type: tablet
          tpm: {}
          interfaces:
            - masquerade: {}
              model: virtio
              name: default
          networkInterfaceMultiqueue: true
        features:
          acpi: {}
          apic: {}
          hyperv:
            ipi: {}
            synic: {}
            synictimer:
              direct: {}
            spinlocks:
              spinlocks: 8191
            evmcs:
              enabled: true
            relaxed: {}
            vpindex: {}
            runtime: {}
            tlbflush: {}
            frequencies: {}
            vapic: {}
          smm: {}
        firmware: 
          bootloader:
            efi:
              secureBoot: true
        ioThreads:
          supplementalPoolThreadCount: 8
        ioThreadsPolicy: supplementalPool
        resources:
          requests:
            cpu: $CPU_REQUEST
            memory: $MEMORY_REQUEST
          limits:
            cpu: $CPU_LIMIT
            memory: $MEMORY_LIMIT
      evictionStrategy: LiveMigrate
      hostname: $PREFIX-vmroot-$vm
      networks:
        - name: default
          pod: {}
      terminationGracePeriodSeconds: 180
      volumes:
        - cloudInitNoCloud:
            secretRef:
              name: $CLOUDINIT_SECRET
          name: cloudinitdisk
        - dataVolume:
            name: $PREFIX-rootdisk-$vm
          name: $PREFIX-rootdisk-$vm

EOF
sleep 1
done

echo ""
echo "Starting $TOTAL_VMS VM(s): $PREFIX-$START .. $PREFIX-$END ..."
START_OK=0
START_FAIL=0
for vm in $(seq "$START" "$END"); do
    vm_name="$PREFIX-$vm"
    # Ensure Always (in case create used a different strategy) then start.
    oc patch vm "$vm_name" -n "$NAMESPACE" --type merge \
        -p '{"spec":{"runStrategy":"Always"}}' >/dev/null 2>&1 || true
    if virtctl start "$vm_name" -n "$NAMESPACE" 2>/dev/null; then
        echo "  started: $vm_name"
        START_OK=$((START_OK + 1))
    else
        # Already running / starting is OK with runStrategy: Always
        phase=$(oc get vmi "$vm_name" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)
        printable=$(oc get vm "$vm_name" -n "$NAMESPACE" -o jsonpath='{.status.printableStatus}' 2>/dev/null || true)
        if [[ "$phase" == "Running" ]] || [[ "$printable" == "Running" ]] || [[ "$printable" == "Starting" ]] || [[ "$printable" == "WaitingForVolumeBinding" ]]; then
            echo "  already active: $vm_name (phase=${phase:-n/a} status=${printable:-n/a})"
            START_OK=$((START_OK + 1))
        else
            echo "  warning: could not start $vm_name (phase=${phase:-n/a} status=${printable:-n/a}) — check: oc get vm,vmi $vm_name -n $NAMESPACE"
            START_FAIL=$((START_FAIL + 1))
        fi
    fi
done
echo "Start summary: ok=$START_OK  failed/unknown=$START_FAIL"
echo "  Watch: oc get vms,vmi -n $NAMESPACE | grep $PREFIX"

echo ""
echo "✅ Successfully created $TOTAL_VMS VMs: $PREFIX-$START to $PREFIX-$END"
echo "You can check the status with: oc get vms -n $NAMESPACE | grep $PREFIX"
if [[ -n "$ORIGVM" ]]; then
    echo "Cloned from VM: $ORIGVM (VolumeSnapshot: $GOLDEN_SNAPSHOT)"
fi
echo ""
echo "Passwordless SSH notes:"
echo "  • Injected pubkey from: $SSH_KEY_SOURCE"
echo "  • Cloud-init runs only on first boot. Recreate the VM (delete + re-run) after changing the key/secret."
echo "  • After boot, verify inside the guest: C:\\ProgramData\\winCdisk-cloudinit.log"
echo "    and C:\\ProgramData\\ssh\\administrators_authorized_keys"
echo "  • Example: virtctl ssh Administrator@vmi/$PREFIX-$START -n $NAMESPACE"

# Cleanup auto-created snapshot only if --delete-snapshot was passed
if [[ -n "${ORIG_SNAPSHOT_CREATED:-}" ]]; then
    if [[ "$KEEP_ORIG_SNAPSHOT" -eq 0 ]]; then
        echo ""
        echo "Deleting VolumeSnapshot '$ORIG_SNAPSHOT_CREATED' (--delete-snapshot)..."
        echo "  Warning: if DataVolumes are still cloning, delete may fail or break clones."
        oc delete volumesnapshot "$ORIG_SNAPSHOT_CREATED" -n "$NAMESPACE" --wait=false 2>/dev/null || true
    else
        echo ""
        echo "VolumeSnapshot kept: $ORIG_SNAPSHOT_CREATED"
        echo "  Reuse later: $0 -p ... -c N --golden-snapshot $ORIG_SNAPSHOT_CREATED"
        echo "  Delete when clones are Ready: oc delete volumesnapshot $ORIG_SNAPSHOT_CREATED -n $NAMESPACE"
    fi
fi
