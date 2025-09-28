# Generic I/O Tests with FIO - Flexible I/O tester  

This directory contains FIO (Flexible I/O Tester) performance testing scripts for linux virtual machines. 

It is tested with:

- OpenShift Container Platform Virtualization virtual machines
- baremetal hosts
- kvm hosts
- VMware virtual machines

It can run tests only on RHOCPV virtual machines, or at same time combined with kvm/baremetal hosts. 

### Red Hat OpenShift Virtualization Prerequisites 

- Functional RHOCPV environment for case when Red Hat OpenShift Virtualization virtual machines are expected to be tested
- Working storage backend which can be used by RHOCPV to create virtual machines. 
Any storage class available in RHOCPV environment will be fine. We will use storage class built on top of ODF ( OpenShift Data Foundation ) storage  
- Passwordless ssh access from command node to all test virtual machines. We can use `kubectl create secret generic vmkeyroot  --from-file=/root/.ssh/id_rsa.pub` to generate secret which will be later used when machines are created by [multivm.sh](https://github.com/ekuric/rhblog/blob/main/multivm.sh) in Step 1.

### Baremetal / KVM / VMware machines testing prerequestis 

- Free block device for testing
- Passwordless ssh access to test machines


### General Prerequisites

- command / bastion node from where tests will be orchestrated. This can be any machine from where we can reach to  RHOCPV machines via `ssh` and `oc`. For baremetal, kvm , vmware test case `ssh` access from bastion to test machines must be functional
- Preinstalled FIO packages on test machines, or set up proper repositories from where necessary packages can be installed. If different image than upstream Fedora/Centos Stream is used then ensure that machine is subscribed to proper channels. 


### Step 1: Create Test VMs

We have template for single test machine 
```bash
# Use the provided template
oc apply -f templates/geniotest.yml
```
For virtual machine image we use current Fedora ( at time of this writing it is Fedora 42 ). Generally any RHEL based distribution will work. `dnf` is used as packet manager, so Debian based images will not work out of box. 

For multiple virtual machine creation it is possible also to use [multivm.sh](https://github.com/ekuric/rhblog/blob/main/multivm.sh) which we created for this purpose. Run `./multivm.sh -h` to see all options it offers.

In order to create 10 test virtual machines we can ran on the bastion host

```bash
$ ./multivm.sh -p vm --cores 8 --sockets 2 --threads 1 --memory 16Gi -s 1 -e 5 
```

adapt CPU/Memory values to correspond specific test needs. 

### Step 2: Configure Tests

`fio-config.yaml` contain all necessary for test, you will need to adapt it for your specific test case! 

```bash
# Edit the FIO configuration
vim io-generic/fio-config.yaml
```

Example configuration:
```yaml

# VM/Host Configuration
vm:
  # Method 1: External host file (recommended for mixed environments)
  #host_file: "hosts.txt"
  
  # Alternative Method 2: Simple host list with mixed VM and server names
  #hosts: "vm-1 vm-2 myhost.test.com" 
  #  vm-3 vm-4 vm-5" 
  # server1 server2 vm-3"
  
  # Alternative Method 3: Host pattern expansion
  host_pattern: "vm-{1..100}"
  
  # Alternative Method 4: Label-based selection (VMs only)
  #host_labels: "type=vm"
  
  # OpenShift/Kubernetes namespace for VMs (ignored for regular servers)
  namespace: "default"

# Storage Configuration
storage:
  # Per-host device configuration (MANDATORY - no global fallback for safety)
  devices:
    # OpenShift VMs (typically use virtual devices) - using patterns for efficiency
    "vm-{1..100}": "vdc"
    
    # Individual hosts (for special cases)
    "myhost.test.com": "sdc"
  
  # Mount point for the test filesystem
  mount_point: "/root/tests/data"
  
  # Filesystem type to create
  filesystem: "xfs"

# FIO Test Configuration
fio:
  # Test file size (e.g., "1G", "500M", "10G")
  test_size: "10G"
  
  # Test runtime in seconds
  runtime: "300"
  
  # Block sizes to test (space-separated)
  block_sizes: "4k 8k 128k 1024k 4096k"
  
  # I/O patterns to test (space-separated)
  io_patterns: "read write randread randwrite"
  
  # Number of parallel jobs
  numjobs: "4"
  
  # I/O depth
  iodepth: "16"
  
  # Direct I/O (bypass page cache)
  direct_io: "1"
  
  # rate_iops - if not set then it is ignore, if set - rate_iops value will be used in test
  # rate_iops: "500"

# Output Configuration
output:
  # Directory to store results on remote hosts
  directory: "/root/fio-results"
  
  # Output format (json+, normal, etc.)
  format: "json+"
```

This is an example of fio-config.yaml. It is possible to edit it and adapt to specific test scenarios.

It is important to point out below points

- in `devices:` section we must specify devices to be used for test. For virtual machines we can use patterns as in most cases if virtual machines are created with additional disk for testing it will be presented as `vdc` and in this case we can specify pattern
```
vm-{1..100}: "vdc"  
``` 
What means for virtual machines `vm-{1..100}` we want to use `vdc` block device for testing. 

If we ran test in heterogenous environment and want to use specific disk on particular machine then it is necessary to note that in such scenario we must use `hosts.txt` file specified as `host_file: /path/to/hosts.txt"` file where we specify hosts we want to test. 
In that case host which is not listed in host_pattern range ( eg. vm-{1..100} ) can get specific device name in `devices` section. For example

```
myhost.test.com: "sdc" 
```

As stated above,best way to specify hosts when testing different configuration is to use `host_file`
An example of `hosts.txt` is listed below. 

```bash
vm-{1..100}
myhost.test.com
```
With custom created `hosts.txt` we can various machines and use different block devices on these machines for test.


### Step 3: Validate Configuration

If not specified different configuration file with `-c` option, then default `fio-config.yaml` will be used. 

```bash
cd io-generic
./fio-tests.sh --dry-run
```
it will produce output showing us what will be executed 

```bash
[2025-09-26 08:30:21] INFO Starting FIO remote testing script
[2025-09-26 08:30:21] INFO Using auto-detection: virtctl for VMs, SSH for regular hosts
[2025-09-26 08:30:21] INFO Using host file: mixed-hosts.txt
[2025-09-26 08:30:21] INFO Loaded 3 hosts from file: mixed-hosts.txt
[2025-09-26 08:30:21] INFO Configuration loaded from: fio-config.yaml
[2025-09-26 08:30:21] INFO VMs: vm-1 vm-2 myhost.test.com
[2025-09-26 08:30:21] INFO Namespace: default
[2025-09-26 08:30:21] INFO Host connection methods (auto-detection):
[2025-09-26 08:30:21] INFO   vm-1: virtctl (VM detected)
[2025-09-26 08:30:21] INFO   vm-2: virtctl (VM detected)
[2025-09-26 08:30:21] INFO   myhost.test.com: SSH (regular host)
[2025-09-26 08:30:21] INFO Storage device configuration:
[2025-09-26 08:30:21] INFO   vm-1: /dev/vdc
[2025-09-26 08:30:21] INFO   vm-2: /dev/vdc
[2025-09-26 08:30:21] INFO   myhost.test.com: /dev/sdc
[2025-09-26 08:30:21] INFO Mount point: /root/tests/data
[2025-09-26 08:30:21] INFO Filesystem: xfs
[2025-09-26 08:30:21] INFO Test size: 1G
[2025-09-26 08:30:21] INFO Runtime: 60s
[2025-09-26 08:30:21] INFO Block sizes: 4k 8k 128k 1024k
[2025-09-26 08:30:21] INFO I/O patterns: read write randread randwrite
[2025-09-26 08:30:21] INFO Number of jobs: 4
[2025-09-26 08:30:21] INFO I/O depth: 16
[2025-09-26 08:30:21] INFO Direct I/O: 1
[2025-09-26 08:30:21] INFO Output directory: /root/fio-results
[2025-09-26 08:30:21] INFO Skipping VM validation in dry-run mode
[2025-09-26 08:30:21] INFO DRY RUN MODE: Configuration validated successfully
[2025-09-26 08:30:21] INFO Would execute the following steps:
[2025-09-26 08:30:21] INFO   1. Install FIO and dependencies on VMs
[2025-09-26 08:30:21] INFO   2. Prepare storage (format and mount devices) - 5 parallel steps
[2025-09-26 08:30:21] INFO   3. Write initial test dataset
[2025-09-26 08:30:21] INFO   4. Run FIO performance tests with different patterns and block sizes
[2025-09-26 08:30:21] INFO   5. Collect test results
[2025-09-26 08:30:21] INFO   6. Clean up test environment (3 steps: storage, processes, results)
[2025-09-26 08:30:21] INFO 
[2025-09-26 08:30:21] INFO WARNING: Step 2 will format the specified storage device!
[2025-09-26 08:30:21] INFO Use without --dry-run to execute the actual tests
``` 

If everything is as we want - we can execute test. Do not execute test if there is issue with devices listed for test. These devices will be formatted and it is important to have correct devices for test.

### Step 4: Execute Tests
```bash
./fio-tests.sh -c fio-config.yaml 
```

If you want to avoid prompt to confirm to proceed with test, then it is possible to use  `--yes-i-mean-it` what will force test to proceed without asking to confirm it. 
Use option `--yes-i-mean-it` when you are sure that you have proper configuration in `hosts` and `devices` section of `fio-config.yaml`.


Depending on specified IO operations and test duration test can take different time to finish. 


### Step 5: Results Collection

Results are automatically copied to your local machine in in timestamped directory.

```bash

$ ls -l fio-results-20231215-143022/
#   ├── vm1/
#   │   ├── fio-test-randread-bs-4k.json
#   │   ├── fio-test-randwrite-bs-4k.json
#   │   └── write_dataset.json
#   └── vm2/
#       ├── fio-test-randread-bs-4k.json
#       └── ...
```


## Smart Host Management 🚀

For large-scale testing (10s, 100s, or 1000s of VMs), manually listing hosts is impractical. Below examples show how to 
manage many test hosts easily. 


### 1. Host Range Patterns
```yaml
vm:
  host_pattern: "vm{1..200}"          # Creates: vm1, vm2, ..., vm200
  # host_pattern: "test-{001..050}"   # Zero-padded: test-001, test-002, ..., test-050
  # host_pattern: "worker-{1..10}"    # Creates: worker-1, worker-2, ..., worker-10
```

### 2. Label-Based Selection (Recommended for Dynamic Environments)
```yaml
vm:
  host_labels: "app=fio-test"                     # Select VMs with this label
  # host_labels: "env=perf,workload=storage"     # Multiple labels (AND condition)
  # host_labels: "tier=storage"                  # Custom organizational labels
```

First, label your VMs:
```bash
# Label VMs for FIO testing
oc label vm vm1 vm2 vm3 app=fio-test
oc label vm storage-vm{1..50} workload=storage-performance

# Verify labels
oc get vms -l app=fio-test
```

### 3. External Host Files
```yaml
vm:
  host_file: "./hosts.txt"    # File with hostnames (one per line)
```

Example of host file content: 
```bash
vm-{1..100}
myhost.test.com
EOF
```

### Testing Your Host Selection
```bash
# Debug host selection without running tests
./fio-tests.sh -c config.yaml --debug --dry-run
```

## Requirements

- **yq**: YAML processor (`sudo dnf install yq`)
- **virtctl**: Kubernetes VM management tool
- **oc**: OpenShift CLI (for label-based selection)
- Virtual machines must be created and accessible via SSH
Using generic [secret](https://github.com/ekuric/fusion-access/blob/main/templates/secretgen.sh) created before virtual machines are created is easiest way to create virtual machine with predefined ssh key for ssh access. Example virtual machine template is listed 
[here](https://github.com/ekuric/fusion-access/blob/main/templates/geniotest.yml)

## Safety Notes

⚠️ **WARNING**: The FIO tests format storage devices, which is destructive!
- Always use `--dry-run` first to validate configuration
- Ensure you're testing the correct devices
- Backup any important data before testing
- The script will ask for confirmation before formatting devices

## Results Format

FIO results is generated in `.json` format and contain full test results output with detailed performance metrics:

- IOPS (Input/Output Operations Per Second)
- Bandwidth (MB/s)
- Latency percentiles
- CPU usage
- And more...

From these files test results can be extracted. We will create scripts / tools to automate this. 

# FIO Test Configuration Examples

This directory contains example configuration files for the `fio-tests.sh` script, demonstrating different connection modes and host selection methods.

## Configuration Files

### 1. SSH Only Mode
- **File**: `fio-config-ssh-only.yaml`
- **Purpose**: For testing regular servers accessible via direct SSH
- **Usage**: `./fio-tests.sh --ssh-only -c fio-config-ssh-only.yaml`

### 2. Virtctl Only Mode
- **File**: `fio-config-virtctl-only.yaml`
- **Purpose**: For testing VMs accessible via virtctl/oc commands
- **Usage**: `./fio-tests.sh --virtctl-only -c fio-config-virtctl-only.yaml`

### 3. Mixed Hosts Mode
- **File**: `fio-config-mixed-hosts.yaml`
- **Purpose**: For testing both OpenShift VMs and bare metal servers with auto-detection
- **Usage**: `./fio-tests.sh -c fio-config-mixed-hosts.yaml`
- **Requires**: `mixed-hosts.txt` file with both VM and server hostnames

### 4. Comprehensive Examples
- **File**: `fio-config-examples.yaml`
- **Purpose**: Demonstrates all host selection methods and configurations
- **Usage**: Uncomment the desired example section

### 5. Per-Host Device Configuration
- **File**: `fio-config-per-host-devices.yaml`
- **Purpose**: Demonstrates per-host storage device configuration
- **Usage**: `./fio-tests.sh --ssh-only -c fio-config-per-host-devices.yaml`

### 6. Mixed Device Configuration
- **File**: `fio-config-mixed-devices.yaml`
- **Purpose**: Demonstrates per-host device configuration for multiple hosts
- **Usage**: `./fio-tests.sh --ssh-only -c fio-config-mixed-devices.yaml`

### 7. Host Patterns in Devices
- **File**: `fio-config-host-patterns.yaml`
- **Purpose**: Demonstrates using host patterns in the devices section for efficient configuration
- **Usage**: `./fio-tests.sh -c fio-config-host-patterns.yaml`

## Host Selection Methods

The script supports four different methods for specifying hosts:

### Method 1: Simple Host List
```yaml
vm:
  hosts: "server1 server2 server3"
```
- **Best for**: Small, fixed sets of hosts
- **Works with**: All connection modes

### Method 2: Host Pattern Expansion
```yaml
vm:
  host_pattern: "vm-{1..100}"
```
- **Best for**: Large sets of sequentially named hosts
- **Works with**: All connection modes
- **Note**: Uses bash brace expansion

### Method 3: External Host File
```yaml
vm:
  host_file: "hosts.txt"
```
- **Best for**: Mixed environments, dynamic host lists
- **Works with**: All connection modes
- **File format**: One hostname per line, comments start with #

### Method 4: Label-Based Selection
```yaml
vm:
  host_labels: "environment=test,workload=fio"
```
- **Best for**: Kubernetes/OpenShift environments
- **Works with**: Virtctl only mode
- **Note**: Queries VMs using `oc get vms -l <labels>`

## Per-Host Device Configuration

The script **requires** per-host storage device configuration for safety. Each host must have its storage device explicitly specified. This prevents accidental formatting of the wrong disk (like the OS disk). This is particularly important when:

- Different hosts have different storage hardware (SATA, NVMe, virtual disks)
- Some hosts have multiple storage devices and you want to test specific ones
- You need to ensure safety by explicitly specifying which device to test

### Configuration Method

#### Per-Host Device Configuration (MANDATORY)
```yaml
storage:
  devices:
    server1: "sdb"      # /dev/sdb for server1
    server2: "sda"      # /dev/sda for server2
    server3: "nvme0n1"  # /dev/nvme0n1 for server3
```


### Device Name Format
- **Do NOT include** `/dev/` prefix in configuration
- Use just the device name: `sda`, `sdb`, `nvme0n1`, `vdb`
- The script automatically adds `/dev/` prefix when using the device

### Hostname Format
- **Quote hostnames** that contain dots or special characters in YAML
- Example: `"special-server.my.node.com": "sda"`
- The script automatically handles quoted hostnames in the configuration

### Benefits
- **Hardware Flexibility**: Test different storage types in the same run
- **Fine-tuning**: Optimize device selection per host
- **Mixed Environments**: Handle hosts with different storage configurations

## Host Patterns in Devices Section

The devices section now supports host patterns for efficient configuration of multiple hosts with the same device:

```yaml
storage:
  devices:
    # Host patterns for groups of hosts with same device
    "vm-{1..5}": "vdc"           # vm-1, vm-2, vm-3, vm-4, vm-5 all use /dev/vdc
    "server-{1..3}": "sdb"       # server-1, server-2, server-3 all use /dev/sdb
    "worker-{1..10}": "nvme0n1"  # worker-1 through worker-10 all use /dev/nvme0n1
    "test-{001..050}": "vdb"     # test-001 through test-050 all use /dev/vdb (zero-padded)
    
    # Individual hosts can still be specified
    "special-server": "sda"
```

### Pattern Syntax
- **Range patterns**: `"vm-{1..5}"` expands to `vm-1`, `vm-2`, `vm-3`, `vm-4`, `vm-5`
- **Zero-padded**: `"test-{001..050}"` expands to `test-001`, `test-002`, ..., `test-050`
- **Mixed with individual**: Patterns and individual hosts can be mixed in the same configuration
- **Quoted keys**: Pattern keys must be quoted in YAML to handle special characters

### Benefits
- **Efficiency**: Configure many hosts with same device in one line
- **Maintainability**: Easy to update device for entire groups
- **Flexibility**: Mix patterns with individual host specifications
- **Scalability**: Perfect for large VM deployments with consistent device layouts

## Mixed Hosts File Format

The `mixed-hosts.txt` file contains hostnames for both OpenShift VMs and bare metal servers. You can now use host patterns in this file!

```bash
# OpenShift Virtual Machines (will be accessed via virtctl ssh)
# Use patterns for efficient configuration of multiple VMs
vm-{1..5}
worker-{1..3}
app-server-{1..2}

# Bare Metal Servers (will be accessed via direct SSH)
# You can use patterns for bare metal servers too, or list them individually
fed{1..3}
baremetal-server-{1..2}
storage-node-{1..2}

# Individual hosts (for special cases)
special-server.my.node.com
```

### Host File Pattern Examples:
- `vm-{1..10}` → `vm-1`, `vm-2`, ..., `vm-10`
- `server-{001..050}` → `server-001`, `server-002`, ..., `server-050` (zero-padded)
- `worker-{1..5}` → `worker-1`, `worker-2`, `worker-3`, `worker-4`, `worker-5`
- `test-{a..c}` → `test-a`, `test-b`, `test-c` (letter ranges)

### Benefits of Host Patterns in Files:
- **Efficiency**: Define many hosts with a single pattern line
- **Scalability**: Easy to scale from 5 VMs to 50 VMs by changing `{1..5}` to `{1..50}`
- **Maintainability**: Update host ranges without listing every individual host
- **Flexibility**: Mix patterns with individual hostnames in the same file
- **Consistency**: Ensures consistent naming across your infrastructure

### Auto-Detection Logic
- **VMs**: Script checks if hostname exists as VM/VMI in OpenShift namespace
- **Bare Metal**: Hosts not found as VMs are treated as regular servers
- **Connection Method**: Automatically uses `virtctl ssh` for VMs, `ssh` for servers
- **Backward Compatibility**: Existing configurations continue to work

## Connection Modes

### Auto-detection Mode (Default)
- **Command**: `./fio-tests.sh -c config.yaml`
- **Behavior**: Automatically detects VM vs regular host and uses appropriate connection method
- **Requirements**: Both virtctl/oc and SSH access
- **Best for**: Mixed environments

### SSH Only Mode
- **Command**: `./fio-tests.sh --ssh-only -c config.yaml`
- **Behavior**: Forces SSH for all hosts
- **Requirements**: Direct SSH access to all hosts
- **Best for**: Regular servers, cloud instances, bare metal servers
- **Namespace**: Not required - script shows "N/A (SSH-only mode)" for bare metal environments

### Virtctl Only Mode
- **Command**: `./fio-tests.sh --virtctl-only -c config.yaml`
- **Behavior**: Forces virtctl for all hosts
- **Requirements**: virtctl/oc access to all VMs
- **Best for**: OpenShift/Kubernetes VMs

## Configuration Parameters

### VM/Host Configuration
- `vm.hosts`: Space-separated list of hostnames
- `vm.host_pattern`: Bash expansion pattern (e.g., "vm-{1..100}")
- `vm.host_file`: Path to file containing hostnames
- `vm.host_labels`: Kubernetes label selector
- `vm.namespace`: OpenShift/Kubernetes namespace (for VMs only - not needed for SSH-only mode)

### Storage Configuration
- `storage.devices`: **MANDATORY** - Per-host device configuration (no global fallback for safety)
  - `storage.devices.hostname`: Device name for each host (e.g., "sda", "nvme0n1")
  - **CRITICAL**: Each host MUST have a device specified - no fallback devices allowed for safety
- `storage.mount_point`: Mount point for test filesystem
- `storage.filesystem`: Filesystem type to create

**Device Configuration Examples:**
```yaml
# Per-host device configuration (MANDATORY - no global fallback for safety)
storage:
  devices:
    server1: "sda"      # /dev/sda - different device for server1
    server2: "sdb"      # /dev/sdb - explicit device for server2
    server3: "nvme0n1"  # /dev/nvme0n1 - NVMe device for server3
    server4: "sdb"      # /dev/sdb - explicit device for server4
    server5: "sdc"      # /dev/sdc - different device for server5
```

### FIO Test Configuration
- `fio.test_size`: Test file size (e.g., "1G", "500M")
- `fio.runtime`: Test duration in seconds
- `fio.block_sizes`: Space-separated block sizes (e.g., "4k 8k 128k")
- `fio.io_patterns`: Space-separated I/O patterns (e.g., "read write randread")
- `fio.numjobs`: Number of parallel jobs
- `fio.iodepth`: I/O depth
- `fio.direct_io`: Direct I/O flag (0 or 1)

### Output Configuration
- `output.directory`: Directory to store results on remote hosts
- `output.format`: FIO output format (e.g., "json+", "normal")

## Usage Examples

### Basic SSH Testing
```bash
# Test 5 servers via SSH
./fio-tests.sh --ssh-only -c fio-config-ssh-only.yaml
```

### Basic VM Testing
```bash
# Test 5 VMs via virtctl
./fio-tests.sh --virtctl-only -c fio-config-virtctl-only.yaml
```

### Mixed Environment Testing
```bash
# Auto-detect connection method for each host
./fio-tests.sh -c fio-config-mixed-hosts.yaml
```

### Dry Run (Test Configuration)
```bash
# Validate configuration without executing tests
./fio-tests.sh --ssh-only -c fio-config-ssh-only.yaml --dry-run
```

### Verbose Output
```bash
# Show detailed execution information
./fio-tests.sh --virtctl-only -c fio-config-virtctl-only.yaml -v
```

### Debug Configuration
```bash
# Show detailed configuration parsing information
./fio-tests.sh -c fio-config-examples.yaml --debug
```

## Prerequisites

### For SSH Only Mode
- SSH access configured to all target hosts
- `ssh` command available
- Root access on target hosts

### For Virtctl Only Mode
- `virtctl` and `oc` commands installed and configured
- Logged into OpenShift/Kubernetes cluster
- VMs exist and are running in specified namespace
- Root access on target VMs

### For Auto-detection Mode
- All prerequisites from both SSH and virtctl modes
- Mixed-hosts.txt file (if using host_file method)

## Troubleshooting

### Common Issues
1. **"No hosts specified"**: Check that at least one host selection method is configured
2. **"VM not found"**: Verify VM names and namespace in OpenShift/Kubernetes
3. **"SSH connection failed"**: Check SSH configuration and host accessibility
4. **"virtctl command not found"**: Install and configure virtctl/oc tools

### Validation
Use `--dry-run` to validate configuration before executing tests:
```bash
./fio-tests.sh --ssh-only -c your-config.yaml --dry-run
```

### Debug Information
Use `--debug` to see detailed configuration parsing:
```bash
./fio-tests.sh -c your-config.yaml --debug
```
