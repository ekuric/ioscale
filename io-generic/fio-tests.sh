#!/bin/bash

# FIO Remote Testing Script
# This script executes FIO performance tests on remote VMs via SSH
# Supports YAML configuration and multiple VM testing

set -euo pipefail  # Exit on error, undefined vars, pipe failures

# Default configuration
CONFIG_FILE="fio-config.yaml"
DRY_RUN=false
VERBOSE=false

# Function to check if required tools are installed
check_dependencies() {
    local missing_tools=()
    
    if ! command -v yq &> /dev/null; then
        missing_tools+=("yq")
    fi
    
    if [[ "$DRY_RUN" == "false" ]] && ! command -v virtctl &> /dev/null; then
        missing_tools+=("virtctl")
    fi
    
    if [[ "$DRY_RUN" == "false" ]] && ! command -v oc &> /dev/null; then
        missing_tools+=("oc")
    fi
    
    # Check if virtctl supports scp command (for result collection)
    if [[ "$DRY_RUN" == "false" ]] && command -v virtctl &> /dev/null; then
        if ! virtctl help | grep -q "scp"; then
            echo "Warning: virtctl does not support 'scp' command"
            echo "Results will be archived on VMs but not automatically copied to localhost"
            echo "You may need to upgrade virtctl or manually copy results"
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
            esac
        done
        echo ""
        echo "Install the missing tools and try again."
        exit 1
    fi
}

# Function to read YAML configuration
read_config() {
    local config_file="$1"
    
    if [[ ! -f "$config_file" ]]; then
        echo "Error: Configuration file '$config_file' not found"
        exit 1
    fi
    
    # Read configuration values from YAML file
    VM_HOSTS=$(yq eval '.vm.hosts' "$config_file")
    NAMESPACE=$(yq eval '.vm.namespace' "$config_file")
    TEST_DEVICE=$(yq eval '.storage.device' "$config_file")
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
    if [[ -z "$TEST_DEVICE" || "$TEST_DEVICE" == "null" ]]; then
        log_error "storage.device must be specified in config"
        exit 1
    fi
    
    # Validate VMs exist (skip in dry-run mode)
    if [[ "$DRY_RUN" == "false" ]]; then
        for host in $VM_HOSTS; do
            if ! oc get vm "$host" -n "$NAMESPACE" &> /dev/null; then
                log_error "Virtual machine '$host' not found in namespace '$NAMESPACE'"
                exit 1
            fi
        done
    else
        log_info "Skipping VM validation in dry-run mode"
    fi
}

# Display configuration
display_config() {
    log_info "Configuration loaded from: $CONFIG_FILE"
    log_info "VMs: $VM_HOSTS"
    log_info "Namespace: $NAMESPACE"
    log_info "Test device: $TEST_DEVICE"
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
    
    if ! virtctl -n "$NAMESPACE" ssh -t "-o StrictHostKeyChecking=no" \
         "root@vmi/$host" -c "$command"; then
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
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "DRY-RUN: Would execute in background on $host: $command"
        return 0
    fi
    
    virtctl -n "$NAMESPACE" ssh -t "-o StrictHostKeyChecking=no" \
            "root@vmi/$host" -c "$command" &
}

# Install FIO and dependencies on VMs
install_dependencies() {
    log_info "Installing FIO and dependencies on VMs..."
    
    for host in $VM_HOSTS; do
        execute_ssh_background "$host" \
            "dnf update -y && dnf install -y fio xfsprogs util-linux" \
            "Installing FIO and filesystem tools"
    done
    wait
}

# Prepare storage on VMs
prepare_storage() {
    log_info "Preparing storage on VMs..."
    
    for host in $VM_HOSTS; do
        # Create test directories
        execute_ssh "$host" \
            "mkdir -p $OUTPUT_DIR $MOUNT_POINT" \
            "Creating test directories"
        
        # Safety check: verify device exists and get confirmation
        execute_ssh "$host" \
            "if [[ ! -b /dev/$TEST_DEVICE ]]; then
                 echo 'ERROR: Block device /dev/$TEST_DEVICE not found'
                 exit 1
             fi
             echo 'Found block device /dev/$TEST_DEVICE'
             lsblk /dev/$TEST_DEVICE" \
            "Validating test device"
        
        # Unmount if already mounted
        execute_ssh "$host" \
            "if mountpoint -q $MOUNT_POINT; then
                 echo 'Unmounting $MOUNT_POINT'
                 umount $MOUNT_POINT || true
             fi" \
            "Unmounting existing mount"
        
        # Format device (WARNING: destructive operation)
        execute_ssh "$host" \
            "echo 'WARNING: Formatting /dev/$TEST_DEVICE with $FILESYSTEM'
             mkfs.$FILESYSTEM -f /dev/$TEST_DEVICE" \
            "Formatting test device"
        
        # Mount device
        execute_ssh "$host" \
            "mount /dev/$TEST_DEVICE $MOUNT_POINT" \
            "Mounting test device"
    done
}

# Write test dataset
write_test_data() {
    log_info "Writing initial test dataset..."
    
    for host in $VM_HOSTS; do
        execute_ssh_background "$host" \
            "cd $OUTPUT_DIR && fio \
                --name=write_dataset \
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
    done
    wait
}

# Run FIO performance tests
run_fio_tests() {
    log_info "Running FIO performance tests..."
    
    local test_counter=1
    
    # Convert space-separated strings to arrays
    local bs_array=($BLOCK_SIZES)
    local pattern_array=($IO_PATTERNS)
    
    for bs in "${bs_array[@]}"; do
        for pattern in "${pattern_array[@]}"; do
            log_info "Running test $test_counter: $pattern with block size $bs"
            
            for host in $VM_HOSTS; do
                local test_name="fio-test-${pattern}-bs-${bs}"
                execute_ssh_background "$host" \
                    "cd $OUTPUT_DIR && fio \
                        --name=$test_name \
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
            done
            wait
            
            ((test_counter++))
        done
    done
}

# Collect test results
collect_results() {
    local results_dir="${1:-./fio-results-$(date +%Y%m%d-%H%M%S)}"
    
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
            if virtctl -n $NAMESPACE scp "root@vmi/$host:$OUTPUT_DIR/fio-results.tar.gz" "$host_dir/fio-results.tar.gz" 2>/dev/null; then
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
                # Fallback: use virtctl ssh with cat to copy file
                if virtctl -n $NAMESPACE ssh -t "-o StrictHostKeyChecking=no" \
                   "root@vmi/$host" -c "cat $OUTPUT_DIR/fio-results.tar.gz" > "$host_dir/fio-results.tar.gz" 2>/dev/null; then
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
                    log_info "Manual copy command: virtctl -n $NAMESPACE ssh root@vmi/$host -c 'cat $OUTPUT_DIR/fio-results.tar.gz' > $host_dir/fio-results.tar.gz"
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

# Cleanup test environment
cleanup_storage() {
    log_info "Cleaning up storage on VMs..."
    
    for host in $VM_HOSTS; do
        execute_ssh "$host" \
            "if mountpoint -q $MOUNT_POINT; then
                 echo 'Unmounting $MOUNT_POINT'
                 umount $MOUNT_POINT
             fi
             rm -rf $OUTPUT_DIR/*.json" \
            "Cleaning up test environment"
    done
}

# Main function
main() {
    log_info "Starting FIO remote testing script"
    
    check_dependencies
    read_config "$CONFIG_FILE"
    display_config
    validate_inputs
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "DRY RUN MODE: Configuration validated successfully"
        log_info "Would execute the following steps:"
        log_info "  1. Install FIO and dependencies on VMs"
        log_info "  2. Prepare storage (format and mount devices)"
        log_info "  3. Write initial test dataset"
        log_info "  4. Run FIO performance tests with different patterns and block sizes"
        log_info "  5. Collect test results"
        log_info "  6. Clean up test environment"
        log_info ""
        log_info "WARNING: Step 2 will format the specified storage device!"
        log_info "Use without --dry-run to execute the actual tests"
        return 0
    fi
    
    # Confirmation for destructive operations
    echo ""
    log_warn "WARNING: This script will format device '$TEST_DEVICE' on all VMs!"
    log_warn "VMs: $VM_HOSTS"
    log_warn "Device: /dev/$TEST_DEVICE"
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
    local final_results_dir="./fio-results-$results_timestamp"
    
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
    $0 [-h] [-c config_file] [-v] [--dry-run]

OPTIONS:
    -h                  Show this help message
    -c <config_file>    Path to YAML configuration file (default: fio-config.yaml)
    -v                  Verbose output
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

# Parse command line arguments
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
        --dry-run)
            DRY_RUN=true
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