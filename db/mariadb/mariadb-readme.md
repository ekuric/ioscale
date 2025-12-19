# MariaDB HammerDB TPCC Testing Script

## Overview

`mariadb.py` is a comprehensive Python script for running MariaDB performance tests using HammerDB TPCC benchmarks on Kubernetes virtual machines or bare metal/KVM hosts. The script automates the entire testing workflow including package installation, database setup, test execution, VM migration testing, and results collection.

The script supports two connection modes:
- **virtctl mode** (default): For OpenShift/Kubernetes VMs accessed via `virtctl`
- **SSH-only mode**: For bare metal servers or KVM hosts accessed directly via SSH

## Features

- **YAML-based Configuration**: All settings configured via YAML file
- **Dual Connection Modes**: Supports both virtctl (OpenShift VMs) and SSH (bare metal/KVM)
- **Multiple Host Selection Methods**: Support for simple lists, patterns, labels, and external files
- **Parallel Execution**: Operations run in parallel across all hosts for efficiency
- **VM Migration Testing**: Optional VM migration during test execution to test migration resilience (virtctl mode only)
- **Persistent Mounts**: Optional `/etc/fstab` entries for automatic mounting after reboot
- **Flexible Workflows**: Support for preparation-only, full test, or results-copy-only modes
- **Comprehensive Logging**: Detailed logging with optional description-based file naming
- **Package Management**: Automatic checking and installation of required packages
- **Results Collection**: Automatic collection and organization of test results
- **Smart Timeout Handling**: Intelligent verification of test startup to avoid false alarms

## Requirements

### Python Dependencies
- Python 3.6+
- PyYAML >= 5.4.1

See the [Installation](#installation) section below for detailed setup instructions.

### System Tools

**For virtctl mode (OpenShift/Kubernetes VMs):**
- `virtctl` - KubeVirt client tool for VM access
- `oc` (OpenShift CLI) - For Kubernetes/OpenShift cluster access

**For SSH-only mode (bare metal/KVM hosts):**
- `ssh` - OpenSSH client (standard on most Linux systems)

### SSH Access Requirements

**Passwordless SSH access is required** for the scripts to function properly. The scripts execute commands remotely and cannot prompt for passwords.

**For virtctl mode:**
- Passwordless SSH access via `virtctl ssh` must be configured. Easiest way is to build in secret at machine creatition time. Secret [virtual machine template example](https://github.com/ekuric/ioscale/blob/main/templates/vmdbtest.yml#L58) and how to create kubernetes [secret to be used in virutal machines](https://github.com/ekuric/ioscale/blob/main/templates/secretgen.sh)
- Ensure your OpenShift/Kubernetes credentials allow VM access

**For SSH-only mode (bare metal/KVM hosts):**
- Passwordless SSH key-based authentication must be configured for all target hosts
- Configure SSH keys using:
  ```bash
  ssh-copy-id root@your-host
  # Or manually copy your public key to ~/.ssh/authorized_keys on each host
  ```
- Verify access works: `ssh root@your-host` should connect without password prompt. If this is not working, do not proceed before it is fixed. 


**Note**: The script auto-detects which mode to use based on the `--ssh-only` or `--virtctl-only` flags, or by checking if hosts are VMs in the namespace.

## Installation

1. **Ensure Python 3.6+ is installed**
   ```bash
   python3 --version  # Should show Python 3.6 or higher
   ```

2. **Install dependencies (choose one method):**
   
   **Using `uv` (recommended - faster and more reliable):**
   ```bash
   # Install uv (on Fedora)
   dnf install uv
   
   # Or install from source
   curl -LsSf https://astral.sh/uv/install.sh | sh
   
   # Create and activate virtual environment
   uv venv
   source .venv/bin/activate
   
   # Install dependencies
   uv pip install -r requirements.txt
   # Or install PyYAML only
   uv pip install "PyYAML>=5.4.1"
   ```
   
   **Using `pip` with virtual environment:**
   ```bash
   # Create virtual environment
   python3 -m venv venv
   source venv/bin/activate
   
   # Install dependencies
   pip install -r requirements.txt
   # Or install PyYAML only
   pip install "PyYAML>=5.4.1"
   ```

3. **For virtctl mode**: Ensure `virtctl` and `oc` are in your PATH
   ```bash
   which virtctl oc  # Verify tools are available
   ```

4. **For SSH-only mode**: Ensure `ssh` is available (standard on Linux)
   ```bash
   which ssh  # Verify SSH is available
   ```

5. **Clone or download the script and configuration files**
   ```bash
   git clone <repository-url>
   cd iops
   ```

## Configuration File

The script uses a YAML configuration file (default: `mariadb-config.yaml`). See `mariadb-config.yaml` for a complete example.

### Configuration Sections

#### Top-Level Description
```yaml
description: "my test description"  # Optional: Used in log and result file naming
```

#### Storage Configuration
```yaml
storage:
  mount_point: null              # OR use mount_point ( if left "null", "/perf1" will be used as mount point as default where device from disk_list be mounted )
  disk_list: "/dev/vdc"          # Block device (e.g., "/dev/vdc")
  persistent: ""                  # "true" to create /etc/fstab entries, "" for temporary mounts
```

**Note**: Either `mount_point` OR `disk_list` must be specified (set the other to `null`).

#### Database Configuration

**For virtctl mode (OpenShift/Kubernetes VMs):**
```yaml
database:
  # Host selection (choose one method):
  hosts: "vm1 vm2 vm3"           # Method 1: Simple list
  # host_pattern: "db{1..200}"    # Method 2: Pattern expansion
  # host_labels: "app=mariadb"    # Method 3: Label selector (virtctl only)
  # host_file: "/path/to/hosts.txt"  # Method 4: External file
  
  namespace: "default"           # Kubernetes namespace (required for virtctl mode)
  warehouse_count: 50            # Number of TPCC warehouses
  test_duration: 15             # Test duration in minutes
```

**For SSH-only mode (bare metal/KVM hosts):**
```yaml
database:
  # Host selection (choose one method):
  hosts: "server1 server2"       # Method 1: Simple list (recommended)
  # host_pattern: "server{1..10}"  # Method 2: Pattern expansion
  # host_file: "/path/to/hosts.txt"  # Method 3: External file
  # host_labels: NOT SUPPORTED    # Label selector not available in SSH-only mode
  
  # namespace: "default"          # Not needed for SSH-only mode (comment out or omit)
  warehouse_count: 50            # Number of TPCC warehouses
  test_duration: 15             # Test duration in minutes
```

**Host Selection Methods:**
1. **Simple List**: Space-separated hostnames (works for both modes)
2. **Pattern Expansion**: Bash-style ranges like `db{1..200}` or `server{1..10}` (works for both modes)
3. **Label Selector**: Kubernetes labels like `app=mariadb-test` (virtctl mode only)
4. **External File**: Path to file with one hostname per line (works for both modes)

#### Test Configuration
```yaml
test:
  user_count: "1 2 4 8"          # Space-separated list of user counts
  run_name: "HDB_MDB"            # Base name for test runs
  storage_type: "null"           # Storage type identifier
  log_level: "INFO"              # Logging level (DEBUG, INFO, WARN, ERROR)
```

#### HammerDB Configuration
```yaml
hammerdb:
  repo: "https://github.com/ekuric/fusion-access.git"
  path: "/root/hammerdb-tpcc-wrapper-scripts"
  install_dir: "/usr/local/HammerDB"
```

#### Migration Configuration (Optional - virtctl mode only)
```yaml
migrate:
  user_counts: "4 8"             # User counts that trigger migration
  interval: 0                    # Seconds between migrations (0 = parallel)
```

**Note 1**: VM migration is only available in virtctl mode (OpenShift/Kubernetes environments). Migration configuration is ignored when using `--ssh-only` mode (bare metal/KVM hosts).
**Note 2**: For virtual machine migration to work it is necessary that virtual machine disks are created with proper permissions - RWX - ReadWriteMany 


## Command-Line Options

### Basic Options

```bash
python3 mariadb.py [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `-c, --config FILE` | Path to YAML configuration file (default: `mariadb-config.yaml`) |
| `-v, --verbose` | Enable verbose/debug output |
| `--dry-run` | Validate configuration and show what would be done without executing |
| `--prepare-hosts` | Only run preparation steps (install packages, MariaDB setup) |
| `--copy-results` | Only copy results from hosts (skip all other steps) |
| `--ssh-only` | Force SSH for all hosts (bare metal/KVM, no virtctl) |
| `--virtctl-only` | Force virtctl for all hosts (OpenShift VMs) |

### Usage Examples

**Note**: If using a virtual environment, activate it first:
```bash
source .venv/bin/activate  # or: source venv/bin/activate
```

#### Basic Usage
```bash
# Use default config file (mariadb-config.yaml)
python3 mariadb.py

# Use custom config file
python3 mariadb.py -c my-config.yaml

# Verbose output
python3 mariadb.py -v
```

#### Preparation Mode
```bash
# Only prepare hosts (install packages, MariaDB, etc.)
python3 mariadb.py --prepare-hosts

# With verbose output
python3 mariadb.py --prepare-hosts -v
```

#### Results Copy Mode
```bash
# Only copy results without re-running tests
python3 mariadb.py --copy-results

# With custom config
python3 mariadb.py -c my-config.yaml --copy-results
```

#### Dry Run
```bash
# Validate configuration without executing
python3 mariadb.py --dry-run
```

#### SSH-Only Mode (Bare Metal/KVM)
```bash
# Run tests on bare metal servers or KVM hosts via SSH
python3 mariadb.py --ssh-only -c mariadb-ssh-config.yaml

# Prepare hosts via SSH
python3 mariadb.py --ssh-only --prepare-hosts -c mariadb-ssh-config.yaml

# Copy results via SSH
python3 mariadb.py --ssh-only --copy-results -c mariadb-ssh-config.yaml
```

#### Virtctl Mode (OpenShift/Kubernetes VMs)
```bash
# Force virtctl mode (default behavior)
python3 mariadb.py --virtctl-only -c mariadb-config.yaml

# Auto-detection (checks if hosts are VMs)
python3 mariadb.py -c mariadb-config.yaml
```

## Workflow Modes

### 1. Full Test Mode (Default)
Runs the complete workflow:
1. Install dependencies on all VMs
2. Deploy HammerDB scripts
3. Install MariaDB
4. Build TPCC database
5. Run performance tests
6. Collect test results
7. Stop MariaDB and cleanup

```bash
python3 mariadb.py -c mariadb-config.yaml
```

### 2. Preparation Mode
Only prepares hosts for testing:
1. Install dependencies
2. Deploy HammerDB scripts
3. Install MariaDB

```bash
python3 mariadb.py --prepare-hosts
```

### 3. Results Copy Mode
Only copies results from hosts:
1. Collect test results from all VMs
2. Copy log files to results directory

```bash
python3 mariadb.py --copy-results
```

## Advanced Features

### VM Migration Testing (virtctl mode only)

**Important**: VM migration testing is only available in virtctl mode (OpenShift/Kubernetes environments). This feature is not available when using `--ssh-only` mode (bare metal/KVM hosts).

The script supports VM migration during test execution to test migration resilience. Migration occurs at the midpoint of the actual test duration (after rampup).

**Configuration:**
```yaml
migrate:
  user_counts: "4 8 16"          # User counts that trigger migration
  interval: 0                    # 0 = parallel, >0 = sequential with interval
```

**How it works:**
- Tests run with specified user counts
- For user counts listed in `migrate.user_counts`, VMs are migrated during the test
- Migration occurs at: `rampup_time (120s) + (test_duration / 2)`
- Example: For a 10-minute test, migration occurs at 2min + 5min = 7 minutes after test start
- Requires virtctl mode (OpenShift/Kubernetes) - migration configuration is ignored in SSH-only mode

### Persistent Mounts

When `storage.persistent` is set to `"true"`, the script creates `/etc/fstab` entries so that storage devices are automatically mounted after VM reboot.

**Configuration:**
```yaml
storage:
  disk_list: "/dev/vdc"
  persistent: "true"              # Enable persistent mounts
```

**What it does:**
- After formatting and mounting the device, creates an entry in `/etc/fstab`
- Format: `/dev/vdc /perf1 xfs defaults 0 0`
- The device will be automatically mounted on system reboot

### Package Management

The script automatically checks if required packages are installed and only installs missing ones:

**Packages checked/installed:**
- `git`, `curl`, `vim`, `wget` (basic tools)
- `mariadb`, `mariadb-server`, `mariadb-server-utils`, `mariadb-errmsg` (MariaDB packages)

**Behavior:**
- Checks each package before installation
- Only installs missing packages
- Provides clear logging about what was installed vs. already present

### Logging and Output Files

**Log Files:**
- Format: `mariadb-YYYYMMDD-description.txt` or `mariadb-YYYYMMDD.txt`
- Location: Current working directory
- Contains all script output with timestamps

**Results Directories:**
- Format: `mariadb-results-YYYYMMDD-HHMMSS-description/` or `mariadb-results-YYYYMMDD-HHMMSS/`
- Structure:
  ```
  mariadb-results-20241120-143022-test1/
  ├── vm1/
  │   ├── build_mariadb1.out
  │   ├── test_mariadb_2025.11.20_2pod_pod1_1.out
  │   └── ...
  ├── vm2/
  │   └── ...
  └── mariadb-20241120-test1.txt  # Log file copy
  ```

## Examples

### Example 1: Simple Test
```yaml
# mariadb-config.yaml
description: "baseline-test"
storage:
  disk_list: "/dev/vdc"
  persistent: ""
database:
  hosts: "vmdb-1 vmdb-2"
  namespace: "default"
  warehouse_count: 50
  test_duration: 5
test:
  user_count: "1 2 4"
```

```bash
python3 mariadb.py -c mariadb-config.yaml
```

### Example 2: Large Scale with Migration
```yaml
description: "migration-test"
storage:
  disk_list: "/dev/vdc"
  persistent: "true"
database:
  host_pattern: "vmdb-{1..50}"
  namespace: "database-testing"
  warehouse_count: 100
  test_duration: 15
test:
  user_count: "4 8 16 32"
migrate:
  user_counts: "8 16"
  interval: 5
```

### Example 3: Label-Based Selection
```yaml
database:
  host_labels: "app=mariadb-performance,env=production"
  namespace: "production"
  warehouse_count: 200
  test_duration: 30
```

### Example 4: Two-Phase Workflow
```bash
# Phase 1: Prepare all hosts
python3 mariadb.py --prepare-hosts -c mariadb-config.yaml

# Phase 2: Run tests (hosts already prepared)
python3 mariadb.py -c mariadb-config.yaml
```

### Example 5: Re-copy Results
```bash
# After tests complete, re-copy results without re-running
python3 mariadb.py --copy-results -c mariadb-config.yaml
```

### Example 6: SSH-Only Mode (Bare Metal/KVM)
```yaml
# mariadb-ssh-config.yaml
description: "bare-metal-test"
storage:
  mount_point: "/perf1"
  disk_list: "/dev/vdb"
  persistent: false
database:
  hosts: "server1 server2 server3"
  # namespace not needed for SSH-only mode
  warehouse_count: 50
  test_duration: 10
test:
  user_count: "1 2 4"
hammerdb:
  repo: "https://github.com/ekuric/fusion-access.git"
  path: "/root/hammerdb-tpcc-wrapper-scripts"
  install_dir: "/usr/local/HammerDB"
```

```bash
# Run on bare metal servers via SSH
python3 mariadb.py --ssh-only -c mariadb-ssh-config.yaml
```

**Note**: VM migration is not available in SSH-only mode since it requires Kubernetes/OpenShift.

## Troubleshooting

### Common Issues

#### 1. "Configuration file not found"
**Error**: `Configuration file 'mariadb-config.yaml' not found`

**Solution**: 
- Specify config file: `python3 mariadb.py -c /path/to/config.yaml`
- Or create `mariadb-config.yaml` in current directory

#### 2. "Virtual machine not found"
**Error**: `Virtual machine 'vm1' not found in namespace 'default'`

**Solution**:
- **If using SSH-only mode**: Use `--ssh-only` flag to skip VM validation
- **If using virtctl mode**: 
  - Verify VM exists: `oc get vm vm1 -n default`
  - Check namespace in config file
  - Ensure you're logged into the correct cluster

#### 3. "Failed to install MariaDB"
**Error**: Installation script fails

**Solution**:
- Check VM connectivity: `virtctl ssh root@vmi/vm1 -n default`
- Verify disk device exists and is not mounted
- Check logs for specific error messages
- Try `--verbose` for more details

#### 4. "Command timeout"
**Error**: `Command timeout on vm1: ... (timeout: 300s)` or `Command timeout on server1: Starting performance test`

**Solution**:
- The script now includes smart timeout handling - it verifies tests actually started even if SSH times out
- Check if the test process is running: `ssh root@server1 "ps aux | grep hammerdbcli"`
- Check if output file exists: `ssh root@server1 "ls -lh /usr/local/HammerDB/test_mariadb_*.out"`
- For very long operations, increase timeout in code if needed
- Check host performance and network connectivity
- Verify the operation is actually running on the host

#### 5. "No results found"
**Error**: Results collection finds no files

**Solution**:
- Verify tests actually completed successfully
- Check HammerDB output files on hosts manually:
  - virtctl mode: `virtctl ssh root@vmi/vm1 -n default "ls -lh /usr/local/HammerDB/test_mariadb_*.out"`
  - SSH-only mode: `ssh root@server1 "ls -lh /usr/local/HammerDB/test_mariadb_*.out"`
- Use `--copy-results` to re-copy if files exist

#### 6. "templates/mariadb directory not found"
**Error**: `ERROR: Clone completed but templates/mariadb directory not found`

**Solution**:
- Verify repository URL is correct: `https://github.com/ekuric/fusion-access.git`
- Check repository structure - templates should be in the cloned repository
- The script now includes better diagnostics - check the error output for repository structure details
- Ensure git submodules are initialized (script handles this automatically)

### Debugging Tips

1. **Use verbose mode**: `python3 mariadb.py -v`
2. **Use dry-run**: `python3 mariadb.py --dry-run` to validate config
3. **Check log files**: Review `mariadb-YYYYMMDD-*.txt` for detailed output
4. **Manual host access**:
   - virtctl mode: `virtctl ssh root@vmi/vm1 -n namespace` to debug on VMs
   - SSH-only mode: `ssh root@server1` to debug on bare metal/KVM hosts
5. **Check HammerDB logs**: Look in `/usr/local/HammerDB/` on hosts for HammerDB output files
6. **Verify test startup**: The script now verifies tests actually started - check logs for "✓ Test confirmed running" messages
7. **Check SSH connectivity**: For SSH-only mode, ensure passwordless SSH access is configured

## Output and Results

### Test Output Files

On each VM, test results are stored in:
- Build files: `build_mariadb*.out`
- Test files: `test_mariadb_YYYY.MM.DD_Npod_podN_USERCOUNT.out`

### Results Collection

Results are automatically:
1. Archived into `mariadb-results.tar.gz` on each host
2. Copied to localhost:
   - virtctl mode: via `virtctl scp` (with fallback to SSH+base64)
   - SSH-only mode: via `scp`
3. Extracted into organized directory structure
4. Log file is copied to results directory

### Performance Metrics

TPM (Transactions Per Minute) values are extracted from test output files and displayed in the summary.

## Best Practices

1. **Use descriptions**: Always set `description` in config for better log/results organization
2. **Two-phase workflow**: For large deployments, use `--prepare-hosts` first, then run tests
3. **Persistent mounts**: Use `persistent: "true"` if VMs will be rebooted
4. **Dry-run first**: Always validate config with `--dry-run` before running
5. **Monitor logs**: Watch log files during execution for early problem detection
6. **Backup results**: Results directories are timestamped - keep important ones

## Limitations

**For virtctl mode:**
- Requires Kubernetes/OpenShift cluster with KubeVirt
- Requires `virtctl` and `oc` tools
- VM migration testing only available in virtctl mode

**For SSH-only mode:**
- Requires SSH access to target hosts
- VM migration testing not available (requires Kubernetes)
- Label-based host selection not available (requires Kubernetes)

**Common limitations:**
- All operations run as root on target hosts
- Requires network connectivity to hosts
- HammerDB scripts must be accessible via git repository
- Storage devices must be available and not in use
- SSH key-based authentication recommended for SSH-only mode

## Support

For issues or questions:
1. Check log files for detailed error messages
2. Use `--verbose` and `--dry-run` for debugging
3. Review configuration file syntax
4. Verify VM accessibility and permissions

## Version History

- **Current Version**: Supports YAML configuration, SSH-only mode, VM migration, persistent mounts, and flexible workflows
- Features: 
  - Dual connection modes (virtctl and SSH-only)
  - Package management
  - Parallel execution
  - Comprehensive logging
  - Results collection
  - Smart timeout handling and test verification
  - Improved git clone process with submodule support

---

## Connection Modes

### Virtctl Mode (Default)
For OpenShift/Kubernetes virtual machines:
- Uses `virtctl` and `oc` for VM access
- Supports VM migration testing
- Supports label-based host selection
- Requires namespace configuration
- Auto-detected if hosts are VMs in the namespace

### SSH-Only Mode
For bare metal servers or KVM hosts:
- Uses standard SSH for host access
- No Kubernetes/OpenShift dependencies
- Simpler configuration (no namespace needed)
- Use `--ssh-only` flag to enable
- Recommended for KVM/bare metal environments

**Note**: The script is designed for performance testing in both Kubernetes/OpenShift environments (with KubeVirt) and bare metal/KVM environments. Ensure you have proper permissions and access to the hosts before running tests.


