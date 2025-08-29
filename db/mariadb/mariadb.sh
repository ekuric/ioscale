#!/bin/bash

# MariaDB HammerDB TPCC Testing Script (YAML Configuration Version)
# This script sets up and runs MariaDB performance tests using HammerDB TPCC benchmarks
# Configuration is read from a YAML file instead of command line arguments

set -euo pipefail  # Exit on error, undefined vars, pipe failures

# Default configuration file
CONFIG_FILE="config.yaml"
DRY_RUN=false

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

# Smart host selection function
get_db_hosts() {
    local config_file="$1"
    local hosts=""
    
    # Method 1: Host pattern expansion (e.g., db{1..200})
    local host_pattern=$(yq eval '.database.host_pattern' "$config_file")
    if [[ "$host_pattern" != "null" && -n "$host_pattern" ]]; then
        log_info "Using host pattern: $host_pattern"
        
        # Use bash expansion for patterns like db{1..200}
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
    local host_labels=$(yq eval '.database.host_labels' "$config_file")
    if [[ "$host_labels" != "null" && -n "$host_labels" ]]; then
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
            echo "example-db1 example-db2"  # Placeholder for dry-run
            return 0
        fi
    fi
    
    # Method 3: External host file
    local host_file=$(yq eval '.database.host_file' "$config_file")
    if [[ "$host_file" != "null" && -n "$host_file" ]]; then
        log_info "Using host file: $host_file"
        
        if [[ -f "$host_file" ]]; then
            # Read hosts from file, skip comments and empty lines
            hosts=$(grep -v '^#' "$host_file" | grep -v '^[[:space:]]*$' | tr '\n' ' ' | xargs)
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
    hosts=$(yq eval '.database.hosts' "$config_file")
    if [[ "$hosts" != "null" && -n "$hosts" ]]; then
        log_info "Using simple host list: $hosts"
        echo "$hosts"
        return 0
    fi
    
    # No hosts found
    log_error "No hosts specified in configuration. Use one of: hosts, host_pattern, host_labels, or host_file"
    exit 1
}

# Function to read YAML configuration
read_config() {
    local config_file="$1"
    
    if [[ ! -f "$config_file" ]]; then
        echo "Error: Configuration file '$config_file' not found"
        exit 1
    fi
    
    # Read configuration values from YAML file
    MOUNT_POINT=$(yq eval '.storage.mount_point' "$config_file")
    DISK_LIST=$(yq eval '.storage.disk_list' "$config_file")
    WAREHOUSE_COUNT=$(yq eval '.database.warehouse_count' "$config_file")
    USER_COUNT=$(yq eval '.test.user_count' "$config_file")
    NAMESPACE=$(yq eval '.database.namespace' "$config_file")
    
    # Smart host selection with multiple methods
    DB_HOSTS=$(get_db_hosts "$config_file")
    HAMMERDB_REPO=$(yq eval '.hammerdb.repo' "$config_file")
    HAMMERDB_PATH=$(yq eval '.hammerdb.path' "$config_file")
    HAMMERDB_DIR=$(yq eval '.hammerdb.install_dir' "$config_file")
    TEST_DURATION=$(yq eval '.database.test_duration' "$config_file")
    LOG_LEVEL=$(yq eval '.test.log_level' "$config_file")
    RUN_NAME=$(yq eval '.test.run_name' "$config_file")
    STORAGE_TYPE=$(yq eval '.test.storage_type' "$config_file")
    
    # Handle null values (convert to "none" or appropriate defaults)
    if [[ "$MOUNT_POINT" == "null" ]]; then
        MOUNT_POINT="none"
    fi
    if [[ "$DISK_LIST" == "null" ]]; then
        DISK_LIST="none"
    fi
    if [[ "$NAMESPACE" == "null" ]]; then
        NAMESPACE="default"
    fi
    if [[ "$LOG_LEVEL" == "null" ]]; then
        LOG_LEVEL="INFO"
    fi
    if [[ "$HAMMERDB_DIR" == "null" ]]; then
        HAMMERDB_DIR="/usr/local/HammerDB"
    fi
    if [[ "$RUN_NAME" == "null" ]]; then
        RUN_NAME="HDB_MDB"
    fi
    if [[ "$STORAGE_TYPE" == "null" ]]; then
        STORAGE_TYPE="null"
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
MariaDB HammerDB TPCC Testing Script (YAML Configuration Version)

DESCRIPTION:
    This script automates MariaDB performance testing using HammerDB TPCC benchmarks.
    Configuration is read from a YAML file instead of command line arguments.

USAGE:
    $0 [-h] [-c config_file] [-v] [--dry-run]

OPTIONS:
    -h                  Show this help message
    -c <config_file>    Path to YAML configuration file (default: config.yaml)
    -v                  Verbose output
    --dry-run           Validate configuration and show what would be done without executing

EXAMPLES:
    $0                          # Use default config.yaml
    $0 -c test-config.yaml      # Use custom configuration file
    $0 -c config.yaml -v        # Use default config with verbose output

YAML CONFIGURATION:
    See config.yaml for configuration file format and examples.

NOTES:
    - Requires 'yq' tool for YAML parsing
    - Script requires virtctl and oc for VM access
    - All operations are performed as root on target VMs
EOF
}

# Input validation
validate_inputs() {
    if [[ "$MOUNT_POINT" == "none" && "$DISK_LIST" == "none" ]]; then
        log_error "Either storage.disk_list or storage.mount_point must be specified in config"
        exit 1
    fi
    
    # Validate hosts are reachable (skip in dry-run mode)
    if [[ "$DRY_RUN" == "false" ]]; then
        for host in $DB_HOSTS; do
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
    log_info "Hosts: $DB_HOSTS"
    log_info "Namespace: $NAMESPACE"
    log_info "Warehouse count: $WAREHOUSE_COUNT"
    log_info "User counts: $USER_COUNT"
    log_info "Test duration: $TEST_DURATION minutes"
    if [[ "$DISK_LIST" != "none" ]]; then
        log_info "Disk device: $DISK_LIST"
    fi
    if [[ "$MOUNT_POINT" != "none" ]]; then
        log_info "Mount point: $MOUNT_POINT"
    fi
    log_info "HammerDB repo: $HAMMERDB_REPO"
    log_info "HammerDB path: $HAMMERDB_PATH"
    log_info "HammerDB install dir: $HAMMERDB_DIR"
    log_info "Run name: $RUN_NAME"
    log_info "Storage type: $STORAGE_TYPE"
    log_info "Log level: $LOG_LEVEL"
}

# Execute SSH command with error handling
execute_ssh() {
    local host="$1"
    local command="$2"
    local description="${3:-command}"
    
    log_info "Executing on $host: $description"
    
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
    
    # Run in subshell to prevent single failure from killing main script
    (
        if ! virtctl -n "$NAMESPACE" ssh -t "-o StrictHostKeyChecking=no" \
                "root@vmi/$host" -c "$command" 2>&1; then
            log_error "Background SSH command failed on $host: $description"
            exit 1
        fi
    ) &
}

# Wait for background jobs with error tracking
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

# Safely manage MariaDB service (with service existence check)
manage_mariadb_service() {
    local host="$1"
    local action="$2"
    local description="${3:-MariaDB service management}"
    
    case "$action" in
        "restart")
            execute_ssh "$host" \
                "if systemctl list-unit-files | grep -q '^mariadb.*\.service'; then
                     if systemctl is-active --quiet mariadb; then
                         echo 'MariaDB is running, restarting...'
                         systemctl restart mariadb
                     else
                         echo 'MariaDB is installed but not running, starting...'
                         systemctl start mariadb
                     fi
                 else
                     echo 'WARNING: MariaDB service not found, skipping restart'
                     exit 0
                 fi" \
                "$description"
            ;;
        "stop")
            execute_ssh "$host" \
                "if systemctl list-unit-files | grep -q '^mariadb.*\.service'; then
                     if systemctl is-active --quiet mariadb; then
                         echo 'MariaDB is running, stopping...'
                         systemctl stop mariadb
                     else
                         echo 'MariaDB is not running'
                     fi
                 else
                     echo 'WARNING: MariaDB service not found, nothing to stop'
                 fi" \
                "$description"
            ;;
        *)
            log_error "Invalid action '$action' for MariaDB service management"
            return 1
            ;;
    esac
}

# Install dependencies on VMs
install_dependencies() {
    log_info "Installing dependencies on VMs..."
    
    local bg_pids=()
    for host in $DB_HOSTS; do
        execute_ssh_background "$host" \
            "dnf -y install git curl vim wget" \
            "Installing dependencies"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    
    wait_for_background_jobs "dependency installation" "${bg_pids[@]}"
}

# Deploy HammerDB scripts to VMs
deploy_scripts() {
    log_info "Deploying HammerDB scripts to VMs..."
    
    # Step 1: Prepare directories in parallel
    local bg_pids=()
    for host in $DB_HOSTS; do
        execute_ssh_background "$host" \
            "rm -rf '$HAMMERDB_PATH' && mkdir -p '$HAMMERDB_PATH'" \
            "Preparing scripts directory"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    wait_for_background_jobs "directory preparation" "${bg_pids[@]}"
    
    # Step 2: Clone repositories in parallel
    bg_pids=()
    for host in $DB_HOSTS; do
        execute_ssh_background "$host" \
            "cd '$HAMMERDB_PATH' && git clone '$HAMMERDB_REPO' ." \
            "Cloning HammerDB scripts"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    wait_for_background_jobs "repository cloning" "${bg_pids[@]}"
    
    # Step 3: Set permissions in parallel
    bg_pids=()
    for host in $DB_HOSTS; do
        execute_ssh_background "$host" \
            "chmod +x '$HAMMERDB_PATH/templates/mariadb/Hammerdb-mariadb-install-script'" \
            "Setting execute permissions"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    wait_for_background_jobs "permission setting" "${bg_pids[@]}"
}

# Install MariaDB on VMs
install_mariadb() {
    log_info "Installing MariaDB on VMs..."
    
    local bg_pids=()
    for host in $DB_HOSTS; do
        if [[ "$MOUNT_POINT" != "none" ]]; then
            execute_ssh_background "$host" \
                "cd '$HAMMERDB_PATH/templates/mariadb'; ./Hammerdb-mariadb-install-script -m '$MOUNT_POINT'" \
                "Installing MariaDB with mount point"
        else
            execute_ssh_background "$host" \
                "cd '$HAMMERDB_PATH/templates/mariadb'; ./Hammerdb-mariadb-install-script -d '$DISK_LIST'" \
                "Installing MariaDB with disk device"
        fi
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    
    wait_for_background_jobs "MariaDB installation" "${bg_pids[@]}"
}

# Build TPCC database
build_database() {
    log_info "Building TPCC database with parallel execution..."
    
    # Step 1: Restart MariaDB services in parallel
    log_info "Step 1/5: Restarting MariaDB services on all hosts..."
    local bg_pids=()
    for host in $DB_HOSTS; do
        # Use background execution for service management
        execute_ssh_background "$host" \
            "if systemctl list-unit-files | grep -q '^mariadb.*\.service'; then
                 if systemctl is-active --quiet mariadb; then
                     echo 'MariaDB is running, restarting...'
                     systemctl restart mariadb
                 else
                     echo 'MariaDB is installed but not running, starting...'
                     systemctl start mariadb
                 fi
             else
                 echo 'WARNING: MariaDB service not found, skipping restart'
             fi" \
            "Restarting MariaDB service"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    wait_for_background_jobs "MariaDB service restart" "${bg_pids[@]}"
    
    # Step 2: Wait for services to be ready
    log_info "Step 2/5: Waiting for MariaDB services to be ready..."
    sleep 15
    
    # Step 3: Clean existing databases in parallel
    log_info "Step 3/5: Cleaning existing databases on all hosts..."
    bg_pids=()
    for host in $DB_HOSTS; do
        execute_ssh_background "$host" \
            "echo 'DROP DATABASE IF EXISTS tpcc;' | mysql -u root -p\$MARIADB_ROOT_PASSWORD" \
            "Cleaning existing database"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    wait_for_background_jobs "database cleanup" "${bg_pids[@]}"
    
    # Step 4: Copy and configure build scripts in parallel
    log_info "Step 4/5: Preparing build scripts on all hosts..."
    bg_pids=()
    local counter=1
    for host in $DB_HOSTS; do
        execute_ssh_background "$host" \
            "cd '$HAMMERDB_DIR' && 
             cp build_mariadb.tcl build${counter}_mariadb.tcl &&
             sed -i 's/^diset tpcc mysql_count_ware.*/diset tpcc mysql_count_ware $WAREHOUSE_COUNT/' build${counter}_mariadb.tcl" \
            "Preparing build script (build${counter}_mariadb.tcl)"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
        ((counter++))
    done
    wait_for_background_jobs "build script preparation" "${bg_pids[@]}"
    
    # Step 5: Build databases in parallel
    log_info "Step 5/5: Building TPCC databases on all hosts (this may take a while)..."
    bg_pids=()
    counter=1
    for host in $DB_HOSTS; do
        execute_ssh_background "$host" \
            "cd '$HAMMERDB_DIR' && nohup ./hammerdbcli auto build${counter}_mariadb.tcl > build_mariadb${counter}.out 2>&1" \
            "Building database (output: build_mariadb${counter}.out)"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
        ((counter++))
    done
    wait_for_background_jobs "database building" "${bg_pids[@]}"
    
    log_info "Database building completed on all hosts!"
}

# Run performance tests
run_tests() {
    log_info "Running performance tests..."
    
    local num_hosts
    num_hosts=$(echo "$DB_HOSTS" | wc -w)
    local run_date
    run_date=$(date +%Y.%m.%d)
    
    for user_count in $USER_COUNT; do
        log_info "Starting test run with $user_count users on all hosts..."
        
        # Step 1: Setup test scripts on all hosts in parallel
        log_info "Preparing test scripts for $user_count users..."
        local bg_pids=()
        local counter=1
        for host in $DB_HOSTS; do
            execute_ssh_background "$host" \
                "cd '$HAMMERDB_DIR' && 
                 cp '$HAMMERDB_PATH/templates/mariadb/mariadbsetup/runtest_mariadb.tcl' runtest${counter}_mariadb.tcl 2>/dev/null || cp runtest_mariadb.tcl runtest${counter}_mariadb.tcl &&
                 sed -i 's/^diset tpcc mysql_count_ware.*/diset tpcc mysql_count_ware $WAREHOUSE_COUNT/g' runtest${counter}_mariadb.tcl &&
                 sed -i 's/^vuset.*/vuset vu $user_count/g' runtest${counter}_mariadb.tcl &&
                 sed -i 's/^diset tpcc mysql_duration.*/diset tpcc mysql_duration $TEST_DURATION/g' runtest${counter}_mariadb.tcl" \
                "Preparing test script (runtest${counter}_mariadb.tcl) for $user_count users"
            if [[ "$DRY_RUN" == "false" ]]; then
                bg_pids+=($!)
            fi
            ((counter++))
        done
        wait_for_background_jobs "test script preparation for $user_count users" "${bg_pids[@]}"
        
        # Step 2: Run performance tests on all hosts in parallel
        log_info "Executing performance tests with $user_count users..."
        bg_pids=()
        counter=1
        for host in $DB_HOSTS; do
            local output_file="test_mariadb_${run_date}_${num_hosts}pod_pod${counter}_${user_count}.out"
            execute_ssh_background "$host" \
                "cd '$HAMMERDB_DIR' && nohup ./hammerdbcli auto runtest${counter}_mariadb.tcl > '$output_file' 2>&1" \
                "Running performance test (output: $output_file)"
            if [[ "$DRY_RUN" == "false" ]]; then
                bg_pids+=($!)
            fi
            ((counter++))
        done
        wait_for_background_jobs "performance test execution with $user_count users" "${bg_pids[@]}"
        
        # Step 3: Collect results from all hosts
        log_info "Collecting test results for $user_count users:"
        counter=1
        for host in $DB_HOSTS; do
            local output_file="test_mariadb_${run_date}_${num_hosts}pod_pod${counter}_${user_count}.out"
            if [[ "$DRY_RUN" == "false" ]]; then
                local result
                result=$(execute_ssh "$host" \
                    "cd '$HAMMERDB_DIR'; grep TPM '$output_file' | awk '{print \$7}' || echo 'No results found'" \
                    "Collecting TPM results") || result="Error collecting results"
                log_info "Host $host: $result TPM"
            else
                log_info "DRY-RUN: Would collect results from $host"
            fi
            ((counter++))
        done
        
        log_info "Completed test run with $user_count users on all hosts"
    done
}

# Collect test results from all VMs
collect_results() {
    local results_dir="${1:-./mariadb-results-$(date +%Y%m%d-%H%M%S)}"
    
    log_info "Collecting MariaDB test results..."
    mkdir -p "$results_dir"
    
    for host in $DB_HOSTS; do
        local host_dir="$results_dir/$host"
        mkdir -p "$host_dir"
        
        log_info "Collecting results from $host..."
        
        # Create results archive on VM
        if [[ "$DRY_RUN" == "true" ]]; then
            log_info "DRY-RUN: Would archive results on $host"
        else
            execute_ssh "$host" \
                "cd '$HAMMERDB_DIR' && tar czf mariadb-results.tar.gz build_mariadb*.out test_mariadb_*.out 2>/dev/null || tar czf mariadb-results.tar.gz build_mariadb*.out 2>/dev/null || echo 'No result files found'" \
                "Creating results archive"
        fi
        
        # Copy results from VM to localhost using virtctl scp
        if [[ "$DRY_RUN" == "true" ]]; then
            log_info "DRY-RUN: Would copy results from $host to $host_dir/"
        else
            log_info "Copying results from $host to localhost..."
            if virtctl -n "$NAMESPACE" scp "root@vmi/$host:$HAMMERDB_DIR/mariadb-results.tar.gz" "$host_dir/mariadb-results.tar.gz" 2>/dev/null; then
                log_info "Successfully copied results from $host using virtctl scp"
                
                # Extract results locally for easier access
                if command -v tar &> /dev/null; then
                    cd "$host_dir"
                    if tar -xzf mariadb-results.tar.gz 2>/dev/null; then
                        log_info "Extracted results for $host"
                        # Remove the tar file to save space, keep extracted files
                        rm -f mariadb-results.tar.gz
                    else
                        log_warn "Failed to extract results for $host, keeping tar file"
                    fi
                    cd - > /dev/null
                fi
            else
                log_warn "virtctl scp failed, trying alternative method..."
                # Fallback: use virtctl ssh with cat to copy file
                if virtctl -n "$NAMESPACE" ssh -t "-o StrictHostKeyChecking=no" \
                   "root@vmi/$host" -c "cat '$HAMMERDB_DIR/mariadb-results.tar.gz'" > "$host_dir/mariadb-results.tar.gz" 2>/dev/null; then
                    log_info "Successfully copied results from $host using ssh+cat fallback"
                    
                    # Extract results locally
                    if command -v tar &> /dev/null; then
                        cd "$host_dir"
                        if tar -xzf mariadb-results.tar.gz 2>/dev/null; then
                            log_info "Extracted results for $host"
                            rm -f mariadb-results.tar.gz
                        else
                            log_warn "Failed to extract results for $host, keeping tar file"
                        fi
                        cd - > /dev/null
                    fi
                else
                    log_error "Failed to copy results from $host using both methods"
                    log_info "Results are still available on $host at $HAMMERDB_DIR/mariadb-results.tar.gz"
                    log_info "Manual copy command: virtctl -n $NAMESPACE ssh root@vmi/$host -c 'cat $HAMMERDB_DIR/mariadb-results.tar.gz' > $host_dir/mariadb-results.tar.gz"
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
            find "$results_dir" -type f \( -name "*.out" -o -name "*.log" \) | head -10
            local total_files=$(find "$results_dir" -type f \( -name "*.out" -o -name "*.log" \) | wc -l)
            log_info "Total result files: $total_files"
        fi
        
        # Display summary of test results
        log_info "MariaDB Test Results Summary:"
        for host_dir in "$results_dir"/*/; do
            if [[ -d "$host_dir" ]]; then
                local hostname=$(basename "$host_dir")
                local build_files=$(find "$host_dir" -name "build_mariadb*.out" | wc -l)
                local test_files=$(find "$host_dir" -name "test_mariadb_*.out" | wc -l)
                log_info "  $hostname: $build_files build files, $test_files test files"
                
                # Extract performance metrics if available
                for test_file in "$host_dir"/test_mariadb_*.out; do
                    if [[ -f "$test_file" ]]; then
                        local tpm=$(grep -o "TPM.*[0-9]\+" "$test_file" 2>/dev/null | tail -1 || echo "TPM not found")
                        log_info "    $(basename "$test_file"): $tpm"
                    fi
                done
            fi
        done
    fi
}

# Stop MariaDB instances
stop_mariadb() {
    log_info "Stopping MariaDB instances on all hosts..."
    
    # Step 1: Stop MariaDB services in parallel
    local bg_pids=()
    for host in $DB_HOSTS; do
        execute_ssh_background "$host" \
            "if systemctl list-unit-files | grep -q '^mariadb.*\.service'; then
                 if systemctl is-active --quiet mariadb; then
                     echo 'MariaDB is running, stopping...'
                     systemctl stop mariadb
                 else
                     echo 'MariaDB is not running'
                 fi
             else
                 echo 'WARNING: MariaDB service not found, nothing to stop'
             fi" \
            "Stopping MariaDB service"
        if [[ "$DRY_RUN" == "false" ]]; then
            bg_pids+=($!)
        fi
    done
    wait_for_background_jobs "MariaDB service stop" "${bg_pids[@]}"
    
    # Step 2: Cleanup storage (unmount if using mount point or disk device) in parallel
    if [[ "$MOUNT_POINT" != "none" && "$MOUNT_POINT" != "null" ]]; then
        log_info "Cleaning up storage mount points on all hosts..."
        
        bg_pids=()
        for host in $DB_HOSTS; do
            execute_ssh_background "$host" \
                "# Check if mount point is mounted and unmount it
                 if mountpoint -q '$MOUNT_POINT' 2>/dev/null; then
                     echo 'Unmounting $MOUNT_POINT'
                     umount '$MOUNT_POINT' && echo 'Successfully unmounted $MOUNT_POINT'
                 else
                     echo 'Mount point $MOUNT_POINT is not mounted or does not exist'
                 fi
                 # Clean up any temporary files in HammerDB directory
                 cd '$HAMMERDB_DIR' && rm -f mariadb-results.tar.gz 2>/dev/null || true" \
                "Cleaning up storage and temporary files"
            if [[ "$DRY_RUN" == "false" ]]; then
                bg_pids+=($!)
            fi
        done
        wait_for_background_jobs "storage cleanup" "${bg_pids[@]}"
    elif [[ "$DISK_LIST" != "none" && "$DISK_LIST" != "null" ]]; then
        log_info "Cleaning up disk device mount points on all hosts..."
        
        bg_pids=()
        for host in $DB_HOSTS; do
            execute_ssh_background "$host" \
                "# MariaDB installation script mounts disk devices to /perf1
                 # Check if /perf1 is mounted and unmount it
                 if mountpoint -q '/perf1' 2>/dev/null; then
                     echo 'Unmounting /perf1 (disk device mount point)'
                     umount '/perf1' && echo 'Successfully unmounted /perf1'
                 else
                     echo 'Mount point /perf1 is not mounted or does not exist'
                 fi
                 # Clean up any temporary files in HammerDB directory
                 cd '$HAMMERDB_DIR' && rm -f mariadb-results.tar.gz 2>/dev/null || true" \
                "Cleaning up disk device mount point and temporary files"
            if [[ "$DRY_RUN" == "false" ]]; then
                bg_pids+=($!)
            fi
        done
        wait_for_background_jobs "disk device cleanup" "${bg_pids[@]}"
    else
        log_info "No storage configuration detected - only cleaning up temporary files"
        
        # Still clean up temporary files
        bg_pids=()
        for host in $DB_HOSTS; do
            execute_ssh_background "$host" \
                "cd '$HAMMERDB_DIR' && rm -f mariadb-results.tar.gz 2>/dev/null || true" \
                "Cleaning up temporary files"
            if [[ "$DRY_RUN" == "false" ]]; then
                bg_pids+=($!)
            fi
        done
        wait_for_background_jobs "temporary file cleanup" "${bg_pids[@]}"
    fi
}

# Main function
main() {
    log_info "Starting MariaDB HammerDB TPCC testing script"
    
    check_dependencies
    read_config "$CONFIG_FILE"
    display_config
    validate_inputs
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "DRY RUN MODE: Configuration validated successfully"
        log_info "Would execute the following steps:"
        log_info "  1. Install dependencies on VMs"
        log_info "  2. Deploy HammerDB scripts"
        log_info "  3. Install MariaDB"
        log_info "  4. Build TPCC database"
        log_info "  5. Run performance tests"
        log_info "  6. Collect test results from all VMs"
        log_info "  7. Stop MariaDB instances and cleanup storage"
        log_info "Use without --dry-run to execute the actual tests"
        return 0
    fi
    
    install_dependencies
    deploy_scripts
    install_mariadb
    build_database
    run_tests
    
    # Collect results - create directory name once
    local results_timestamp=$(date +%Y%m%d-%H%M%S)
    local final_results_dir="./mariadb-results-$results_timestamp"
    
    collect_results "$final_results_dir"
    stop_mariadb
    
    log_info "MariaDB performance testing completed successfully"
    log_info "Results have been copied to localhost: $final_results_dir"
    log_info "Each VM's results are in separate subdirectories with extracted files"
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