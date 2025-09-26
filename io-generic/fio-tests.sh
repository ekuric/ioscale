#!/bin/bash

# FIO Remote Testing Script
# This script executes FIO performance tests on remote VMs via SSH
# Supports YAML configuration and multiple VM testing

set -euo pipefail  # Exit on error, undefined vars, pipe failures

# Default configuration
CONFIG_FILE="fio-config.yaml"
DRY_RUN=false
VERBOSE=false
USE_VIRTCTL=""  # Default to auto-detection, can be forced with --ssh-only or --virtctl-only

# Function to check if required tools are installed
check_dependencies() {
    local missing_tools=()
    
    if ! command -v yq &> /dev/null; then
        missing_tools+=("yq")
    fi
    
    # Check dependencies based on mode
    if [[ "$DRY_RUN" == "false" ]]; then
        if [[ "$USE_VIRTCTL" == "true" ]]; then
            # Force virtctl mode - need virtctl and oc
            if ! command -v virtctl &> /dev/null; then
                missing_tools+=("virtctl")
            fi
            if ! command -v oc &> /dev/null; then
                missing_tools+=("oc")
            fi
        elif [[ "$USE_VIRTCTL" == "false" ]]; then
            # Force SSH mode - need ssh
            if ! command -v ssh &> /dev/null; then
                missing_tools+=("ssh")
            fi
        else
            # Auto-detection mode - need both virtctl/oc and ssh
            if ! command -v virtctl &> /dev/null; then
                missing_tools+=("virtctl")
            fi
            if ! command -v oc &> /dev/null; then
                missing_tools+=("oc")
            fi
            if ! command -v ssh &> /dev/null; then
                missing_tools+=("ssh")
            fi
        fi
    fi
    
    # Check if virtctl supports scp command (for result collection)
    if [[ "$DRY_RUN" == "false" ]] && command -v virtctl &> /dev/null; then
        if [[ "$USE_VIRTCTL" == "true" ]] || [[ "$USE_VIRTCTL" != "false" ]]; then
            if ! virtctl help | grep -q "scp"; then
                echo "Warning: virtctl does not support 'scp' command"
                echo "Results will be archived on VMs but not automatically copied to localhost"
                echo "You may need to upgrade virtctl or manually copy results"
            fi
        fi
    fi
    
    if [ ${#missing_tools[@]} -gt 0 ]; then
        echo "Error: The following required tools are missing:"
        for tool in "${missing_tools[@]}"; do
            case $tool in
                yq)
                    echo "  - yq: Install with 'sudo dnf install yq' or 'sudo apt install yq'"
                    ;;
                virtctl)
                    echo "  - virtctl: Install from https://kubevirt.io/user-guide/operations/virtctl_client_tool/"
                    echo "    Or if using kubectl: 'kubectl krew install virt'"
                    ;;
                oc)
                    echo "  - oc: Install OpenShift CLI from https://openshift.com/download"
                    ;;
                ssh)
                    echo "  - ssh: Install with 'sudo dnf install openssh-clients' or 'sudo apt install openssh-client'"
                    ;;
            esac
        done
        echo ""
        echo "Install the missing tools and try again."
        exit 1
    fi
}

# Function to detect if a host is a virtual machine
is_vm_host() {
    local host="$1"
    
    # If USE_VIRTCTL is explicitly set to false, treat all hosts as regular hosts
    if [[ "$USE_VIRTCTL" == "false" ]]; then
        return 1  # Not a VM
    fi
    
    # If USE_VIRTCTL is explicitly set to true, treat all hosts as VMs
    if [[ "$USE_VIRTCTL" == "true" ]]; then
        return 0  # Is a VM
    fi
    
    # Auto-detection logic (when USE_VIRTCTL is not explicitly set)
    # Check if the host exists as a VM in the namespace
    if command -v oc &> /dev/null && [[ -n "$NAMESPACE" ]]; then
        if oc get vm "$host" -n "$NAMESPACE" &>/dev/null; then
            return 0  # Is a VM
        fi
    fi
    
    # Check if the host exists as a VMI in the namespace
    if command -v oc &> /dev/null && [[ -n "$NAMESPACE" ]]; then
        if oc get vmi "$host" -n "$NAMESPACE" &>/dev/null; then
            return 0  # Is a VM
        fi
    fi
    
    # Default to regular host if not found as VM
    return 1  # Not a VM
}

# Function to get SSH command based on host type
get_ssh_command() {
    local host="$1"
    local command="$2"
    
    if is_vm_host "$host"; then
        echo "virtctl -n \"$NAMESPACE\" ssh -t \"-o StrictHostKeyChecking=no\" \"root@vmi/$host\" -c \"$command\""
    else
        echo "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$host \"$command\""
    fi
}

# Function to get SCP command based on host type
get_scp_command() {
    local source="$1"
    local destination="$2"
    
    # Extract hostname from source path (format: root@vmi/hostname:path or root@hostname:path)
    local host
    if [[ "$source" =~ root@vmi/([^:]+): ]]; then
        host="${BASH_REMATCH[1]}"
    elif [[ "$source" =~ root@([^:]+): ]]; then
        host="${BASH_REMATCH[1]}"
    else
        log_error "Cannot extract hostname from source: $source"
        return 1
    fi
    
    if is_vm_host "$host"; then
        echo "virtctl -n \"$NAMESPACE\" scp \"$source\" \"$destination\""
    else
        # For SSH mode, convert virtctl format to regular SSH format
        # Convert "root@vmi/hostname:path" to "root@hostname:path"
        local ssh_source="${source/root@vmi\//root@}"
        echo "scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \"$ssh_source\" \"$destination\""
    fi
}

# Smart host selection function
get_vm_hosts() {
    local config_file="$1"
    local hosts=""
    
    # Method 1: Host pattern expansion (e.g., vm{1..200})
    local host_pattern=$(yq eval '.vm.host_pattern' "$config_file")
    if [[ "$host_pattern" != "null" && -n "$host_pattern" ]]; then
        log_info "Using host pattern: $host_pattern"
        
        # Use bash expansion for patterns like vm{1..200}
        # Note: This works in bash with brace expansion enabled
        if [[ "$host_pattern" =~ \{[0-9]+\.\.[0-9]+\} ]]; then
            # Enable brace expansion and expand the pattern
            set +o nounset  # Temporarily disable for expansion
            hosts=$(eval echo "$host_pattern")
            set -o nounset  # Re-enable
            log_info "Expanded pattern to: $(echo $hosts | wc -w) hosts"
            echo "$hosts"
            return 0
        fi
    fi
    
    # Method 2: Label-based selection from Kubernetes
    local host_labels=$(yq eval '.vm.host_labels' "$config_file")
    if [[ "$host_labels" != "null" && -n "$host_labels" ]]; then
        # Check if SSH-only mode is enabled
        if [[ "$USE_VIRTCTL" == "false" ]]; then
            log_error "Label-based host selection is not supported in SSH-only mode. Use hosts, host_pattern, or host_file instead."
            exit 1
        fi
        
        log_info "Using label selector: $host_labels"
        
        if [[ "$DRY_RUN" == "false" ]] && command -v oc &> /dev/null; then
            # Query OpenShift/Kubernetes for VMs with specified labels
            hosts=$(oc get vms -n "$NAMESPACE" -l "$host_labels" -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}' 2>/dev/null || true)
            if [[ -n "$hosts" ]]; then
                log_info "Found $(echo $hosts | wc -w) VMs matching labels: $host_labels"
                echo "$hosts"
                return 0
            else
                log_warn "No VMs found matching labels: $host_labels"
            fi
        else
            log_info "Dry-run mode: Would query VMs with labels: $host_labels"
            echo "example-vm1 example-vm2"  # Placeholder for dry-run
            return 0
        fi
    fi
    
    # Method 3: External host file
    local host_file=$(yq eval '.vm.host_file' "$config_file")
    if [[ "$host_file" != "null" && -n "$host_file" ]]; then
        log_info "Using host file: $host_file"
        
        if [[ -f "$host_file" ]]; then
            # Read hosts from file, skip comments and empty lines
            local all_hosts=""
            while IFS= read -r line; do
                # Skip comments and empty lines
                if [[ "$line" =~ ^[[:space:]]*# ]] || [[ -z "${line// }" ]]; then
                    continue
                fi
                
                # Check if line contains a pattern (has braces)
                if [[ "$line" == *"{"*"}"* ]]; then
                    # Expand the pattern
                    local expanded=$(eval echo "$line")
                    all_hosts="$all_hosts $expanded"
                else
                    # Regular hostname
                    all_hosts="$all_hosts $line"
                fi
            done < "$host_file"
            
            # Clean up the hosts list
            hosts=$(echo "$all_hosts" | xargs)
            if [[ -n "$hosts" ]]; then
                log_info "Loaded $(echo $hosts | wc -w) hosts from file: $host_file"
                echo "$hosts"
                return 0
            else
                log_warn "No valid hosts found in file: $host_file (all lines are comments or empty)"
            fi
        else
            log_error "Host file not found: $host_file"
            exit 1
        fi
    fi
    
    # Method 4: Simple host list (fallback)
    hosts=$(yq eval '.vm.hosts' "$config_file")
    if [[ "$hosts" != "null" && -n "$hosts" ]]; then
        log_info "Using simple host list: $hosts"
        echo "$hosts"
        return 0
    fi
    
    # No hosts found
    log_error "No hosts specified in configuration. Use one of: hosts, host_pattern, host_labels, or host_file"
    exit 1
}

# Function to check if a host matches a pattern and get its device
get_device_from_pattern() {
    local host="$1"
    local pattern="$2"
    local config_file="$3"
    
    # Check if the pattern contains braces (indicating a host pattern)
    if [[ "$pattern" == *"{"*"}"* ]]; then
        # Expand the pattern and check if our host matches
        local expanded_hosts=$(eval echo "$pattern")
        for expanded_host in $expanded_hosts; do
            if [[ "$expanded_host" == "$host" ]]; then
                # Found a match, get the device for this pattern
                local pattern_device=$(yq eval ".storage.devices.\"${pattern}\"" "$config_file")
                if [[ "$pattern_device" != "null" && "$pattern_device" != "" && -n "$pattern_device" ]]; then
                    echo "$pattern_device"
                    return 0
                fi
            fi
        done
    fi
    return 1
}

# Function to get storage device for a specific host
get_device_for_host() {
    local host="$1"
    local config_file="$2"
    
    # Get per-host device configuration (MANDATORY - no fallback for safety)
    # Quote the hostname to handle hostnames with dots or special characters
    local per_host_device=$(yq eval ".storage.devices.\"${host}\"" "$config_file")
    
    # Check if device is properly configured (not null and not empty)
    if [[ "$per_host_device" != "null" && "$per_host_device" != "" && -n "$per_host_device" ]]; then
        echo "$per_host_device"
        return 0
    fi
    
    # If no exact match, try host patterns (e.g., vm-{1..5}: "vdc")
    local devices_section=$(yq eval '.storage.devices' "$config_file")
    if [[ "$devices_section" != "null" ]]; then
        # Get all keys from devices section and check for patterns
        local all_keys=$(yq eval '.storage.devices | keys | .[]' "$config_file")
        for pattern in $all_keys; do
            if get_device_from_pattern "$host" "$pattern" "$config_file"; then
                return 0
            fi
        done
    fi
    
    # No device found - this is an error for safety
    return 1
}

# Function to read YAML configuration
read_config() {
    local config_file="$1"
    
    if [[ ! -f "$config_file" ]]; then
        echo "Error: Configuration file '$config_file' not found"
        exit 1
    fi
    
    # Read configuration values from YAML file
    # Namespace is only relevant for virtctl/oc operations
    if [[ "$USE_VIRTCTL" != "false" ]]; then
        NAMESPACE=$(yq eval '.vm.namespace' "$config_file")
    else
        NAMESPACE="N/A"  # Not applicable for SSH-only mode
    fi
    
    # Smart host selection with multiple methods
    VM_HOSTS=$(get_vm_hosts "$config_file")
    
    # Device configuration - per-host devices only (no global fallback for safety)
    MOUNT_POINT=$(yq eval '.storage.mount_point' "$config_file")
    FILESYSTEM=$(yq eval '.storage.filesystem' "$config_file")
    
    # FIO Test Configuration
    TEST_SIZE=$(yq eval '.fio.test_size' "$config_file")
    TEST_RUNTIME=$(yq eval '.fio.runtime' "$config_file")
    BLOCK_SIZES=$(yq eval '.fio.block_sizes' "$config_file")
    IO_PATTERNS=$(yq eval '.fio.io_patterns' "$config_file")
    NUMJOBS=$(yq eval '.fio.numjobs' "$config_file")
    IODEPTH=$(yq eval '.fio.iodepth' "$config_file")
    DIRECT_IO=$(yq eval '.fio.direct_io' "$config_file")
    
    # Output Configuration
    OUTPUT_DIR=$(yq eval '.output.directory' "$config_file")
    OUTPUT_FORMAT=$(yq eval '.output.format' "$config_file")
    
    # Handle null values
    if [[ "$NAMESPACE" == "null" ]]; then
        NAMESPACE="default"
    fi
    if [[ "$MOUNT_POINT" == "null" ]]; then
        MOUNT_POINT="/root/tests/data"
    fi
    if [[ "$FILESYSTEM" == "null" ]]; then
        FILESYSTEM="xfs"
    fi
    if [[ "$OUTPUT_DIR" == "null" ]]; then
        OUTPUT_DIR="/root/fio-results"
    fi
    if [[ "$OUTPUT_FORMAT" == "null" ]]; then
        OUTPUT_FORMAT="json+"
    fi
}

# Logging functions
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2
}

log_error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2
}

log_info() { log "INFO" "$@"; }
log_warn() { log "WARN" "$@"; }

# Error handling
handle_error() {
    local exit_code=$?
    log_error "Script failed at line $1 with exit code $exit_code"
    cleanup
    exit $exit_code
}

# Cleanup function
cleanup() {
    log_info "Performing cleanup..."
    # Add any cleanup operations here
}

trap 'handle_error $LINENO' ERR

# Usage function
usage() {
    cat << EOF
FIO Remote Testing Script

DESCRIPTION:
    This script executes FIO performance tests on remote VMs via SSH.
    Configuration is read from a YAML file.

USAGE:
    $0 [-h] [-c config_file] [-v] [--dry-run]

OPTIONS:
    -h                  Show this help message
    -c <config_file>    Path to YAML configuration file (default: fio-config.yaml)
    -v                  Verbose output
    --debug             Show detailed configuration parsing debug information
    --dry-run           Validate configuration and show what would be done without executing

EXAMPLES:
    $0                          # Use default fio-config.yaml
    $0 -c test-config.yaml      # Use custom configuration file
    $0 -c config.yaml -v        # Use default config with verbose output

YAML CONFIGURATION:
    See fio-config.yaml for configuration file format and examples.

NOTES:
    - Requires 'yq' tool for YAML parsing
    - Script requires virtctl and oc for VM access
    - All operations are performed as root on target VMs
    - WARNING: This script formats storage devices - ensure correct configuration
EOF
}

# Input validation
validate_inputs() {
    # Validate device configuration for each host (MANDATORY for safety)
    for host in $VM_HOSTS; do
        if ! get_device_for_host "$host" "$CONFIG_FILE" >/dev/null; then
            log_error "CRITICAL: No storage device specified for host '$host'"
            log_error "Each host MUST have a device specified in storage.devices.$host"
            log_error "This is mandatory for safety - no fallback devices allowed"
            log_error "Example: storage.devices.$host: \"sdb\""
            exit 1
        fi
    done
    
    # Validate VMs exist (skip in dry-run mode and SSH-only mode)
    if [[ "$DRY_RUN" == "false" && "$USE_VIRTCTL" != "false" ]]; then
        for host in $VM_HOSTS; do
            # Only validate hosts that are detected as VMs
            if is_vm_host "$host"; then
                if ! oc get vm "$host" -n "$NAMESPACE" &> /dev/null; then
                    log_error "Virtual machine '$host' not found in namespace '$NAMESPACE'"
                    exit 1
                fi
            fi
        done
    elif [[ "$DRY_RUN" == "true" ]]; then
        log_info "Skipping VM validation in dry-run mode"
    elif [[ "$USE_VIRTCTL" == "false" ]]; then
        log_info "Skipping VM validation in SSH-only mode"
    fi
}

# Display configuration
# Function to show connection method for each host
show_host_connection_methods() {
    if [[ "$USE_VIRTCTL" == "true" ]] || [[ "$USE_VIRTCTL" == "false" ]]; then
        # Fixed mode - all hosts use the same method
        return 0
    fi
    
    # Auto-detection mode - show method for each host
    log_info "Host connection methods (auto-detection):"
    for host in $VM_HOSTS; do
        if is_vm_host "$host"; then
            log_info "  $host: virtctl (VM detected)"
        else
            log_info "  $host: SSH (regular host)"
        fi
    done
}

display_config() {
    log_info "Configuration loaded from: $CONFIG_FILE"
    log_info "VMs: $VM_HOSTS"
    if [[ "$USE_VIRTCTL" != "false" ]]; then
        log_info "Namespace: $NAMESPACE"
    else
        log_info "Namespace: N/A (SSH-only mode)"
    fi
    show_host_connection_methods
    # Display device configuration
    log_info "Storage device configuration:"
    for host in $VM_HOSTS; do
        local host_device=$(get_device_for_host "$host" "$CONFIG_FILE")
        log_info "  $host: /dev/$host_device"
    done
    log_info "Mount point: $MOUNT_POINT"
    log_info "Filesystem: $FILESYSTEM"
    log_info "Test size: $TEST_SIZE"
    log_info "Runtime: ${TEST_RUNTIME}s"
    log_info "Block sizes: $BLOCK_SIZES"
    log_info "I/O patterns: $IO_PATTERNS"
    log_info "Number of jobs: $NUMJOBS"
    log_info "I/O depth: $IODEPTH"
    log_info "Direct I/O: $DIRECT_IO"
    log_info "Output directory: $OUTPUT_DIR"
}

# Debug configuration parsing
debug_config_parsing() {
    log_info "=== CONFIGURATION PARSING DEBUG ==="
    
    # Show host selection method
    log_info "Host Selection Analysis:"
    local host_pattern=$(yq eval '.vm.host_pattern' "$CONFIG_FILE")
    local host_labels=$(yq eval '.vm.host_labels' "$CONFIG_FILE")
    local host_file=$(yq eval '.vm.host_file' "$CONFIG_FILE")
    local hosts=$(yq eval '.vm.hosts' "$CONFIG_FILE")
    
    log_info "  host_pattern: '$host_pattern'"
    log_info "  host_labels: '$host_labels'"
    log_info "  host_file: '$host_file'"
    log_info "  hosts: '$hosts'"
    log_info "  Final VM_HOSTS: '$VM_HOSTS' ($(echo $VM_HOSTS | wc -w) hosts)"
    
    # Show raw FIO config values
    log_info "Raw YAML values:"
    if command -v yq &> /dev/null && [[ -f "$CONFIG_FILE" ]]; then
        yq eval '.fio.block_sizes' "$CONFIG_FILE" | while read line; do
            log_info "  block_sizes: '$line'"
        done
        yq eval '.fio.io_patterns' "$CONFIG_FILE" | while read line; do
            log_info "  io_patterns: '$line'"
        done
    fi
    
    # Show how arrays are parsed
    local test_bs_array=($BLOCK_SIZES)
    local test_pattern_array=($IO_PATTERNS)
    
    log_info "Array parsing results:"
    log_info "  Block sizes array has ${#test_bs_array[@]} elements:"
    for i in "${!test_bs_array[@]}"; do
        log_info "    [$i] = '${test_bs_array[$i]}'"
    done
    
    log_info "  IO patterns array has ${#test_pattern_array[@]} elements:"
    for i in "${!test_pattern_array[@]}"; do
        log_info "    [$i] = '${test_pattern_array[$i]}'"
    done
    
    log_info "Expected test matrix: ${#test_bs_array[@]} block sizes × ${#test_pattern_array[@]} patterns = $((${#test_bs_array[@]} * ${#test_pattern_array[@]})) total tests"
    log_info "=== END DEBUG ==="
}

# Execute SSH command with error handling
execute_ssh() {
    local host="$1"
    local command="$2"
    local description="${3:-command}"
    
    if [[ "$VERBOSE" == "true" ]]; then
        log_info "Executing on $host: $description"
        log_info "Command: $command"
    else
        log_info "Executing on $host: $description"
    fi
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "DRY-RUN: Would execute on $host: $command"
        return 0
    fi
    
    local ssh_cmd
    ssh_cmd=$(get_ssh_command "$host" "$command")
    
    if ! eval "$ssh_cmd"; then
        log_error "Failed to execute '$description' on $host"
        return 1
    fi
}

# Execute SSH command in background
execute_ssh_background() {
    local host="$1"
    local command="$2"
    local description="${3:-background command}"
    
    log_info "Starting background task on $host: $description"
    
    if [[ "$VERBOSE" == "true" ]]; then
        log_info "Background command on $host: $command"
    fi
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "DRY-RUN: Would execute in background on $host: $command"
        return 0
    fi
    
    # Use a subshell to prevent SSH failures from killing the main script
    (
        local ssh_cmd
        ssh_cmd=$(get_ssh_command "$host" "$command")
        
        if ! eval "$ssh_cmd" 2>&1; then
            log_error "Background SSH command failed on $host: $description"
            exit 1
        fi
    ) &
}

# Install FIO and dependencies on VMs
install_dependencies() {
    log_info "Installing FIO and dependencies on VMs..."
    #  better to use image with fio and xfsprogs installed  
    local bg_pids=()
    for host in $VM_HOSTS; do
        execute_ssh_background "$host" \
            "dnf update -y && dnf install -y fio xfsprogs util-linux" \
            "Installing FIO and filesystem tools"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    
    wait_for_background_jobs "dependency installation" "${bg_pids[@]}"
}

# Prepare storage on VMs
prepare_storage() {
    log_info "Preparing storage on VMs with parallel execution..."
    
    # Step 1: Create test directories on all hosts in parallel
    log_info "Step 1/5: Creating test directories on all hosts..."
    local bg_pids=()
    for host in $VM_HOSTS; do
        execute_ssh_background "$host" \
            "mkdir -p $OUTPUT_DIR $MOUNT_POINT" \
            "Creating test directories"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    
    wait_for_background_jobs "directory creation" "${bg_pids[@]}"
    
    # Step 2: Validate test devices on all hosts in parallel
    log_info "Step 2/5: Validating test devices on all hosts..."
    bg_pids=()
    for host in $VM_HOSTS; do
        local host_device=$(get_device_for_host "$host" "$CONFIG_FILE")
        execute_ssh_background "$host" \
            "if [[ ! -b /dev/$host_device ]]; then
                 echo 'ERROR: Block device /dev/$host_device not found'
                 exit 1
             fi
             echo 'Found block device /dev/$host_device'
             lsblk /dev/$host_device" \
            "Validating test device"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    
    wait_for_background_jobs "device validation" "${bg_pids[@]}"
    
    # Step 3: Unmount existing mounts on all hosts in parallel
    log_info "Step 3/5: Unmounting existing mounts on all hosts..."
    bg_pids=()
    for host in $VM_HOSTS; do
        execute_ssh_background "$host" \
            "if mountpoint -q $MOUNT_POINT; then
                 echo 'Unmounting $MOUNT_POINT'
                 umount $MOUNT_POINT || true
             else
                 echo 'Mount point $MOUNT_POINT is not mounted'
             fi" \
            "Unmounting existing mount"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    
    wait_for_background_jobs "unmounting existing mounts" "${bg_pids[@]}"
    
    # Step 4: Format devices on all hosts in parallel
    log_info "Step 4/5: Formatting devices on all hosts (WARNING: destructive operation)..."
    bg_pids=()
    for host in $VM_HOSTS; do
        local host_device=$(get_device_for_host "$host" "$CONFIG_FILE")
        execute_ssh_background "$host" \
            "echo 'WARNING: Formatting /dev/$host_device with $FILESYSTEM'
             mkfs.$FILESYSTEM -f /dev/$host_device" \
            "Formatting test device"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    
    wait_for_background_jobs "device formatting" "${bg_pids[@]}"
    
    # Step 5: Mount devices on all hosts in parallel
    log_info "Step 5/5: Mounting devices on all hosts..."
    bg_pids=()
    for host in $VM_HOSTS; do
        local host_device=$(get_device_for_host "$host" "$CONFIG_FILE")
        execute_ssh_background "$host" \
            "mount /dev/$host_device $MOUNT_POINT" \
            "Mounting test device"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    
    wait_for_background_jobs "device mounting" "${bg_pids[@]}"
    
    log_info "Storage preparation completed on all hosts!"
}

# Write test dataset
# NOTE: Using --name=testfile ensures all subsequent tests operate on the same file
write_test_data() {
    log_info "Writing initial test dataset..."
    
    local bg_pids=()
    for host in $VM_HOSTS; do
        execute_ssh_background "$host" \
            "cd $OUTPUT_DIR && fio \
                --name=testfile \
                --directory=$MOUNT_POINT \
                --size=$TEST_SIZE \
                --rw=randwrite \
                --bs=4k \
                --runtime=300 \
                --direct=$DIRECT_IO \
                --numjobs=$NUMJOBS \
                --time_based=1 \
                --iodepth=$IODEPTH \
                --output-format=$OUTPUT_FORMAT \
                --output=write_dataset.json" \
            "Writing test dataset"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    
    wait_for_background_jobs "test dataset writing" "${bg_pids[@]}"
}

# Run FIO performance tests
# NOTE: Using --name=testfile (same as write phase) ensures tests operate on the same data
run_fio_tests() {
    log_info "Running FIO performance tests..."
    
    local test_counter=1
    
    # Convert space-separated strings to arrays with debugging
    log_info "Block sizes from config: '$BLOCK_SIZES'"
    log_info "IO patterns from config: '$IO_PATTERNS'"
    
    local bs_array=($BLOCK_SIZES)
    local pattern_array=($IO_PATTERNS)
    
    log_info "Parsed block sizes: ${#bs_array[@]} items: ${bs_array[*]}"
    log_info "Parsed IO patterns: ${#pattern_array[@]} items: ${pattern_array[*]}"
    
    for bs in "${bs_array[@]}"; do
        log_info "Starting block size iteration: $bs"
        
        for pattern in "${pattern_array[@]}"; do
            log_info "Running test $test_counter: $pattern with block size $bs"
            
            # Track background jobs for this test iteration
            local bg_pids=()
            
            for host in $VM_HOSTS; do
                local test_name="fio-test-${pattern}-bs-${bs}"
                log_info "Starting FIO test on $host: $test_name"
                
                # Run in background and capture PID
                execute_ssh_background "$host" \
                    "cd $OUTPUT_DIR && fio \
                        --name=testfile \
                        --directory=$MOUNT_POINT \
                        --size=$TEST_SIZE \
                        --rw=$pattern \
                        --bs=$bs \
                        --runtime=$TEST_RUNTIME \
                        --direct=$DIRECT_IO \
                        --numjobs=$NUMJOBS \
                        --time_based=1 \
                        --iodepth=$IODEPTH \
                        --output-format=$OUTPUT_FORMAT \
                        --output=${test_name}.json" \
                    "FIO test: $pattern, block size: $bs"
                
                # Store the PID of the last background job
                if [[ "$DRY_RUN" == "false" ]]; then
                    bg_pids+=($!)
                fi
            done
            
            # Wait for all background jobs and check their status
            log_info "Waiting for all FIO tests to complete for $pattern with block size $bs..."
            if [[ "$DRY_RUN" == "false" ]]; then
                local failed_jobs=0
                for pid in "${bg_pids[@]}"; do
                    if wait "$pid"; then
                        log_info "Background job $pid completed successfully"
                    else
                        log_error "Background job $pid failed"
                        ((failed_jobs++))
                    fi
                done
                
                if [[ $failed_jobs -gt 0 ]]; then
                    log_warn "$failed_jobs background jobs failed for test: $pattern with block size $bs"
                else
                    log_info "All background jobs completed successfully for test: $pattern with block size $bs"
                fi
            fi
            
            ((test_counter++))
            log_info "Completed test $((test_counter-1)): $pattern with block size $bs"
        done
        
        log_info "Completed all patterns for block size: $bs"
    done
    
    log_info "Completed all FIO performance tests"
}

# Collect test results
collect_results() {
    local results_dir="${1:-./fio-results-$(date +%Y%m%d-%H%M%S)-machines_$(echo $VM_HOSTS | wc -w)}"
    
    log_info "Collecting test results..."
    mkdir -p "$results_dir"
    
    for host in $VM_HOSTS; do
        local host_dir="$results_dir/$host"
        mkdir -p "$host_dir"
        
        log_info "Collecting results from $host..."
        
        # Create results archive on VM
        execute_ssh "$host" \
            "cd $OUTPUT_DIR && tar czf fio-results.tar.gz *.json" \
            "Creating results archive"
        
        # Copy results from VM to localhost using virtctl scp
        if [[ "$DRY_RUN" == "true" ]]; then
            log_info "DRY-RUN: Would copy results from $host to $host_dir/"
        else
            log_info "Copying results from $host to localhost..."
            local scp_cmd
            scp_cmd=$(get_scp_command "root@vmi/$host:$OUTPUT_DIR/fio-results.tar.gz" "$host_dir/fio-results.tar.gz")
            
            if eval "$scp_cmd" 2>/dev/null; then
                log_info "Successfully copied results from $host using virtctl scp"
                
                # Extract results locally for easier access
                if command -v tar &> /dev/null; then
                    cd "$host_dir"
                    if tar -xzf fio-results.tar.gz; then
                        log_info "Extracted results for $host"
                        # Remove the tar file to save space, keep extracted JSON files
                        rm -f fio-results.tar.gz
                    else
                        log_warn "Failed to extract results for $host, keeping tar file"
                    fi
                    cd - > /dev/null
                fi
            else
                log_warn "virtctl scp failed, trying alternative method..."
                # Fallback: use ssh with cat to copy file
                local ssh_cmd
                ssh_cmd=$(get_ssh_command "$host" "cat $OUTPUT_DIR/fio-results.tar.gz")
                
                if eval "$ssh_cmd" > "$host_dir/fio-results.tar.gz" 2>/dev/null; then
                    log_info "Successfully copied results from $host using ssh+cat fallback"
                    
                    # Extract results locally
                    if command -v tar &> /dev/null; then
                        cd "$host_dir"
                        if tar -xzf fio-results.tar.gz; then
                            log_info "Extracted results for $host"
                            rm -f fio-results.tar.gz
                        else
                            log_warn "Failed to extract results for $host, keeping tar file"
                        fi
                        cd - > /dev/null
                    fi
                else
                    log_error "Failed to copy results from $host using both methods"
                    log_info "Results are still available on $host at $OUTPUT_DIR/fio-results.tar.gz"
                    if [[ "$USE_VIRTCTL" == "true" ]]; then
                        log_info "Manual copy command: virtctl -n $NAMESPACE ssh root@vmi/$host -c 'cat $OUTPUT_DIR/fio-results.tar.gz' > $host_dir/fio-results.tar.gz"
                    else
                        log_info "Manual copy command: ssh root@$host 'cat $OUTPUT_DIR/fio-results.tar.gz' > $host_dir/fio-results.tar.gz"
                    fi
                fi
            fi
        fi
    done
    
    if [[ "$DRY_RUN" == "false" ]]; then
        log_info "All results collected in: $results_dir"
        log_info "Results structure:"
        if command -v tree &> /dev/null; then
            tree "$results_dir" 2>/dev/null || ls -la "$results_dir"/*
        else
            find "$results_dir" -type f -name "*.json" | head -10
            local total_files=$(find "$results_dir" -type f -name "*.json" | wc -l)
            log_info "Total JSON result files: $total_files"
        fi
    fi
}

# Wait for background jobs with error tracking (same as MariaDB script)
wait_for_background_jobs() {
    local description="$1"
    local pids=("${@:2}")
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "DRY-RUN: Would wait for background jobs: $description"
        return 0
    fi
    
    if [[ ${#pids[@]} -eq 0 ]]; then
        log_info "No background jobs to wait for: $description"
        return 0
    fi
    
    log_info "Waiting for ${#pids[@]} background jobs to complete: $description"
    
    local failed_jobs=0
    for pid in "${pids[@]}"; do
        if wait "$pid"; then
            log_info "Background job $pid completed successfully"
        else
            log_error "Background job $pid failed"
            ((failed_jobs++))
        fi
    done
    
    if [[ $failed_jobs -gt 0 ]]; then
        log_error "$failed_jobs/${#pids[@]} background jobs failed for: $description"
        return 1
    else
        log_info "All background jobs completed successfully: $description"
        return 0
    fi
}

# Cleanup test environment (following MariaDB pattern)
cleanup_storage() {
    log_info "Cleaning up storage on VMs..."
    
    # Step 1: Cleanup storage mount points in parallel
    log_info "Step 1/3: Cleaning up storage mount points on all hosts..."
    
    local bg_pids=()
    for host in $VM_HOSTS; do
        execute_ssh_background "$host" \
            "# Check if mount point is mounted and unmount it
             if mountpoint -q $MOUNT_POINT 2>/dev/null; then
                 echo 'Unmounting $MOUNT_POINT'
                 umount $MOUNT_POINT && echo 'Successfully unmounted $MOUNT_POINT'
             else
                 echo 'Mount point $MOUNT_POINT is not mounted or does not exist'
             fi
             
            # Optional: Clean up test data directory if it's empty
            if [[ -d $MOUNT_POINT ]]; then
                if ls -A $MOUNT_POINT 2>/dev/null | grep -q .; then
                    echo 'Directory $MOUNT_POINT is not empty, keeping it'
                else
                    echo 'Removing empty test directory $MOUNT_POINT'
                    rmdir $MOUNT_POINT 2>/dev/null || true
                fi
            fi" \
            "Cleaning up storage mount points"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    
    wait_for_background_jobs "storage cleanup" "${bg_pids[@]}"
    
    # Step 2: Clean up any remaining FIO processes in parallel
    log_info "Step 2/3: Cleaning up any remaining FIO processes on all hosts..."
    
    # ignoring this for now, as it's not needed 
    #bg_pids=()
    # for host in $VM_HOSTS; do
    #    execute_ssh_background "$host" \
    #        "# Clean up any remaining FIO processes (safety measure)
    #         pkill -f 'fio.*testfile' 2>/dev/null || true
    #         echo 'FIO process cleanup completed'" \
    #        "Cleaning up FIO processes"
    #    if [[ "$DRY_RUN" == "false" ]]; then
    #        bg_pids+=($!)
    #   fi
    #done
    
    #wait_for_background_jobs "FIO process cleanup" "${bg_pids[@]}"
    
    # Step 3: Clean up test results in parallel
    log_info "Step 3/3: Cleaning up test results on all hosts..."
    
    bg_pids=()
    for host in $VM_HOSTS; do
        execute_ssh_background "$host" \
            "# Clean up test results
             rm -rf $OUTPUT_DIR/*.json 2>/dev/null || true
             echo 'Test results cleanup completed'" \
            "Cleaning up test results"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    
    wait_for_background_jobs "test results cleanup" "${bg_pids[@]}"
}

# Main function
main() {
    log_info "Starting FIO remote testing script"
    
    # Show connection mode
    if [[ "$USE_VIRTCTL" == "true" ]]; then
        log_info "Using virtctl for all host connections"
    elif [[ "$USE_VIRTCTL" == "false" ]]; then
        log_info "Using direct SSH for all host connections"
    else
        log_info "Using auto-detection: virtctl for VMs, SSH for regular hosts"
    fi
    
    check_dependencies
    read_config "$CONFIG_FILE"
    display_config
    
    # Show debug information if requested
    if [[ "$DEBUG_CONFIG" == "true" ]]; then
        debug_config_parsing
    fi
    
    validate_inputs
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "DRY RUN MODE: Configuration validated successfully"
        log_info "Would execute the following steps:"
        log_info "  1. Install FIO and dependencies on VMs"
        log_info "  2. Prepare storage (format and mount devices) - 5 parallel steps"
        log_info "  3. Write initial test dataset"
        log_info "  4. Run FIO performance tests with different patterns and block sizes"
        log_info "  5. Collect test results"
        log_info "  6. Clean up test environment (3 steps: storage, processes, results)"
        log_info ""
        log_info "WARNING: Step 2 will format the specified storage device!"
        log_info "Use without --dry-run to execute the actual tests"
        return 0
    fi
    
    # Confirmation for destructive operations
    echo ""
    log_warn "WARNING: This script will format storage devices on all hosts!"
    log_warn "Hosts: $VM_HOSTS"
    log_warn "Devices to be formatted:"
    for host in $VM_HOSTS; do
        local host_device=$(get_device_for_host "$host" "$CONFIG_FILE")
        log_warn "  $host: /dev/$host_device"
    done
    echo ""
    read -p "Are you sure you want to continue? (yes/no): " confirm
    
    if [[ "$confirm" != "yes" ]]; then
        log_info "Operation cancelled by user"
        exit 0
    fi
    
    install_dependencies
    prepare_storage
    write_test_data
    run_fio_tests
    
    # Collect results - create directory name once
    local results_timestamp=$(date +%Y%m%d-%H%M%S)
    local machine_count=$(echo $VM_HOSTS | wc -w)
    local final_results_dir="./fio-results-$results_timestamp-machines_$machine_count"
    
    collect_results "$final_results_dir"
    cleanup_storage
    
    log_info "FIO performance testing completed successfully"
    log_info "Results have been copied to localhost: $final_results_dir"
    log_info "Each VM's results are in separate subdirectories with extracted JSON files"
}

# Usage function
usage() {
    cat << EOF
FIO Remote Testing Script

DESCRIPTION:
    This script executes FIO performance tests on remote VMs via SSH.
    Configuration is read from a YAML file.

USAGE:
    $0 [-h] [-c config_file] [-v] [--dry-run] [--ssh-only] [--virtctl-only]

OPTIONS:
    -h                  Show this help message
    -c <config_file>    Path to YAML configuration file (default: fio-config.yaml)
    -v                  Verbose output
    --dry-run           Validate configuration and show what would be done without executing
    --ssh-only          Force SSH for all hosts (requires direct SSH access)
    --virtctl-only      Force virtctl for all hosts (requires virtctl/oc access)
                        Default: Auto-detect host type (virtctl for VMs, SSH for regular hosts)

EXAMPLES:
    $0                          # Auto-detect: virtctl for VMs, SSH for regular hosts
    $0 -c test-config.yaml      # Use custom config with auto-detection
    $0 -c config.yaml -v        # Use default config with verbose output
    $0 --ssh-only               # Force SSH for all hosts
    $0 --virtctl-only           # Force virtctl for all hosts
    $0 --ssh-only --dry-run     # Test SSH mode without executing

YAML CONFIGURATION:
    See fio-config.yaml for configuration file format and examples.

NOTES:
    - Requires 'yq' tool for YAML parsing
    - Default mode (auto-detection): requires both virtctl/oc and ssh
    - Force virtctl mode: requires virtctl and oc for all hosts
    - Force SSH mode: requires direct SSH access to all hosts
    - Mixed host support: can test both VMs and regular hosts in same run
    - All operations are performed as root on target hosts
    - Results are saved to: fio-results-YYYYMMDD-HHMMSS-machines_N (N = number of hosts)
    - WARNING: This script formats storage devices - ensure correct configuration
EOF
}

# Parse command line arguments
DEBUG_CONFIG=false
while [ $# -gt 0 ]; do
    case $1 in
        -h|--help)
            usage
            exit 0
            ;;
        -c|--config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        -v|--verbose)
            VERBOSE=true
            set -x
            shift
            ;;
        --debug)
            DEBUG_CONFIG=true
            VERBOSE=true  # Enable verbose when debug is on
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --ssh-only)
            USE_VIRTCTL=false
            shift
            ;;
        --virtctl-only)
            USE_VIRTCTL=true
            shift
            ;;
        *)
            log_error "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

# Run main function
main 