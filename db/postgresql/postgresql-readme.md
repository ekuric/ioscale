# PostgreSQL HammerDB TPCC Testing Script

## Overview

`postgresql.py` is a comprehensive Python script for running PostgreSQL performance tests using HammerDB TPCC benchmarks on Kubernetes virtual machines. The script automates the entire testing workflow including package installation, database setup, test execution, VM migration testing, and results collection.

## Features

- **YAML-based Configuration**: All settings configured via YAML file
- **Multiple Host Selection Methods**: Support for simple lists, patterns, labels, and external files
- **Parallel Execution**: Operations run in parallel across all hosts for efficiency
- **VM Migration Testing**: Optional VM migration during test execution to test migration resilience
- **Persistent Mounts**: Optional `/etc/fstab` entries for automatic mounting after reboot
- **Flexible Workflows**: Support for preparation-only, full test, or results-copy-only modes
- **Comprehensive Logging**: Detailed logging with optional description-based file naming
- **Package Management**: Automatic checking and installation of required packages
- **Results Collection**: Automatic collection and organization of test results

## Requirements

### Python Dependencies
- Python 3.6+
- PyYAML >= 5.4.1

Install dependencies:
```bash
pip install PyYAML>=5.4.1
# or
pip install -r requirements.txt
```

### System Tools
- `virtctl` - KubeVirt client tool for VM access
- `oc` (OpenShift CLI) - For Kubernetes/OpenShift cluster access

## Installation

1. Ensure Python 3.6+ is installed
2. Install PyYAML: `pip install PyYAML>=5.4.1`
3. Ensure `virtctl` and `oc` are in your PATH
4. Clone or download the script and configuration files

## Configuration File

The script uses a YAML configuration file (default: `postgresql-config.yaml`). See `postgresql-config.yaml` for a complete example.

### Configuration Sections

#### Top-Level Description
```yaml
description: "my test description"  # Optional: Used in log and result file naming
```

#### Storage Configuration
```yaml
storage:
  mount_point: null              # OR use mount_point (e.g., "/perf1")
  disk_list: "/dev/vdc"          # Block device (e.g., "/dev/vdc")
  persistent: ""                  # "true" to create /etc/fstab entries, "" for temporary mounts
```

**Note**: Either `mount_point` OR `disk_list` must be specified (set the other to `null`).

#### Database Configuration
```yaml
database:
  # Host selection (choose one method):
  hosts: "vm1 vm2 vm3"           # Method 1: Simple list
  # host_pattern: "pg{1..200}"    # Method 2: Pattern expansion
  # host_labels: "app=postgresql"  # Method 3: Label selector
  # host_file: "/path/to/hosts.txt"  # Method 4: External file
  
  namespace: "default"           # Kubernetes namespace
  warehouse_count: 50            # Number of TPCC warehouses
  test_duration: 15             # Test duration in minutes
```

**Host Selection Methods:**
1. **Simple List**: Space-separated hostnames
2. **Pattern Expansion**: Bash-style ranges like `pg{1..200}` or `postgres-{001..050}`
3. **Label Selector**: Kubernetes labels like `app=postgresql-test` or `env=prod,tier=db`
4. **External File**: Path to file with one hostname per line

#### Test Configuration
```yaml
test:
  user_count: "1 2 4 8"          # Space-separated list of user counts
  log_level: "INFO"              # Logging level (DEBUG, INFO, WARN, ERROR)
```

#### HammerDB Configuration
```yaml
hammerdb:
  repo: "https://github.com/ekuric/fusion-access.git"
  path: "/root/hammerdb-tpcc-wrapper-scripts"
```

#### Migration Configuration (Optional)
```yaml
migrate:
  user_counts: "4 8"             # User counts that trigger migration
  interval: 0                    # Seconds between migrations (0 = parallel)
```

## Command-Line Options

### Basic Options

```bash
python3 postgresql.py [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `-c, --config FILE` | Path to YAML configuration file (default: `postgresql-config.yaml`) |
| `-v, --verbose` | Enable verbose/debug output |
| `--dry-run` | Validate configuration and show what would be done without executing |
| `--prepare-hosts` | Only run preparation steps (install packages, PostgreSQL setup) |
| `--copy-results` | Only copy results from hosts (skip all other steps) |

### Usage Examples

#### Basic Usage
```bash
# Use default config file (postgresql-config.yaml)
python3 postgresql.py

# Use custom config file
python3 postgresql.py -c my-config.yaml

# Verbose output
python3 postgresql.py -v
```

#### Preparation Mode
```bash
# Only prepare hosts (install packages, PostgreSQL, etc.)
python3 postgresql.py --prepare-hosts

# With verbose output
python3 postgresql.py --prepare-hosts -v
```

#### Results Copy Mode
```bash
# Only copy results without re-running tests
python3 postgresql.py --copy-results

# With custom config
python3 postgresql.py -c my-config.yaml --copy-results
```

#### Dry Run
```bash
# Validate configuration without executing
python3 postgresql.py --dry-run
```

## Workflow Modes

### 1. Full Test Mode (Default)
Runs the complete workflow:
1. Install dependencies on all VMs
2. Deploy HammerDB scripts
3. Install PostgreSQL
4. Build TPCC database
5. Run performance tests
6. Collect test results
7. Stop PostgreSQL and cleanup

```bash
python3 postgresql.py -c postgresql-config.yaml
```

### 2. Preparation Mode
Only prepares hosts for testing:
1. Install dependencies
2. Deploy HammerDB scripts
3. Install PostgreSQL

```bash
python3 postgresql.py --prepare-hosts
```

### 3. Results Copy Mode
Only copies results from hosts:
1. Collect test results from all VMs
2. Copy log files to results directory

```bash
python3 postgresql.py --copy-results
```

## Advanced Features

### VM Migration Testing

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
- `postgresql`, `postgresql-contrib`, `postgresql-server`, `glibc-langpack-en`, `libpq` (PostgreSQL packages)

**Behavior:**
- Checks each package before installation
- Only installs missing packages
- Provides clear logging about what was installed vs. already present

### Logging and Output Files

**Log Files:**
- Format: `postgresql-YYYYMMDD-description.txt` or `postgresql-YYYYMMDD.txt`
- Location: Current working directory
- Contains all script output with timestamps

**Results Directories:**
- Format: `postgresql-results-YYYYMMDD-HHMMSS-description/` or `postgresql-results-YYYYMMDD-HHMMSS/`
- Structure:
  ```
  postgresql-results-20241120-143022-test1/
  ├── vm1/
  │   ├── build_pg1.out
  │   ├── test_postgresql_pg_2025.11.20_2pod_pod1_1.out
  │   └── ...
  ├── vm2/
  │   └── ...
  └── postgresql-20241120-test1.txt  # Log file copy
  ```

## Examples

### Example 1: Simple Test
```yaml
# postgresql-config.yaml
description: "baseline-test"
storage:
  disk_list: "/dev/vdc"
  persistent: ""
database:
  hosts: "vmpg-1 vmpg-2"
  namespace: "default"
  warehouse_count: 50
  test_duration: 5
test:
  user_count: "1 2 4"
```

```bash
python3 postgresql.py -c postgresql-config.yaml
```

### Example 2: Large Scale with Migration
```yaml
description: "migration-test"
storage:
  disk_list: "/dev/vdc"
  persistent: "true"
database:
  host_pattern: "vmpg-{1..50}"
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
  host_labels: "app=postgresql-performance,env=production"
  namespace: "production"
  warehouse_count: 200
  test_duration: 30
```

### Example 4: Two-Phase Workflow
```bash
# Phase 1: Prepare all hosts
python3 postgresql.py --prepare-hosts -c postgresql-config.yaml

# Phase 2: Run tests (hosts already prepared)
python3 postgresql.py -c postgresql-config.yaml
```

### Example 5: Re-copy Results
```bash
# After tests complete, re-copy results without re-running
python3 postgresql.py --copy-results -c postgresql-config.yaml
```

## Troubleshooting

### Common Issues

#### 1. "Configuration file not found"
**Error**: `Configuration file 'postgresql-config.yaml' not found`

**Solution**: 
- Specify config file: `python3 postgresql.py -c /path/to/config.yaml`
- Or create `postgresql-config.yaml` in current directory

#### 2. "Virtual machine not found"
**Error**: `Virtual machine 'vm1' not found in namespace 'default'`

**Solution**:
- Verify VM exists: `oc get vm vm1 -n default`
- Check namespace in config file
- Ensure you're logged into the correct cluster

#### 3. "Failed to install PostgreSQL"
**Error**: Installation script fails

**Solution**:
- Check VM connectivity: `virtctl ssh root@vmi/vm1 -n default`
- Verify disk device exists and is not mounted
- Check logs for specific error messages
- Try `--verbose` for more details

#### 4. "Command timeout"
**Error**: `Command timeout on vm1: ... (timeout: 300s)`

**Solution**:
- Increase timeout in code if needed (for very long operations)
- Check VM performance and network connectivity
- Verify the operation is actually running on the VM

#### 5. "No results found"
**Error**: Results collection finds no files

**Solution**:
- Verify tests actually completed successfully
- Check HammerDB output files on VMs manually
- Use `--copy-results` to re-copy if files exist

### Debugging Tips

1. **Use verbose mode**: `python3 postgresql.py -v`
2. **Use dry-run**: `python3 postgresql.py --dry-run` to validate config
3. **Check log files**: Review `postgresql-YYYYMMDD-*.txt` for detailed output
4. **Manual VM access**: Use `virtctl ssh root@vmi/vm1 -n namespace` to debug on VMs
5. **Check HammerDB logs**: Look in `/usr/local/HammerDB/` on VMs for HammerDB output files

## Output and Results

### Test Output Files

On each VM, test results are stored in:
- Build files: `build_pg*.out`
- Test files: `test_postgresql_pg_YYYY.MM.DD_Npod_podN_USERCOUNT.out`

**Note**: The script also supports older file naming patterns (`test_ESX_pg_*`) for backward compatibility with results analysis tools.

### Results Collection

Results are automatically:
1. Archived into `postgresql-results.tar.gz` on each VM
2. Copied to localhost via `virtctl scp`
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

## Differences from MariaDB Script

While `postgresql.py` follows the same structure as `mariadb.py`, there are some PostgreSQL-specific differences:

1. **Package names**: Uses PostgreSQL packages instead of MariaDB
2. **Database setup**: Uses PostgreSQL-specific initialization and configuration
3. **File naming**: Uses `pg` prefix in output files (e.g., `build_pg1.out`)
4. **VM number extraction**: Automatically extracts VM number from hostname for file naming
5. **Backward compatibility**: Supports both `test_postgresql_pg_*` and `test_ESX_pg_*` file patterns

## Limitations

- Requires Kubernetes/OpenShift cluster with KubeVirt
- All operations run as root on target VMs
- Requires network connectivity to VMs
- HammerDB scripts must be accessible via git repository
- Storage devices must be available and not in use

## Support

For issues or questions:
1. Check log files for detailed error messages
2. Use `--verbose` and `--dry-run` for debugging
3. Review configuration file syntax
4. Verify VM accessibility and permissions

## Version History

- **Current Version**: Supports YAML configuration, VM migration, persistent mounts, and flexible workflows
- Features: Package management, parallel execution, comprehensive logging, results collection

---

**Note**: This script is designed for performance testing in Kubernetes/OpenShift environments with KubeVirt. Ensure you have proper permissions and access to the cluster before running tests.


