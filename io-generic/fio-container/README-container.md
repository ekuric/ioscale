# fio-tests.py Container

Run FIO performance tests against remote VMs from a container.
Configuration can be provided by mounting a YAML config file or by setting
environment variables.

## Building

```bash
cd fio-container
podman build -t quay.io/ekuric/fio-benchmark:latest .
```

Override the virtctl version at build time:

```bash
podman build --build-arg VIRTCTL_VERSION=v1.8.0 -t quay.io/ekuric/fio-benchmark:latest .
```

## Three Usage Modes


### Mode 1: Config file (recommended)

Mount your `fio-config.yaml`. Everything comes from the file. Best for complex
or heavily customised setups.

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./fio-config.yaml:/work/fio-config.yaml \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

Or point to a baked-in example config ( however necessary to edit it for test machine names ):

```bash
podman run --rm --init --pids-limit=-1 \
  -e CONFIG=/work/examples/example-simple.yaml \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

### Mode 2: Linux env vars (CI/CD)

No config file needed. Set Linux host and FIO variables.

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="vm-{1..10}" \
  -e DEVICES="vm-{1..10}=vdc" \
  -e NAMESPACE="default" \
  -e TEST_SIZE="10G" \
  -e RUNTIME=600 \
  -e BLOCK_SIZES="4k 8k 128k 1024k" \
  -e IO_PATTERNS="read write randread randwrite" \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

### Mode 3: Windows env vars

Set `WIN_HOSTS` or `WIN_HOST_PATTERN` plus `WIN_DEVICES`. Can be used alone
(Windows-only) or combined with Linux env vars (mixed).

**Windows-only:**

```bash
podman run --rm --init --pids-limit=-1 \
  -e WIN_HOST_PATTERN="win-vm-{1..5}" \
  -e WIN_DEVICES="win-vm-{1..5}=1" \
  -e NAMESPACE="default" \
  -e WIN_TEST_SIZE="10GB" \
  -e WIN_RUNTIME=600 \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

**Mixed Linux + Windows:**

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="linux-vm-{1..5}" \
  -e DEVICES="linux-vm-{1..5}=vdc" \
  -e WIN_HOST_PATTERN="win-vm-{1..5}" \
  -e WIN_DEVICES="win-vm-{1..5}=1" \
  -e NAMESPACE="default" \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

**How it works:** If a config file exists at the `CONFIG` path, it is used
as-is. If no file is found, the entrypoint generates one from env vars.
No merging or patching -- it is one or the other.

**Important:** Mount `/work/results` to a host directory to keep results after
the container exits. Without this mount and with `--rm`, results are lost.

## Environment Variables

Env vars are used when no config file is mounted (Modes 2 and 3).
`CONFIG`, `KUBEADMIN_PASSWORD`, and `API_URL` apply to all modes.

| Variable | Default | Description |
|---|---|---|
| `CONFIG` | `/work/fio-config.yaml` | Path to the YAML config file inside the container |
| `HOSTS` | -- | Space-separated host names (pick one of HOSTS / HOST_PATTERN / HOST_LABELS) |
| `HOST_PATTERN` | -- | Brace-expansion pattern, e.g. `vm-{1..10}` |
| `HOST_LABELS` | -- | OCP label selector for VMs |
| `NAMESPACE` | `default` | OpenShift namespace |
| `DEVICES` | **required** | Device mapping: `"pattern=device"`, comma-separated for multiple (e.g. `"vm-{1..10}=vdc,host1=sdb"`) |
| `MOUNT_POINT` | `/root/tests/data` | Mount point for the test filesystem |
| `FILESYSTEM` | `xfs` | Filesystem type to create |
| `PERSISTENT` | -- | Set to `true` for persistent mounts via /etc/fstab |
| `TEST_SIZE` | `1G` | FIO test file size |
| `RUNTIME` | `300` | Test runtime in seconds |
| `BLOCK_SIZES` | `4k 8k 128k` | Space-separated block sizes (e.g. `"4k 8k 128k 1024k"`) |
| `IO_PATTERNS` | `read write randread randwrite` | Space-separated I/O patterns |
| `NUMJOBS` | `4` | Number of parallel FIO jobs |
| `IODEPTH` | `16` | I/O depth |
| `DIRECT_IO` | `1` | Direct I/O (1=bypass page cache) |
| `FIO_INSTALLED` | `false` | Golden Linux image with FIO baked in (`true` = skip package check/install; `false` = install if missing) |
| `RATE_IOPS` | -- | Optional IOPS rate limit |
| `OUTPUT_DIR` | `/root/fio-results` | Result directory on remote hosts |
| `OUTPUT_FORMAT` | `json+` | FIO output format |
| `DESCRIPTION` | -- | Test run description (included in results dir name) |
| `MIGRATE_WORKLOADS` | -- | Space-separated workloads to trigger VM migration (e.g. `"write randwrite"`) |
| `MIGRATE_INTERVAL` | `0` | Seconds between migrations (0 = parallel) |
| `RETRY_INTERVAL` | `30` | Retry interval in seconds |
| `MAX_RETRIES` | `10` | Maximum retry attempts |
| `MONITOR_INTERVAL` | `60` | Task monitor interval in seconds |
| `KUBEADMIN_PASSWORD` | -- | If set, runs `oc login` before the test |
| `API_URL` | `https://api.ocp.example.com:6443` | Cluster API URL for `oc login` |

### Windows env vars (Mode 3)

Set `WIN_HOSTS` or `WIN_HOST_PATTERN` to activate the Windows section.

| Variable | Default | Description |
|---|---|---|
| `WIN_HOSTS` | -- | Space-separated Windows VM names |
| `WIN_HOST_PATTERN` | -- | Brace-expansion pattern, e.g. `win-vm-{1..10}` |
| `WIN_DEVICES` | **required** | Device mapping with Disk IDs: `"win-vm-{1..10}=1"` |
| `WIN_MOUNT_POINT` | `d\:/fio/data` | Windows mount point for test data |
| `WIN_RUN_DIR` | `d:/fio` | Directory containing fio.exe |
| `WIN_TEST_SIZE` | `10GB` | FIO test file size |
| `WIN_RUNTIME` | `600` | Test runtime in seconds |
| `WIN_BLOCK_SIZES` | `4k 8k 128k 1024k` | Space-separated block sizes |
| `WIN_IO_PATTERNS` | `randread randwrite read write` | Space-separated I/O patterns |
| `WIN_NUMJOBS` | `8` | Number of parallel FIO jobs |
| `WIN_IODEPTH` | `16` | I/O depth |
| `WIN_DIRECT_IO` | `1` | Direct I/O (1=yes) |
| `WIN_RATE_IOPS` | -- | Optional IOPS rate limit |
| `WIN_OUTPUT_DIR` | `d:/fio/results` | Result directory on Windows hosts |
| `WIN_OUTPUT_FORMAT` | `json+` | FIO output format |

### Linux golden image (`fio_installed`)

Use `fio_installed: true` in the `fio:` section of your YAML config (or
`FIO_INSTALLED=true` when generating config from env vars) when Linux VMs were
built from a golden qcow2 with FIO packages pre-installed — for example using
`../imagecreator.sh` in this repo.

| Value | Behavior on Linux hosts |
|---|---|
| `false` (default) | Run `dnf install -y fio xfsprogs util-linux` if `fio` is not already present |
| `true` | Skip FIO package check/install on Linux (golden image) |

YAML example:

```yaml
fio:
  test_size: "5G"
  runtime: 300
  block_sizes: "4k 8k 128k"
  io_patterns: "randread randwrite"
  fio_installed: true
```

Env var example (with golden-image VMs):

```bash
-e FIO_INSTALLED=true \
```

Jenkins: set the `FIO_INSTALLED` build parameter to `true` when deploying VMs
from the fio golden image (`Jenkinsfile`, `Jenkinsfile-linux`).

## Baked-in Example Configs

The container includes example configs at `/work/examples/`:

| File | Description |
|---|---|
| `example-simple.yaml` | Minimal 3-host Linux test |
| `example-linux-only.yaml` | Linux-only with description and migration |
| `example-windows-only.yaml` | Windows-only with virtctl |
| `example-mixed-linux-windows.yaml` | Linux + Windows in one run |
| `example-with-migration.yaml` | Linux with VM migration during tests |

Copy one out to use as a starting point:

```bash
podman run --rm --init --pids-limit=-1 quay.io/ekuric/fio-benchmark:latest cat /work/examples/example-simple.yaml > my-config.yaml
```

## fio-tests.py CLI Parameters

The entrypoint passes `--virtctl-only` and `--yes-i-mean-it` automatically. Any
extra arguments after the image name are forwarded to `fio-tests.py`.

### `-v`, `--verbose`

Enable verbose output.

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./fio-config.yaml:/work/fio-config.yaml \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest \
  --verbose
```

### `--dry-run`

Validate configuration and show what would be done without executing.

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="vm-{1..5}" \
  -e DEVICES="vm-{1..5}=vdc" \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest \
  --dry-run
```

### `--ssh-only`

Force plain SSH for all hosts (overrides the default `--virtctl-only`).

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./fio-config.yaml:/work/fio-config.yaml \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  quay.io/ekuric/fio-benchmark:latest \
  --ssh-only
```

### `--prepare-machine`

Only install FIO dependencies on machines, skip all testing. When
`fio_installed: true` on Linux-only runs, this step is skipped entirely.

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="vm-{1..10}" \
  -e DEVICES="vm-{1..10}=vdc" \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest \
  --prepare-machine
```

### `--copy-results`

Only copy results from hosts (skip installation, preparation, and testing).

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./fio-config.yaml:/work/fio-config.yaml \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest \
  --copy-results
```

### `--skip-connectivity-test`

Skip connectivity test and proceed directly to command execution.

### `--interval INTERVAL`

Override retry interval in seconds.

### `--max-retries MAX_RETRIES`

Override maximum number of retry attempts.

### `--monitor-interval MONITOR_INTERVAL`

Override task monitor interval in seconds.

### `--debug`

Show detailed configuration parsing debug information.

## Usage Examples

### Config file only

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./fio-config.yaml:/work/fio-config.yaml \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

### Linux-only (env vars, all parameters)

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="vm-{1..10}" \
  -e DEVICES="vm-{1..10}=vdc" \
  -e NAMESPACE="default" \
  -e DESCRIPTION="linux perf test" \
  -e TEST_SIZE="1G" \
  -e RUNTIME=300 \
  -e BLOCK_SIZES="4k 8k 128k" \
  -e IO_PATTERNS="read write randread randwrite" \
  -e NUMJOBS=4 \
  -e IODEPTH=16 \
  -e DIRECT_IO=1 \
  -e MOUNT_POINT="/root/tests/data" \
  -e FILESYSTEM="xfs" \
  -e PERSISTENT="" \
  -e OUTPUT_DIR="/root/fio-results" \
  -e OUTPUT_FORMAT="json+" \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

### Linux golden image (FIO pre-installed)

Use this when VMs were created from a golden qcow2 built with
`containers-bench/imagecreator.sh`:

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="fio-vm-{1..100}" \
  -e DEVICES="fio-vm-{1..100}=vdb" \
  -e FIO_INSTALLED=true \
  -e TEST_SIZE="16G" \
  -e RUNTIME=300 \
  -e BLOCK_SIZES="4k 8k 128k" \
  -e IO_PATTERNS="randread randwrite" \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

Or mount a config file with `fio_installed: true` under the `fio:` section.

### Dry run with verbose output

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./fio-config.yaml:/work/fio-config.yaml \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest \
  --dry-run --verbose
```

### Multiple device patterns

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOSTS="vm-1 vm-2 bare1" \
  -e DEVICES="vm-1=vdc,vm-2=vdc,bare1=sdb" \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

### With VM migration during tests

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="vm-{1..20}" \
  -e DEVICES="vm-{1..20}=vdc" \
  -e RUNTIME=600 \
  -e MIGRATE_WORKLOADS="write randwrite" \
  -e MIGRATE_INTERVAL=2 \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

### Windows-only (env vars, all parameters)

```bash
podman run --rm --init --pids-limit=-1 \
  -e WIN_HOST_PATTERN="win-vm-{1..5}" \
  -e WIN_DEVICES="win-vm-{1..5}=1" \
  -e NAMESPACE="default" \
  -e DESCRIPTION="windows perf test" \
  -e WIN_RUN_DIR="d:/fio" \
  -e WIN_TEST_SIZE="10GB" \
  -e WIN_RUNTIME=600 \
  -e WIN_BLOCK_SIZES="4k 8k 128k 1024k" \
  -e WIN_IO_PATTERNS="randread randwrite read write" \
  -e WIN_NUMJOBS=8 \
  -e WIN_IODEPTH=16 \
  -e WIN_DIRECT_IO=1 \
  -e WIN_MOUNT_POINT="d\:/fio/data" \
  -e WIN_OUTPUT_DIR="d:/fio/results" \
  -e WIN_OUTPUT_FORMAT="json+" \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

### Mixed Linux + Windows (env vars, all parameters)

```bash
podman run --rm --init --pids-limit=-1 \
  -e NAMESPACE="default" \
  -e DESCRIPTION="mixed linux-windows perf test" \
  -e HOST_PATTERN="linux-vm-{1..5}" \
  -e DEVICES="linux-vm-{1..5}=vdc" \
  -e TEST_SIZE="1G" \
  -e RUNTIME=300 \
  -e BLOCK_SIZES="4k 8k 128k" \
  -e IO_PATTERNS="read write randread randwrite" \
  -e NUMJOBS=4 \
  -e IODEPTH=16 \
  -e DIRECT_IO=1 \
  -e MOUNT_POINT="/root/tests/data" \
  -e FILESYSTEM="xfs" \
  -e OUTPUT_DIR="/root/fio-results" \
  -e OUTPUT_FORMAT="json+" \
  -e WIN_HOST_PATTERN="win-vm-{1..5}" \
  -e WIN_DEVICES="win-vm-{1..5}=1" \
  -e WIN_RUN_DIR="d:/fio" \
  -e WIN_TEST_SIZE="10GB" \
  -e WIN_RUNTIME=600 \
  -e WIN_BLOCK_SIZES="4k 8k 128k 1024k" \
  -e WIN_IO_PATTERNS="randread randwrite read write" \
  -e WIN_NUMJOBS=8 \
  -e WIN_IODEPTH=16 \
  -e WIN_DIRECT_IO=1 \
  -e WIN_MOUNT_POINT="d\:/fio/data" \
  -e WIN_OUTPUT_DIR="d:/fio/results" \
  -e WIN_OUTPUT_FORMAT="json+" \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

### With oc login

```bash
podman run --rm --init --pids-limit=-1 \
  -e KUBEADMIN_PASSWORD="my-password" \
  -e API_URL="https://api.mycluster.example.com:6443" \
  -v ./fio-config.yaml:/work/fio-config.yaml \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

## SSH Key

`virtctl ssh` uses `/root/.ssh/id_rsa` inside the container by default. Mount
your private key:

```bash
-v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro
```

If your key is at a non-standard path, mount it to the expected location:

```bash
-v /home/user/.ssh/my-key:/root/.ssh/id_rsa:ro
```

Or mount the entire `.ssh` directory:

```bash
-v /root/.ssh:/root/.ssh:ro
```

## Container Layout

```
/work/
  fio-tests.py
  entrypoint.sh
  fio-config.yaml         (mounted by user, or generated from env vars)
  examples/
    example-simple.yaml
    example-linux-only.yaml
    example-windows-only.yaml
    example-mixed-linux-windows.yaml
    example-with-migration.yaml
  results/                (output directory)
```

## Timeouts (config file only)

All timeouts have sensible defaults and are optional. To override, add a
`timeouts` section to your config file. Partial overrides work -- only specify
values you want to change. Not available in env-var mode.

| Key | Default | Description |
|---|---|---|
| `default` | `300` | General SSH command timeout (seconds) |
| `quick` | `60` | Short commands: mkdir, package check, etc. |
| `process_check` | `30` | Checking if a remote process is still running |
| `connectivity` | `10` | Initial SSH connectivity test per host |
| `runtime_buffer` | `300` | Extra seconds added to FIO runtime for test timeout |
| `nohup_setup` | `60` | Setting up nohup background FIO on remote host |
| `scp` | `300` | File copy (scp/virtctl scp) timeout |
| `dataset_buffer` | `60` | Extra seconds for FIO dataset pre-write to finish |
| `check_interval` | `10` | Polling interval when waiting for background tasks |
| `migration` | `600` | VM live migration timeout per host |

Example in `fio-config.yaml`:

```yaml
timeouts:
  default: 600
  scp: 600
  migration: 1200
```

## VM Live Migration During Tests

The script can live-migrate OpenShift/KubeVirt VMs **while FIO tests are
running** to measure the impact of VM migration on I/O performance. It uses
`virtctl migrate` to trigger the migrations.

### Configuration

```yaml
migrate:
  workloads: "write randwrite"   # I/O patterns that trigger migration
  interval: 0                    # 0 = parallel, >0 = sequential with delay (seconds)
```

- `workloads` -- space-separated list of I/O patterns. Migration only happens
  during tests matching these patterns. Empty string disables migration.
- `interval` -- migration strategy:
  - `0` -- migrate all VMs simultaneously (parallel)
  - `>0` -- migrate VMs one at a time with this many seconds between each
    (recommended for large VM counts to avoid overloading CNV)

### When Migration Triggers

Migration occurs at the **midpoint** of the FIO test runtime. For a 600-second
test, migrations start at 300 seconds. This captures the performance impact
mid-test while FIO is in steady state.

The flow for each test (block_size x io_pattern):

1. Start FIO on all hosts (background)
2. If the current I/O pattern is in `migrate.workloads`:
   - Wait until the midpoint of the test runtime
   - Migrate all VMs (parallel or sequential per `interval`)
3. Wait for FIO to complete on all hosts
4. Proceed to the next test combination

### Migration Strategies

**Parallel (`interval: 0`):**

All VMs are migrated simultaneously using a thread pool. Fast but can strain
the CNV infrastructure when migrating many VMs at once.

**Sequential (`interval: 2`):**

VMs are migrated one at a time with a delay between each. Safer for large
deployments (e.g. 50+ VMs) as it avoids overwhelming the cluster.

### Retry Logic

Failed migrations get one automatic retry:

1. First pass: attempt all VMs
2. Collect failures
3. Second pass: retry only the failed VMs
4. If retries also fail, log the error (test continues, results still collected)

### Example

With `workloads: "write randwrite"`, `interval: 2`, `runtime: 600`, 10 VMs:

```
write test starts on all 10 VMs
  ├── Wait 300s (midpoint)
  ├── Migrate vm-1 → 2s → vm-2 → 2s → ... → vm-10
  │   (failures retried after first pass)
  └── Wait for FIO to finish

randread test starts (no migration -- not in workloads)

randwrite test starts on all 10 VMs
  ├── Wait 300s (midpoint)
  ├── Migrate vm-1 → 2s → vm-2 → 2s → ... → vm-10
  └── Wait for FIO to finish
```

### Env var mode

When using env vars (no config file), set:

```bash
-e MIGRATE_WORKLOADS="write randwrite" \
-e MIGRATE_INTERVAL=2
```

### Notes on migration

- Only VMs are migrated -- baremetal/SSH hosts are skipped automatically
- Requires `--virtctl-only` mode (default in container) and a valid namespace
- Migration timeout per VM is configurable via `timeouts.migration` (default 600s)
- Migration does not stop the FIO test -- it runs concurrently
- Results from migrated VMs are still collected normally after the test

## Virtual Machine Requirements

### Linux VMs

- Any yum/dnf-based distribution (Fedora or CentOS recommended)
- A separate data disk for testing, added to the VM at creation time
- Passwordless SSH access enabled -- the public SSH key must be baked into the VM image at creation time
- FIO dependencies (`fio`, `xfsprogs`, `util-linux`):
  - **Default (`fio_installed: false`)**: installed automatically via dnf if missing
  - **Golden image (`fio_installed: true`)**: must be pre-installed in the VM image (see `../imagecreator.sh`)

### Windows VMs

- FIO must be pre-installed at `C:\tools\fio` (the script copies it to the test drive but does not install it)
- A separate data disk for testing, added to the VM at creation time
- Passwordless SSH access enabled (OpenSSH server with public key authentication) -- the public SSH key must be baked into the VM image at creation time

### All VMs

- Must be accessible via `virtctl ssh` (OpenShift VMs) or plain `ssh` (baremetal/KVM)
- The SSH key used by the container must match the public key in the VM image
- Mount the matching private key into the container: `-v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro`

## Notes

- If a config file is mounted, it is used as-is. If not, env vars generate one. No merging.
- Env-var mode supports Linux-only, Windows-only, or mixed Linux+Windows testing.
- Use `WIN_*` prefixed env vars for Windows hosts (set `WIN_HOSTS` or `WIN_HOST_PATTERN` to activate).
- `--privileged` is needed for the virtctl SSH proxy to work.
- `/root/.kube/config` mount gives the container access to the OCP cluster.
- Mount your SSH private key to `/root/.ssh/id_rsa` so `virtctl ssh` can authenticate.
- `--yes-i-mean-it` is passed automatically to skip the device formatting confirmation prompt.
- Set `fio_installed: true` (or `FIO_INSTALLED=true`) when using golden Linux images with FIO pre-baked; leave `false` for stock cloud images.
- The entrypoint always prints the config before running, so you can see exactly what values are used.
- Example configs are available inside the container at `/work/examples/`.
