#!/bin/bash

# Enhanced VM creation script with customizable prefix, range, CPU, memory, and storage support
# Usage: ./multivm.sh [OPTIONS]
# Options:
#   -p, --prefix PREFIX    VM name prefix (default: vm)
#   -s, --start NUMBER     Starting VM number (default: 1)
#   -e, --end NUMBER       Ending VM number (default: 1)
#   -c, --count NUMBER     Number of VMs to create (alternative to --end)
#   --cores NUMBER         CPU cores per socket (default: 2)
#   --sockets NUMBER       Number of CPU sockets (default: 2)
#   --threads NUMBER       CPU threads per core (default: 1)
#   --memory SIZE          Memory size with unit (default: 12Gi)
#   --storageclass NAME    Storage class name (default: ocs-storagecluster-ceph-rbd)
#   --imageurl URL         Image URL (default: Fedora Cloud Base 42)
#   -h, --help            Show this help message
#
# Examples:
#   ./multivm.sh -p test -s 1 -e 10                    # Creates test-1 to test-10
#   ./multivm.sh -p worker -c 5                        # Creates worker-1 to worker-5
#   ./multivm.sh -p db --cores 4 --memory 16Gi -c 3    # Creates db-1 to db-3 with 4 cores, 16Gi RAM

# Default values
PREFIX="vm"
START=1
END=1
COUNT=""
CPU_CORES=8
CPU_SOCKETS=1
CPU_THREADS=1
MEMORY="16Gi"
STORAGECLASS="ocs-storagecluster-ceph-rbd-virtualization" # "ocs-storagecluster-ceph-rbd"
IMAGEURL="https://cloud.centos.org/centos/9-stream/x86_64/images/CentOS-Stream-GenericCloud-9-20250520.0.x86_64.qcow2"
OPSTORAGE="70Gi"
DATASTORAGE="300Gi"
GOLDEN_DV=""
GOLDEN_DATA_DV=""
NAMESPACE="default"

# Function to show usage
show_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -p, --prefix PREFIX    VM name prefix (default: vm)"
    echo "  -s, --start NUMBER     Starting VM number (default: 1)"
    echo "  -e, --end NUMBER       Ending VM number (default: 1)"
    echo "  -c, --count NUMBER     Number of VMs to create (alternative to --end)"
    echo "  --cores NUMBER         CPU cores per socket (default: 2)"
    echo "  --sockets NUMBER       Number of CPU sockets (default: 2)"
    echo "  --threads NUMBER       CPU threads per core (default: 1)"
    echo "  --memory SIZE          Memory size with unit (default: 12Gi)"
    echo "  --storageclass NAME    Storage class name (default: ocs-storagecluster-ceph-rbd)"
    echo "  --imageurl URL         Image URL for HTTP import (default: CentOS Stream 9)"
    echo "  --golden-dv NAME       Clone root disk from a pre-imported golden DataVolume instead of HTTP import (much faster)"
    echo "  --golden-data-dv NAME  Clone data disk from a pre-provisioned golden blank DataVolume (no importer pod)"
    echo "  --namespace NAME       Namespace to create VMs in (default: default)"
    echo "  -h, --help            Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 -p test -s 1 -e 10                    # Creates test-1 to test-10"
    echo "  $0 -p worker -c 5                        # Creates worker-1 to worker-5"
    echo "  $0 -p db --cores 4 --memory 16Gi -c 3    # Creates db-1 to db-3 with 4 cores, 16Gi RAM"
    echo "  $0 -p app --sockets 1 --cores 8 -c 2     # Creates app-1 to app-2 with 1 socket, 8 cores"
    echo "  $0 -p web --storageclass fast-ssd -c 3   # Creates web-1 to web-3 with custom storage class"
    echo "  $0 -p db --opstorage 10Gi --datastorage 50Gi --cores 4 --memory 16Gi -c 3    # Creates db-1 to db-3 with 10Gi OS disk and 50Gi data disk, 4 cores, 16Gi RAM"
    echo ""
    echo "CPU Configuration:"
    echo "  Total vCPUs = cores × sockets × threads"
    echo "  Default: 2 cores × 2 sockets × 1 thread = 4 vCPUs"
    echo ""
    echo "Memory Examples:"
    echo "  8Gi, 12Gi, 16Gi, 32Gi, 64Gi"
    echo ""
    echo "Storage Examples:"
    echo "  10Gi, 50Gi, 100Gi, 500Gi"
    echo ""
    echo "Note: You must specify at least one VM to create using -c, -e, or -s options"
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -p|--prefix)
            PREFIX="$2"
            shift 2
            ;;
        -s|--start)
            START="$2"
            shift 2
            ;;
        -e|--end)
            END="$2"
            shift 2
            ;;
        -c|--count)
            COUNT="$2"
            shift 2
            ;;
        --cores)
            CPU_CORES="$2"
            shift 2
            ;;
        --sockets)
            CPU_SOCKETS="$2"
            shift 2
            ;;
        --threads)
            CPU_THREADS="$2"
            shift 2
            ;;
        --memory)
            MEMORY="$2"
            shift 2
            ;;
        --storageclass)
            STORAGECLASS="$2"
            shift 2
            ;;
        --imageurl)
            IMAGEURL="$2"
            shift 2
            ;;
        --opstorage)
            OPSTORAGE="$2"
            shift 2
            ;;
        --datastorage)
            DATASTORAGE="$2"
            shift 2
            ;;
        --golden-dv)
            GOLDEN_DV="$2"
            shift 2
            ;;
        --golden-data-dv)
            GOLDEN_DATA_DV="$2"
            shift 2
            ;;
        --namespace)
            NAMESPACE="$2"
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

# Check if any meaningful options were provided
if [[ -z "$COUNT" ]] && [[ "$START" -eq 1 ]] && [[ "$END" -eq 1 ]]; then
    echo "Error: No options provided. You must specify at least one VM to create."
    echo ""
    show_usage
    exit 1
fi

# Calculate END if COUNT is provided
if [[ -n "$COUNT" ]]; then
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

# Validate memory format (basic check for Gi suffix)
if [[ ! "$MEMORY" =~ ^[0-9]+Gi$ ]]; then
    echo "Error: Memory must be specified with Gi suffix (e.g., 8Gi, 12Gi, 16Gi)"
    exit 1
fi

# Validate storage format (basic check for Gi suffix)
if [[ ! "$OPSTORAGE" =~ ^[0-9]+Gi$ ]] || [[ ! "$DATASTORAGE" =~ ^[0-9]+Gi$ ]]; then
    echo "Error: Storage must be specified with Gi suffix (e.g., 10Gi, 50Gi)"
    exit 1
fi

#if [[ "$OPSTORAGE" -lt 10 ]] || [[ "$DATASTORAGE" -lt 50 ]]; then
#    echo "Error: Storage must be at least 10Gi for OS disk and 50Gi for data disk"
#    exit 1
#fi

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
    echo "Golden DataVolume '$GOLDEN_DV' is ready (phase: Succeeded)"
fi

# If --golden-data-dv is specified, verify the golden blank DataVolume exists and is ready
if [[ -n "$GOLDEN_DATA_DV" ]]; then
    echo "Checking golden data DataVolume '$GOLDEN_DATA_DV' in namespace $NAMESPACE..."
    DV_PHASE=$(oc get dv "$GOLDEN_DATA_DV" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null)
    if [[ -z "$DV_PHASE" ]]; then
        echo "Error: Golden data DataVolume '$GOLDEN_DATA_DV' not found in namespace $NAMESPACE."
        echo "Create it first with: oc apply -f golden-blank-dv.yaml"
        exit 1
    fi
    if [[ "$DV_PHASE" != "Succeeded" ]]; then
        echo "Error: Golden data DataVolume '$GOLDEN_DATA_DV' is not ready (phase: $DV_PHASE)."
        echo "Wait for it to complete: oc get dv $GOLDEN_DATA_DV -n $NAMESPACE -w"
        exit 1
    fi
    echo "Golden data DataVolume '$GOLDEN_DATA_DV' is ready (phase: Succeeded)"
fi

# Calculate total VMs and total vCPUs
TOTAL_VMS=$((END - START + 1))
TOTAL_VCPUS=$((CPU_CORES * CPU_SOCKETS * CPU_THREADS))
CPU_LIMIT=$((TOTAL_VCPUS))
CPU_REQUEST=$((CPU_LIMIT / 16))
if [[ "$CPU_REQUEST" -lt 1 ]]; then
    CPU_REQUEST=1
fi
MEMORY_VALUE="${MEMORY%Gi}"
MEMORY_LIMIT="${MEMORY_VALUE}"
MEMORY_REQUEST=$((MEMORY_LIMIT / 4))
if [[ "$MEMORY_REQUEST" -lt 1 ]]; then
    MEMORY_REQUEST=1
fi
MEMORY_LIMIT="${MEMORY_LIMIT}Gi"
MEMORY_REQUEST="${MEMORY_REQUEST}Gi"

echo "=========================================="
echo "    VM Creation Summary"
echo "=========================================="
echo "Prefix:        $PREFIX"
echo "Range:         $PREFIX-$START to $PREFIX-$END"
echo "Total VMs:     $TOTAL_VMS"
echo "CPU Config:    $CPU_CORES cores × $CPU_SOCKETS sockets × $CPU_THREADS threads = $TOTAL_VCPUS vCPUs"
echo "CPU Requests:  $CPU_REQUEST"
echo "CPU Limits:    $CPU_LIMIT"
echo "Memory:        $MEMORY per VM"
echo "Mem Requests:  $MEMORY_REQUEST"
echo "Mem Limits:    $MEMORY_LIMIT"
echo "Namespace:     $NAMESPACE"
echo "Storage Class: $STORAGECLASS"
echo "OS Disk:      $OPSTORAGE"
echo "Data Disk:    $DATASTORAGE"
if [[ -n "$GOLDEN_DV" ]]; then
    echo "Source:        CLONE from golden DV '$GOLDEN_DV'"
else
    echo "Source:        HTTP import from $IMAGEURL"
fi
echo ""
echo "VM Specifications:"
echo "  • CPU: $TOTAL_VCPUS vCPUs ($CPU_CORES cores × $CPU_SOCKETS sockets × $CPU_THREADS threads)"
echo "  • CPU requests/limits: $CPU_REQUEST / $CPU_LIMIT"
echo "  • Memory requests/limits: $MEMORY_REQUEST / $MEMORY_LIMIT"
if [[ -n "$GOLDEN_DV" ]]; then
    echo "  • OS Disk: $OPSTORAGE (cloned from $GOLDEN_DV)"
else
    echo "  • OS Disk: $OPSTORAGE ($IMAGEURL)"
fi
if [[ -n "$GOLDEN_DATA_DV" ]]; then
    echo "  • Data Disk: $DATASTORAGE (cloned from $GOLDEN_DATA_DV)"
else
    echo "  • Data Disk: $DATASTORAGE (blank)"
fi
echo ""
echo "Starting VM creation in 3 seconds..."
echo "Press Ctrl+C to cancel..."
sleep 3

for vm in $(seq "$START" "$END"); do

if [[ -n "$GOLDEN_DV" ]]; then
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

if [[ -n "$GOLDEN_DATA_DV" ]]; then
DATADISK_SOURCE="source:
          pvc:
            name: $GOLDEN_DATA_DV
            namespace: $NAMESPACE"
else
DATADISK_SOURCE="source:
          blank: {}"
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
        name: $PREFIX-$vm
      spec:
        pvc:
          accessModes:
            - ReadWriteMany
          resources:
            requests:
              storage: $OPSTORAGE
          storageClassName: $STORAGECLASS
          volumeMode: Block
        $ROOTDISK_SOURCE
    - metadata:
        name: $PREFIX-$vm-data
      spec:
        pvc:
          accessModes:
            - ReadWriteMany
          resources:
            requests:
              storage: $DATASTORAGE
          storageClassName: $STORAGECLASS
          volumeMode: Block
        $DATADISK_SOURCE
  runStrategy: Always  
  template:
    metadata:
      annotations:
        vm.kubevirt.io/flavor: large
        vm.kubevirt.io/os: fedora
        vm.kubevirt.io/workload: server
      labels:
        flavor.template.kubevirt.io/large: 'true'
        kubevirt.io/domain: $PREFIX-vmroot
        kubevirt.io/size: large
        vm.kubevirt.io/name: $PREFIX-vmroot-$vm 
    spec:
      accessCredentials:
        - sshPublicKey:
            propagationMethod:
              noCloud: {} 
            source:
              secret:
                secretName: vmkeyroot
      domain:
        cpu:
          cores: $CPU_CORES
          sockets: $CPU_SOCKETS
          threads: $CPU_THREADS
        devices:
          disks:
            - disk:
                bus: virtio
              name: cloudinitdisk
            - disk:
                bus: virtio
              name: $PREFIX-test-disk-$vm
              bootOrder: 1
            - disk:
                bus: virtio
              name: $PREFIX-db-disk-$vm
          inputs:
            - bus: virtio
              name: tablet
              type: tablet
          interfaces:
            - masquerade: {}
              model: virtio
              name: default
          networkInterfaceMultiqueue: true
          rng: {}
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
            userData: |
              #cloud-config
              user: root
              password: fedora
              chpasswd:
                expire: false
              disable_root: false
          name: cloudinitdisk
          disable_root: false
        - dataVolume:
            name: $PREFIX-$vm
          name: $PREFIX-test-disk-$vm
        - dataVolume:
            name: $PREFIX-$vm-data
          name: $PREFIX-db-disk-$vm

EOF
sleep 1
done

echo ""
echo "✅ Successfully created $TOTAL_VMS VMs: $PREFIX-$START to $PREFIX-$END"
echo "You can check the status with: oc get vms -n $NAMESPACE | grep $PREFIX" 
