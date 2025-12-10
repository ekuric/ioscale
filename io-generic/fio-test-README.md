# FIO Remote Testing Script (Python) - Complete Documentation

## Overview

The `fio-tests.py` script is a comprehensive Python tool for executing FIO (Flexible I/O Tester) performance tests on remote virtual machines and hosts. It supports both Linux and Windows hosts, OpenShift/Kubernetes CNV environments (using virtctl), and traditional SSH-based access, with advanced features for reliability, monitoring, and VM management.

## Key Features

### 1. Cross-Platform Support

- **Linux Hosts**: Full support for Linux VMs and servers
- **Windows Hosts**: Complete support for Windows VMs with PowerShell-based operations
- **Mixed Environments**: Test Linux and Windows hosts simultaneously in the same run
- **Auto-Detection**: Automatically detects host type (Linux/Windows, VM/server) and uses appropriate commands

### 2. VM Migration During Tests

The script can trigger VM migrations at the midpoint of FIO test execution, allowing you to test performance impact of live migrations.

#### How It Works

- **Pattern-Based**: Migrations are triggered only for specified I/O patterns/workloads
- **Midpoint Timing**: Migrations occur at exactly half the test runtime (e.g., 5 minutes into a 10-minute test)
- **Execution Modes**: 
  - **Parallel** (default): All VMs migrate concurrently for efficiency
  - **Sequential**: VMs migrate one at a time with a configurable interval between migrations
- **VM-Only**: Only virtual machines are migrated; regular hosts are automatically skipped
- **Retry Logic**: Failed migrations are automatically retried once
- **Virtual Machines**: Must be configured to be able to migrate from one OCP node to another. Proper storage support must be implmented and PVC for virtual machines must be created in `ReadWriteMany` mode. 

#### Configuration

```yaml
migrate:
  # List of I/O patterns/workloads during which to trigger VM migrations
  # Migrations will occur at the midpoint of the test runtime
  # Only VMs (not regular hosts) will be migrated
  workloads: "write randwrite"  # Space-separated list of patterns
  
  # Interval in seconds between VM migrations (default: 0 = parallel migration)
  # Setting this to a positive value (e.g., 2) will migrate VMs sequentially with the specified delay
  # This helps avoid overloading CNV when migrating multiple VMs simultaneously
  interval: 0  # Seconds to wait between migrations (0 = parallel, >0 = sequential)
```

#### Migration Modes

**Parallel Migration (interval: 0)**
- All VMs migrate simultaneously
- Fastest option for small numbers of VMs
- May overload CNV infrastructure with many VMs

**Sequential Migration (interval > 0)**
- VMs migrate one at a time with a delay between each
- Recommended for large numbers of VMs (e.g., 50+ VMs)
- Reduces load on CNV infrastructure
- Example: With 100 VMs and 2s interval, total migration time = 200 seconds

### 3. Windows Host Support

Complete support for Windows virtual machines and hosts with Windows-specific optimizations.

#### Key Features

- **Automatic Detection**: Windows hosts are identified via the `windows` section in the config file
- **Separate Configuration**: Windows hosts use their own storage, FIO, and output configurations
- **PowerShell Commands**: All Windows operations use PowerShell instead of bash
- **Windows-Specific FIO**: Uses `fio.exe` with `windowsaio` ioengine
- **Automatic FIO Installation**: Copies FIO from `c:\tools\fio` to the configured root directory
- **Mixed Testing**: Can test Linux and Windows hosts simultaneously in the same run
- **It is expected** that Windows virutal machines is built with additional disk for testing (`d:`) and FIO tools are already installed on `c:\tools\fio`, this script only copy `fio.exe` to test disk `d:`.


#### Windows Configuration

Windows hosts are configured in a separate `windows` section:

```yaml
windows:
  # Windows host list or pattern
  hosts: "win-vm-1 win-vm-2"
  # OR use pattern: host_pattern: "win-vm-{1..10}"
  
  # Windows Storage Configuration
  storage_win:
    devices:
      "win-vm-{1..10}": "1"  # Use Disk ID (e.g., "1" for Disk 1)
    mount_point: "d\\:/fio/data"  # Windows path format
  
  # Windows FIO Configuration
  fio_win:
    run_dir: "d:/fio"  # Directory containing fio.exe
    test_size: "10GB"
    runtime: 600
    block_sizes: "4k 8k 64k 128k"
    io_patterns: "randread randwrite read write"
    numjobs: 8
    iodepth: 16
    direct_io: 1
  
  # Windows Output Configuration
  output_win:
    directory: "d:/fio/results"
    format: "json+"
```

#### Windows-Specific Behavior

**Storage Preparation:**
- Uses PowerShell `Get-Disk` to validate disks
- Uses `provision-data-disk.ps1` script for disk provisioning
- No manual formatting (handled by provision script)
- No mount/unmount (Windows uses drive letters)

**FIO Installation:**
- `fio-tests.py` expect `fio` tools to be already prepared in advance on Windows virtual machine image. It will not download fio, but it expect FIO to be in `c:\tools\fio` and it is copied from `c:\tools\fio` to the configured root directory (e.g., `d:\`)
- Runs automatically after storage preparation (to avoid wiping FIO during disk formatting)
- Verifies installation with directory listings

**FIO Execution:**
- Uses `fio.exe` instead of `fio`
- Uses `windowsaio` ioengine (required for Windows)
- Uses Windows path format: `d\:\fio\data` (backslashes with escaped colon)
- Runs in PowerShell environment
- Supports parallel dataset writes using ThreadPoolExecutor

**SSH/SCP Access:**
- Uses `Administrator@vmi` instead of `root@vmi` for Windows hosts
- Automatically detected and handled by the script


#### Requirements for Windows

- Windows VMs must have FIO pre-installed at `c:\tools\fio`
- PowerShell must be available on Windows hosts
- `provision-data-disk.ps1` script must be available at `c:\tools\setup\provision-data-disk.ps1`
- Windows hosts must be accessible via `virtctl ssh` or `ssh` with Administrator account, this means that `SSH` must be functional on Windows machines. 


### 4. Mixed Host Support

The script supports both VMs and regular hosts, as well as mixed Linux and Windows environments.

#### Auto-Detection Mode (Default)

- Automatically detects if a host is a VM or regular server
- Automatically detects if a host is Linux or Windows
- Uses `virtctl` for VMs, `ssh` for regular hosts
- Uses `root@vmi` for Linux hosts, `Administrator@vmi` for Windows hosts
- Seamlessly handles mixed environments (Linux/Windows, VM/server)

#### Force Modes

```bash
--ssh-only        # Force SSH for all hosts
--virtctl-only    # Force virtctl for all hosts
```

### 5. Retry Mechanisms

Comprehensive retry logic ensures operations succeed even in unstable network conditions.

#### Features

- **Configurable Retries**: Set retry interval and maximum attempts
- **Connectivity Testing**: Optional connectivity checks before executing commands
- **Smart Timeouts**: Automatically adjusts timeouts for FIO commands based on runtime
- **Background Job Support**: Retries work for both foreground and background operations
- **Process Check Handling**: Non-critical process checks use warnings instead of errors

#### Configuration

```yaml
retry:
  # Retry interval in seconds (default: 30)
  interval: 30
  
  # Maximum number of retry attempts (default: 10)
  # For OCP cluster upgrades test scenarios `max_retries` needs to be set to higher values 
  # for Example: max_retries: 1000  
  # setting max_retries to higher value it will ensure script will not gave up if OCP node is not up fast due to 
  # upgrade. This usually is not the case.
  max_retries: 10
  
  # Skip initial connectivity test (default: false)
  skip_connectivity_test: false
```

#### Command-Line Options

```bash
--interval SECONDS        # Retry interval in seconds
--max-retries N           # Maximum retry attempts
--skip-connectivity-test  # Skip connectivity test
```

### 6. Parallel Execution

All operations are executed in parallel for maximum efficiency:

- **Storage Preparation**: Format and mount devices in parallel (Linux and Windows separately)
- **FIO Installation**: Install packages in parallel on all hosts
- **FIO Tests**: All hosts run tests simultaneously
- **Result Collection**: Parallel collection from all hosts
- **VM Migrations**: All VMs migrate concurrently (or sequentially with configured interval)

### 7. Comprehensive Logging

- **Timestamped Logs**: All operations are logged with timestamps
- **Log File**: Creates a log file with description in filename (e.g., `fio-test-my_test-20231205-120000.txt`)
- **Log File Copy**: Automatically copies log file to results directory at end of test
- **Error Tracking**: Failed operations are clearly marked
- **Progress Reporting**: Real-time status updates
- **Verbose Mode**: Detailed output with `-v` flag
- **Debug Mode**: Additional debug information with `--debug` flag

### 8. Test Description

The script supports a `description` field in the configuration file that is used in log file and results directory names for better organization.

#### Configuration

```yaml
description: "my performance test"  # Optional: Short description for this test run
```

#### Usage

- **Results Directory**: `fio-results-{timestamp}-{sanitized_description}-machines_{count}`
- **Log File**: `fio-test-{sanitized_description}-{timestamp}.txt`
- **Sanitization**: Spaces and special characters are converted to underscores, lowercase

Example:
- Description: `"my new special test"`
- Results: `fio-results-20231205-120000-my_new_special_test-machines_10`
- Log: `fio-test-my_new_special_test-20231205-120000.txt`

## Configuration File Structure

Example configuration files are available in the `io-generic/config-file-examples/linux-windows-yaml-examples/` directory:
- `example-linux-only.yaml` - Linux-only configuration
- `example-windows-only.yaml` - Windows-only configuration
- `example-mixed-linux-windows.yaml` - Mixed Linux and Windows configuration
- `example-with-migration.yaml` - Configuration with VM migration
- `example-simple.yaml` - Minimal configuration for quick testing

### Complete Example (Linux Only)

```yaml
# Test Description (Optional)
description: "my performance test"  # Will be included in log and results directory names

# VM/Host Configuration
vm:
  host_pattern: "vm{1..100}"  # Or use hosts, host_file, or host_labels
  namespace: "default"

# Storage Configuration
storage:
  devices:
    "vm{1..100}": "vdb"  # Per-host or pattern-based device mapping
  mount_point: "/root/tests/data"
  filesystem: "xfs"
  persistent: ""  # Set to "true" for persistent mounts via /etc/fstab

# FIO Test Configuration
fio:
  test_size: "1G"
  runtime: 600  # 10 minutes
  block_sizes: "4k 8k 128k"
  io_patterns: "read write randread randwrite"
  numjobs: 1
  iodepth: 16
  direct_io: 1
  # rate_iops: 1000  # Optional rate limiting

# Output Configuration
output:
  directory: "/root/fio-results"
  format: "json+"

# Retry Configuration
retry:
  interval: 30
  max_retries: 10
  skip_connectivity_test: false

# Task Monitoring Configuration
monitoring:
  task_monitor_interval: 60

# VM Migration Configuration
migrate:
  workloads: "write randwrite"  # Migrate during these patterns
  interval: 0  # Seconds between migrations (0 = parallel, >0 = sequential)
```

### Complete Example (Mixed Linux and Windows)

```yaml
# Test Description (Optional)
description: "cross-platform performance test"

# Linux Hosts Configuration
vm:
  host_pattern: "linux-vm-{1..5}"
  namespace: "default"

# Linux Storage Configuration
storage:
  devices:
    "linux-vm-{1..5}": "vdb"
  mount_point: "/root/tests/data"
  filesystem: "xfs"

# Linux FIO Configuration
fio:
  test_size: "1G"
  runtime: 600
  block_sizes: "4k 8k"
  io_patterns: "read write"
  numjobs: 1
  iodepth: 16
  direct_io: 1

# Linux Output Configuration
output:
  directory: "/root/fio-results"
  format: "json+"

# Windows Hosts Configuration
windows:
  host_pattern: "win-vm-{1..5}"
  
  # Windows Storage Configuration
  storage_win:
    devices:
      "win-vm-{1..5}": "1"  # Disk ID (not /dev/ path)
    mount_point: "d\\:/fio/data"  # Windows path format
  
  # Windows FIO Configuration
  fio_win:
    run_dir: "d:/fio"  # Directory containing fio.exe
    test_size: "10GB"
    runtime: 600
    block_sizes: "4k 8k"
    io_patterns: "read write"
    numjobs: 8
    iodepth: 16
    direct_io: 1
  
  # Windows Output Configuration
  output_win:
    directory: "d:/fio/results"
    format: "json+"

# Retry Configuration
retry:
  interval: 30
  max_retries: 10

# Task Monitoring Configuration
monitoring:
  task_monitor_interval: 60

# VM Migration Configuration (applies to both Linux and Windows VMs)
migrate:
  workloads: "write"
  interval: 0
```

### Complete Example (Windows Only)

```yaml
# Test Description (Optional)
description: "windows performance test"

# Omit 'vm', 'storage', 'fio', and 'output' sections for Windows-only testing
# Only configure 'windows' section

windows:
  hosts: "win-vm-1 win-vm-2"
  storage_win:
    devices:
      "win-vm-1": "1"
      "win-vm-2": "1"
    mount_point: "d\\:/fio/data"
  fio_win:
    run_dir: "d:/fio"
    test_size: "10GB"
    runtime: 600
    block_sizes: "4k 8k"
    io_patterns: "read write"
    numjobs: 8
    iodepth: 16
    direct_io: 1
  output_win:
    directory: "d:/fio/results"
    format: "json+"

# Retry Configuration
retry:
  interval: 30
  max_retries: 10

# Task Monitoring Configuration
monitoring:
  task_monitor_interval: 60
```

## Usage Examples

### Basic Usage

```bash
# Use default configuration (fio-config.yaml)
python3 fio-tests.py

# Custom configuration file
python3 fio-tests.py -c my-config.yaml

# Verbose output
python3 fio-tests.py -v
```

### With Migration

```bash
# Run tests with VM migration during write and randwrite patterns (parallel migration)
python3 fio-tests.py -c config-with-migration.yaml

# Sequential migration with 2 second interval (recommended for many VMs)
# Configure in YAML: migrate.interval: 2
python3 fio-tests.py -c config-with-sequential-migration.yaml
```

### With Custom Retry Settings

```bash
# Retry every 60 seconds, up to 20 attempts
python3 fio-tests.py --interval 60 --max-retries 20
```

### Dry Run

```bash
# Validate configuration without executing
python3 fio-tests.py --dry-run
```

### Prepare Machines Only

```bash
# Install FIO dependencies without running tests
python3 fio-tests.py --prepare-machine
```

### Windows-Only Testing

```bash
# Test only Windows hosts (configure windows section in YAML)
python3 fio-tests.py -c windows-only-config.yaml
```

### Mixed Linux/Windows Testing

```bash
# Test both Linux and Windows hosts simultaneously
python3 fio-tests.py -c mixed-linux-windows-config.yaml
```

### Copy Results Only

```bash
# Only copy results from hosts (skip installation, preparation, and testing)
python3 fio-tests.py --copy-results
```

### Debug Mode

```bash
# Show detailed configuration parsing debug information
python3 fio-tests.py --debug
```

## Command-Line Options

```bash
python3 fio-tests.py [OPTIONS]

Options:
  -c, --config FILE              Path to YAML configuration file (default: fio-config.yaml)
  -v, --verbose                  Verbose output
  --dry-run                      Validate configuration and show what would be done without executing
  --ssh-only                     Force SSH for all hosts
  --virtctl-only                 Force virtctl for all hosts
  --yes-i-mean-it                Skip confirmation prompt for device formatting
  --prepare-machine              Only install FIO dependencies on machines, skip all testing
  --interval SECONDS             Override retry interval in seconds (from config file)
  --max-retries N                Override maximum number of retry attempts (from config file)
  --skip-connectivity-test       Skip connectivity test and proceed directly to command execution
  --monitor-interval SECONDS     Override task monitor interval in seconds (from config file)
  --debug                        Show detailed configuration parsing debug information
  --copy-results                 Only copy results from hosts (skip installation, preparation, and testing)
```

## Workflow

1. **Dependency Check**: Verifies required tools (PyYAML, virtctl/oc, ssh)
2. **Configuration Loading**: Reads and validates YAML configuration
3. **Host Selection**: Expands patterns, queries labels, or reads host files (separates Linux and Windows hosts)
4. **Connectivity Testing**: Tests access to all hosts (optional)
5. **Storage Preparation**: 
   - **Linux**: Creates directories, validates devices, unmounts, formats, mounts filesystems
   - **Windows**: Creates directories, validates disks, runs provision-data-disk.ps1 script
6. **FIO Installation**:
   - **Linux**: Installs FIO and dependencies via package manager
   - **Windows**: Copies FIO from `c:\tools\fio` to configured root directory
7. **Test Dataset Creation**: Writes initial test data on all hosts (Linux and Windows) in parallel
8. **FIO Performance Tests**:
   - Runs tests for each block size and I/O pattern combination
   - Tests run simultaneously on Linux and Windows hosts
   - Monitors tasks for reboots (Linux only)
   - Triggers migrations at midpoint (if configured in `migrate: workloads` section in yaml configuration file)
9. **Result Collection**: Collects and extracts results from all hosts (Linux and Windows) in parallel
10. **Log File Copy**: Copies the test log file to the results directory
11. **Cleanup**: Unmounts devices (Linux) and cleans up temporary files

## Advanced Features

### Smart Timeout Calculation

The script automatically detects FIO commands and adjusts timeouts based on runtime:
- For FIO commands with `--runtime`, timeout = runtime + 300 seconds
- For dataset writes without explicit runtime, uses max configured runtime + 300 seconds
- For non-FIO commands, uses default 300 second timeout

### Process Check Handling

Process status checks (e.g., checking if FIO is running) are treated as non-critical:
- Failures are logged as warnings instead of errors
- Failures are expected (process might not be running)
- Script uses fail-safe behavior (assumes process is not running)

### Error Handling

- **Retry Logic**: Automatic retries for transient failures
- **Graceful Degradation**: Continues operation when possible
- **Detailed Logging**: All errors are logged with context
- **Exit Codes**: Proper exit codes for automation
- **Status Reporting**: Summary of successes and failures

## File Naming

### Results Directory

Results are stored in directories with the following naming pattern:
- **With description**: `fio-results-{timestamp}-{sanitized_description}-machines_{count}`
- **Without description**: `fio-results-{timestamp}-machines_{count}`

Example:
- `fio-results-20231205-120000-my_performance_test-machines_10`
- `fio-results-20231205-120000-machines_10`

### Log Files

Log files follow a similar pattern:
- **With description**: `fio-test-{sanitized_description}-{timestamp}.txt`
- **Without description**: `fio-test-{timestamp}.txt`

The log file is automatically copied to the results directory at the end of the test.

## Best Practices

1. **Test Configuration**: Always use `--dry-run` first to validate configuration
2. **Migration Testing**: Start with short test runtimes when testing migrations and monitor `oc get pods` in virtual machine namespace to check are virtual machines migrated at the midpoint of test. 
3. **Retry Settings**: Adjust retry intervals based on network stability
4. **Monitoring Interval**: Increase for very long tests (>1 hour)
5. **Host Patterns**: Use patterns for large numbers of hosts (e.g., `vm{1..100}`)
6. **Device Safety**: Double-check device mappings before running (destructive operation)
7. **Windows Preparation**: Ensure FIO is pre-installed at `c:\tools\fio` on Windows hosts and Windows virtual machines have proper `SSH` setup ( check connectivity with `virtctl ssh -n vm_namespace Administrator@vmi/vm_name)` and ensure it is working ) 
8. **Windows Paths**: Use Windows path format with escaped backslashes in YAML (e.g., `d\\:/fio/data`)
9. **Mixed Testing**: Test Linux and Windows separately first, then combine for mixed runs
10. **Description Field**: Use the `description` field to organize test runs and results
11. **Sequential Migration**: Use sequential migration mode (interval > 0) for large numbers of VMs

## Troubleshooting

### Migration Not Triggering

- Verify `migrate.workloads` includes the test pattern
- Check that namespace is set correctly
- Ensure virtctl is available and working
- Verify VMs are detected (not regular hosts)

### Windows-Specific Issues

**FIO Not Found:**
- `The system cannot find the drive specified.` ensure `D:` disk is visible and accessible on Windows test machine. 
- Verify FIO exists at `c:\tools\fio` on Windows hosts
- Check that FIO was copied to the configured root directory (e.g., `d:\fio`)
- Review installation logs for copy errors

**PowerShell Errors:**
- Ensure PowerShell is available on Windows hosts
- Check that commands are properly escaped for PowerShell
- Verify `provision-data-disk.ps1` exists at `c:\tools\setup\`

**Path Format Issues:**
- Use Windows path format: `d\\:/fio/data` in YAML (escaped backslashes)
- FIO requires backslash format: `d\:\fio\data` in FIO commands (handled automatically)

**SCP Authentication:**
- Ensure `Administrator@vmi` is used for Windows hosts (automatic)
- Verify SSH keys are configured for Administrator account

**Dataset Write Issues:**
- Check that `--overwrite=1` is used (handled automatically)
- Verify `--numjobs` is set correctly for parallel writes
- Ensure dataset write runs for full runtime (not just creates empty file)

### Connectivity Issues

- Use `--skip-connectivity-test` if test is too strict
- Increase retry interval and max retries
- Check network stability
- Verify SSH/virtctl access manually
- For Windows: Ensure Administrator account has SSH access

### Process Check Warnings

Process check failures are expected and non-critical:
- They indicate the process might not be running (which is normal)
- The script uses fail-safe behavior (assumes process is not running)
- These warnings can be safely ignored in most cases

## Requirements

### Python Dependencies

- Python 3.6 or higher
- PyYAML 5.4.1 or higher

Install dependencies:
```bash
pip install -r requirements.txt
```

### System Tools

- `virtctl` (for VM access in OpenShift/Kubernetes environments)
- `oc` (OpenShift CLI, for VM detection)
- `ssh` (for regular host access)
- `scp` (for file transfers)

### Windows Requirements

- FIO pre-installed at `c:\tools\fio`
- PowerShell available on Windows hosts
- `provision-data-disk.ps1` script at `c:\tools\setup\provision-data-disk.ps1`
- SSH access with Administrator account

## Example Configuration Files

Ready-to-use example configuration files are available in the `yaml-files/` directory:

- **`example-linux-only.yaml`**: Complete Linux-only configuration
- **`example-windows-only.yaml`**: Complete Windows-only configuration
- **`example-mixed-linux-windows.yaml`**: Mixed Linux and Windows hosts
- **`example-with-migration.yaml`**: Configuration with VM migration enabled
- **`example-simple.yaml`**: Minimal configuration for quick testing

To use an example file:
```bash
python3 fio-tests.py -c yaml-files/example-linux-only.yaml
```

## See Also

- `fio-config.yaml`: Main configuration file template
- `yaml-files/`: Directory containing example configuration files
- `merge_fio_migration_results.py`: Tool to merge and analyze split migration results
- `extract_db_results.py`: Tool to extract and visualize database benchmark results
