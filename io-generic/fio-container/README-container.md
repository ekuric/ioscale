# fio-tests.py Container

Run FIO performance tests against remote VMs from a container.
Configuration can be provided by mounting a YAML config file or by setting
environment variables.

Implementation is modular: `fio-tests.py` is a thin CLI shim; logic lives in
`fio_tests/` (`cli`, `config`, `executor`, `fio_run`, `storage`, …).

## Building

Build from this directory (`ioscale_files/io-generic/fio-container`):

```bash
cd ioscale_files/io-generic/fio-container
podman build -t quay.io/ekuric/fio-benchmark:latest .
```

`bpftrace`/cluster tools come from the base image (`quay.io/ekuric/benchmark-base:latest`).
This Dockerfile only copies the FIO harness (`fio_tests/`, shim, examples, entrypoint).

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
| `DEVICES` | **required** unless `USE_TESTDIR=true` | Device mapping: `"pattern=device"`, comma-separated for multiple (e.g. `"vm-{1..10}=vdc,host1=sdb"`). Omit when using OS-disk `--testdir` mode. Still required for `--raw-disk`. |
| `MOUNT_POINT` | `/root/tests/data` | Mount point for the test filesystem (ignored for `--testdir` / `--raw-disk`) |
| `FILESYSTEM` | `xfs` | Filesystem type to create (ignored for `--testdir` / `--raw-disk`) |
| `PERSISTENT` | -- | Set to `true` for persistent mounts via /etc/fstab |
| `TEST_SIZE` | `1G` | FIO test file size |
| `RUNTIME` | `300` | Test runtime in seconds |
| `BLOCK_SIZES` | `4k 8k 128k` | Space-separated block sizes (e.g. `"4k 8k 128k 1024k"`) |
| `IO_PATTERNS` | `read write randread randwrite` | Space-separated I/O patterns |
| `NUMJOBS` | `4` | Number of parallel FIO jobs |
| `IODEPTH` | `16` | I/O depth |
| `DIRECT_IO` | `1` | Direct I/O (1=bypass page cache) |
| `FSYNC` | -- | Optional FIO `--fsync=N` (empty/unset = disabled; e.g. `1` or `32`) |
| `FIO_INSTALLED` | `false` | Golden Linux image with FIO baked in (`true` = skip package check/install; `false` = install if missing) |
| `RATE_IOPS` | -- | Optional IOPS rate limit |
| `OUTPUT_DIR` | `/root/fio-results` | Ignored for remote JSON — always forced to `/root/fio-results` on the OS disk |
| `OUTPUT_FORMAT` | `json+` | FIO output format |
| `DESCRIPTION` | -- | Test run description (included in results dir name) |
| `USE_TESTDIR` | `false` | Set to `true` to add `--testdir` (FIO on OS disk: Linux `/root/testdir`, Windows `c:/testdir`). YAML/env data-disk settings are ignored — see [OS-disk load](#os-disk-load---testdir). Mutually exclusive with `USE_RAW_DISK`. |
| `USE_RAW_DISK` | `false` | Set to `true` to add `--raw-disk` (FIO against the block device with no filesystem). Requires `DEVICES` / `WIN_DEVICES` (unless `CREATE_VMS`). See [Raw-disk load](#raw-disk-load---raw-disk). Mutually exclusive with `USE_TESTDIR`. |
| `CREATE_VMS` | `false` | Provision Linux/Windows VMs before the test (see [On-demand VM provision](#on-demand-vm-provision---create-vms)). When `true`, leave `HOST_PATTERN` / `WIN_HOST_PATTERN` empty — they are ignored. |
| `CLEANUP` | `false` | Delete managed VMs after the test (default: keep). Next `CREATE_VMS` run wipes prior leftovers for **the same OS only** (`fio-container.ioscale/os=linux` or `windows`) so fio-linux and fio-windows can share a namespace. |
| `VMS` | `0` | Number of Linux VMs to create when `CREATE_VMS=true` |
| `WIN_VMS` | `0` | Number of Windows VMs to create when `CREATE_VMS=true` |
| `VM_PREFIX` | `fio-linux` | Linux VM name prefix → `fio-linux-1..N` |
| `WIN_VM_PREFIX` | `fio-win` | Windows VM name prefix → `fio-win-1..M` |
| `VM_CPU` / `VM_MEMORY` | `2` / `4Gi` | Linux VM size |
| `WIN_VM_CPU` / `WIN_VM_MEMORY` | same as Linux | Windows VM size |
| `VM_IMAGE` | `quay.io/containerdisks/fedora:41` | Linux containerdisk (registry) |
| `WIN_VM_IMAGE` | -- | Windows image HTTP URL (required if `WIN_VMS>0`) |
| `STORAGE_CLASS` | -- | **Required** when `CREATE_VMS=true` |
| `ACCESS_MODE` | `ReadWriteMany` | PVC access mode for provisioned volumes |
| `ROOT_VOLUME_SIZE` | `20Gi` | Linux root PVC size |
| `WIN_ROOT_VOLUME_SIZE` | `76Gi` | Windows root PVC size |
| `DATA_DISK_SIZE` | `40Gi` | Blank data disk size on provisioned VMs (skipped for `--testdir`) |
| `WIN_DATA_DISK_SIZE` | same as `DATA_DISK_SIZE` | Windows blank data disk size |
| `SSH_KEY_PATH` | `/tmp/fio-ssh-<runid>` | In-container directory for ephemeral keypair |
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
| `WIN_DEVICES` | required unless `USE_TESTDIR=true` | Device mapping with Disk IDs: `"win-vm-{1..10}=1"`. Omit when using OS-disk `--testdir` mode. Still required for `--raw-disk`. |
| `WIN_MOUNT_POINT` | `d\:/fio/data` | Windows mount point for test data (ignored when `USE_TESTDIR=true` or `USE_RAW_DISK=true`) |
| `WIN_RUN_DIR` | `d:/fio` | Directory containing fio.exe (ignored for `--testdir` / `--raw-disk`; uses `c:/tools/fio`) |
| `WIN_TEST_SIZE` | `10GB` | FIO test file size |
| `WIN_RUNTIME` | `600` | Test runtime in seconds |
| `WIN_BLOCK_SIZES` | `4k 8k 128k 1024k` | Space-separated block sizes |
| `WIN_IO_PATTERNS` | `randread randwrite read write` | Space-separated I/O patterns |
| `WIN_NUMJOBS` | `8` | Number of parallel FIO jobs |
| `WIN_IODEPTH` | `16` | I/O depth |
| `WIN_DIRECT_IO` | `1` | Direct I/O (1=yes) |
| `WIN_FSYNC` | -- | Optional FIO `--fsync=N` (empty/unset = disabled; e.g. `1` or `32`) |
| `WIN_RATE_IOPS` | -- | Optional IOPS rate limit |
| `WIN_OUTPUT_DIR` | `c:/fio-results` | Ignored for remote JSON — always forced to `c:/fio-results` on the OS disk |
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

## On-demand VM provision (`--create-vms`)

Create Linux and/or Windows VMs **in the same run** as the FIO test (no
`multivm.sh` / pre-created hosts). Set `CREATE_VMS=true` (or `--create-vms`),
`VMS` / `WIN_VMS`, and `STORAGE_CLASS`. Leave `HOST_PATTERN` / `WIN_HOST_PATTERN`
empty — they are ignored when create is on.

### Prerequisites

- Namespace **must already exist** (`oc create namespace <ns>` / `oc new-project`).
  The harness does not create it.
- Bastion kubeconfig mounted into the container (`-v /root/.kube:/root/.kube:ro,Z`).
- `STORAGE_CLASS` set to a StorageClass that supports the volume mode you need.
- **Windows image**: OpenSSH + FIO at `c:\tools\fio` (Cloudbase-Init capable).
  SSH keys are injected via `#ps1_sysnative` userdata in a Secret
  (`fio-win-cloudinit-<runid>`), overwriting any golden-image `authorized_keys`.

### Phase order (strict)

Guest work never starts until VMs are Ready and SSH works:

| Phase | What |
|---|---|
| **1** | Generate ephemeral SSH key → wipe prior **managed** leftovers → create source DV + VMs → wait Ready + SSH |
| **2** | Create dirs / prepare storage (testdir \| raw \| FS format+mount) |
| **3** | Install FIO packages if needed |
| **4** | Write dataset |
| **5** | Run FIO performance tests → collect results → optional `--cleanup` |

### SSH keys (container + bastion)

| Location | Path | Lifetime |
|---|---|---|
| In-container (used by virtctl in the run) | `/tmp/fio-ssh-<runid>/id_ed25519` | Removed when the container exits |
| Bastion mirror (for inspection) | `/tmp/fio-ssh/fio-ssh-<runid>/id_ed25519` | Kept if VMs are kept; removed with `--cleanup` |

Mount **only** the subdirectory (SELinux forbids relabeling `/tmp`):

```bash
mkdir -p /tmp/fio-ssh
# ...
-v /tmp/fio-ssh:/host-tmp:Z \
```

```bash
# After create (VMs kept) — from bastion
ls /tmp/fio-ssh/
virtctl ssh fedora@vmi/fio-linux-1 -n <ns> -i /tmp/fio-ssh/fio-ssh-<runid>/id_ed25519
virtctl ssh Administrator@vmi/fio-win-1 -n <ns> -i /tmp/fio-ssh/fio-ssh-<runid>/id_ed25519
```

### Naming, disks, generated YAML

| Item | Default / behavior |
|---|---|
| Linux names | `{VM_PREFIX}-1..N` (default `fio-linux-1..N`) |
| Windows names | `{WIN_VM_PREFIX}-1..M` (default `fio-win-1..M`) |
| Device maps | Auto-filled after provision: Linux `vdb`, Windows Disk `1` |
| Data disk | Attached unless `USE_TESTDIR=true` |
| Labels | `fio-container.ioscale/managed=true` + `fio-container.ioscale/os=linux\|windows` (wipe/cleanup scoped by OS) |

Env-generated `storage` / `storage_win` only include fields the disk mode uses:

| Mode | Generated storage block |
|---|---|
| **testdir** | omitted (no mount/fs/devices in YAML) |
| **raw-disk** | `devices` only (`{}` until provision fills them) |
| **FS disk** | `devices` + `mount_point` + `filesystem` (+ Windows `mount_point`) |

### Cleanup

| Moment | Behavior |
|---|---|
| Start of create | Always wipe prior **managed** VMs/DVs/Secrets |
| End without `--cleanup` | Keep VMs + bastion SSH key mirror |
| End with `--cleanup` | Delete managed VMs/DVs/Secrets + bastion key dir |
| In-container SSH key | Always removed |

Templates live under `/work/templates/` (adapted from `fio-vmlinux` / `fio-vmwindows`).

### Create-VM + test recipes

Common mounts for all recipes below (adjust paths as needed):

```bash
COMMON_MOUNTS=(
  -v /root/fio-results:/work/results:Z
  -v /root/.kube:/root/.kube:ro,Z
  -v /tmp/fio-ssh:/host-tmp:Z
)
mkdir -p /tmp/fio-ssh /root/fio-results
```

#### 1) Linux — create 3 VMs + OS-disk testdir

No data disk. FIO under `/root/testdir`; JSON under `/root/fio-results`.

```bash
podman run --rm --init --pids-limit=-1 --privileged \
  -e CREATE_VMS=true \
  -e VMS=3 \
  -e VM_PREFIX=fio-linux \
  -e VM_CPU=2 \
  -e VM_MEMORY=4Gi \
  -e VM_IMAGE=quay.io/containerdisks/fedora:41 \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e ROOT_VOLUME_SIZE=20Gi \
  -e USE_TESTDIR=true \
  -e NAMESPACE=fio-test \
  -e DESCRIPTION=create-testdir \
  -e TEST_SIZE=4G \
  -e RUNTIME=120 \
  -e BLOCK_SIZES="1024k" \
  -e IO_PATTERNS=write \
  -e NUMJOBS=1 \
  -e IODEPTH=16 \
  "${COMMON_MOUNTS[@]}" \
  quay.io/ekuric/fio-benchmark:latest
```

#### 2) Linux — create VMs + raw-disk FIO

Blank data disk attached; FIO `--filename=/dev/vdb` (no mkfs). Leave `DEVICES` empty.

```bash
podman run --rm --init --pids-limit=-1 --privileged \
  -e CREATE_VMS=true \
  -e VMS=5 \
  -e VM_PREFIX=fio-linux \
  -e VM_CPU=4 \
  -e VM_MEMORY=8Gi \
  -e VM_IMAGE=quay.io/containerdisks/fedora:41 \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e DATA_DISK_SIZE=40Gi \
  -e USE_RAW_DISK=true \
  -e NAMESPACE=fio-test \
  -e DESCRIPTION=create-raw \
  -e TEST_SIZE=10G \
  -e RUNTIME=300 \
  -e BLOCK_SIZES="4k 1024k" \
  -e IO_PATTERNS="write randwrite" \
  "${COMMON_MOUNTS[@]}" \
  quay.io/ekuric/fio-benchmark:latest
```

#### 3) Linux — create VMs + filesystem data disk

Format/mount `vdb` (default XFS at `/root/tests/data`), then FIO on the filesystem.

```bash
podman run --rm --init --pids-limit=-1 --privileged \
  -e CREATE_VMS=true \
  -e VMS=3 \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e DATA_DISK_SIZE=40Gi \
  -e USE_TESTDIR=false \
  -e USE_RAW_DISK=false \
  -e NAMESPACE=fio-test \
  -e DESCRIPTION=create-fsdisk \
  -e TEST_SIZE=5G \
  -e RUNTIME=300 \
  -e BLOCK_SIZES="4k 8k 128k" \
  -e IO_PATTERNS="read write randread randwrite" \
  "${COMMON_MOUNTS[@]}" \
  quay.io/ekuric/fio-benchmark:latest
```

#### 4) Windows — create VMs + OS-disk testdir

Requires `WIN_VM_IMAGE` (HTTP URL). FIO on `c:/testdir`; binary at `c:/tools/fio`.

```bash
podman run --rm --init --pids-limit=-1 --privileged \
  -e CREATE_VMS=true \
  -e WIN_VMS=2 \
  -e WIN_VM_PREFIX=fio-win \
  -e WIN_VM_CPU=4 \
  -e WIN_VM_MEMORY=8Gi \
  -e WIN_VM_IMAGE="https://example.com/windows.qcow2" \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e WIN_ROOT_VOLUME_SIZE=60Gi \
  -e USE_TESTDIR=true \
  -e NAMESPACE=fio-test \
  -e DESCRIPTION=create-win-testdir \
  -e WIN_TEST_SIZE=10GB \
  -e WIN_RUNTIME=300 \
  -e WIN_BLOCK_SIZES="1024k" \
  -e WIN_IO_PATTERNS=write \
  "${COMMON_MOUNTS[@]}" \
  quay.io/ekuric/fio-benchmark:latest
```

#### 5) Windows — create VMs + raw PhysicalDrive

```bash
podman run --rm --init --pids-limit=-1 --privileged \
  -e CREATE_VMS=true \
  -e WIN_VMS=2 \
  -e WIN_VM_IMAGE="https://example.com/windows.qcow2" \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e DATA_DISK_SIZE=40Gi \
  -e USE_RAW_DISK=true \
  -e NAMESPACE=fio-test \
  -e DESCRIPTION=create-win-raw \
  -e WIN_TEST_SIZE=10GB \
  -e WIN_RUNTIME=300 \
  "${COMMON_MOUNTS[@]}" \
  quay.io/ekuric/fio-benchmark:latest
```

#### 6) Mixed Linux + Windows create (keep VMs, inspect later)

```bash
podman run --rm --init --pids-limit=-1 --privileged \
  -e CREATE_VMS=true \
  -e VMS=3 \
  -e WIN_VMS=2 \
  -e VM_IMAGE=quay.io/containerdisks/fedora:41 \
  -e WIN_VM_IMAGE="https://example.com/windows.qcow2" \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e USE_TESTDIR=true \
  -e CLEANUP=false \
  -e NAMESPACE=fio-test \
  -e DESCRIPTION=create-mixed-keep \
  "${COMMON_MOUNTS[@]}" \
  quay.io/ekuric/fio-benchmark:latest
# Then: virtctl ssh ... -i /tmp/fio-ssh/fio-ssh-<runid>/id_ed25519
```

#### 7) Create, run, delete VMs (`CLEANUP=true`)

Same as any recipe above, plus:

```bash
  -e CLEANUP=true \
```

#### 8) Existing hosts (no create) — for comparison

```bash
# Linux raw on pre-created VMs
podman run --rm --init --pids-limit=-1 --privileged \
  -e CREATE_VMS=false \
  -e HOST_PATTERN="fio-linux-{1..3}" \
  -e DEVICES="fio-linux-{1..3}=vdb" \
  -e USE_RAW_DISK=true \
  -e NAMESPACE=fio-test \
  -v /root/fio-results:/work/results:Z \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro,Z \
  -v /root/.kube:/root/.kube:ro,Z \
  quay.io/ekuric/fio-benchmark:latest
```

**Jenkins:** use `Jenkinsfile-linux` / `Jenkinsfile-windows` — set `CREATE_VMS=true`,
counts / prefix / images / `STORAGE_CLASS`, leave host patterns empty, choose
`USE_TESTDIR` or `USE_RAW_DISK`. The pipelines mount `/tmp/fio-ssh:/host-tmp:Z`
and create `/tmp/fio-ssh` on the bastion before `podman run`.

## Fixed JSON result paths (all modes)

FIO JSON (`write_dataset.json`, per-test `*.json`) is **always** written on the OS disk
so test-disk failures cannot lose results. Collection/scp uses the same paths:

| OS | Remote JSON directory |
|---|---|
| Linux | `/root/fio-results` |
| Windows | `c:/fio-results` |

YAML `output.directory` / `windows.output_win.directory` and env `OUTPUT_DIR` /
`WIN_OUTPUT_DIR` are ignored for remote JSON (forced to the paths above).

## OS-disk load (`--testdir`)

By default, FIO uses a **separate data disk** from YAML/env (`DEVICES`, `WIN_DEVICES` /
`storage.devices`, `windows.storage_win.devices`): format/mount on Linux,
`provision-data-disk.ps1` on Windows, and copy FIO to the data drive on Windows.

**`--testdir`** / **`USE_TESTDIR=true`** runs load on the **OS disk** instead (no
formatting of a separate data disk):

| OS | FIO test data | FIO binary (Windows) | JSON results |
|---|---|---|---|
| Linux | `/root/testdir` | n/a (system `fio`) | `/root/fio-results` (fixed) |
| Windows | `c:/testdir` | `c:/tools/fio` (in place, no copy to `d:`) | `c:/fio-results` (fixed) |

## Raw-disk load (`--raw-disk`)

**`--raw-disk`** / **`USE_RAW_DISK=true`** runs FIO against the attached data disk as a
**raw block device** (no filesystem). Mutually exclusive with `--testdir`.
`DEVICES` / `WIN_DEVICES` remain required. OS disks (`vda`/`sda`/…, Windows Disk `0`)
are refused.

| OS | FIO I/O target | Prep | FIO binary (Windows) | JSON results |
|---|---|---|---|---|
| Linux | `--filename=/dev/{device}` | validate + unmount; no mkfs | n/a | `/root/fio-results` |
| Windows | `--filename=\\.\PhysicalDrive{N}` | clear partitions; no provision script | `c:/tools/fio` in place | `c:/fio-results` |

Dataset pre-write still runs (size-based fill of `--size` against the raw device).
When `numjobs > 1`, `--offset_increment={test_size}` is added so jobs do not overlap.

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="vm-{1..10}" \
  -e DEVICES="vm-{1..10}=vdb" \
  -e USE_RAW_DISK=true \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

### What is ignored in `--testdir` mode

fio-tests does **not** use data-disk settings from the config. You can leave them in
a shared YAML for non-testdir runs; with `--testdir` they are skipped and logged as
ignored:

| Source | Ignored when `--testdir` |
|---|---|
| YAML `storage.devices` / `mount_point` / `filesystem` | Yes (Linux) |
| YAML `windows.storage_win.devices` / `mount_point` | Yes |
| YAML `windows.fio_win.run_dir` (and `fio_dir` / `root_dir`) | Yes → fixed `c:/tools/fio` |
| YAML `windows.output_win.directory` | Yes → fixed `c:/fio-results` |
| Env `DEVICES` / `WIN_DEVICES` | Not required; leave empty |
| Env `MOUNT_POINT` / `WIN_MOUNT_POINT` / `WIN_RUN_DIR` | Not applied for paths |

Still used from YAML/env in testdir mode: host lists, `fio` / `fio_win` job parameters
(`test_size`, `runtime`, `block_sizes`, `io_patterns`, `numjobs`, `iodepth`, etc.),
and retry/monitoring. JSON results always use the fixed OS-disk paths above.

**Env mode:** omit `DEVICES` / `WIN_DEVICES` when `USE_TESTDIR=true` (entrypoint
omits the `storage` / `storage_win` sections entirely). For `USE_RAW_DISK=true`,
only `devices` is emitted (no mount/fs fields).

**YAML mode:** a config that still contains `storage_win.devices: …` is fine; with
`--testdir` those entries are not validated as active disks and are not formatted.

**Podman (CLI after image name):**

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./fio-config.yaml:/work/fio-config.yaml \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest \
  --testdir
```

**Podman (env — entrypoint adds `--testdir`):**

```bash
-e USE_TESTDIR=true \
-e WIN_HOST_PATTERN="winc-1" \
# WIN_DEVICES not needed
```

**Jenkins:** set build parameter `USE_TESTDIR` to `true` (`Jenkinsfile`, `Jenkinsfile-linux`,
`Jenkinsfile-windows`). Leave `DEVICES` / `WIN_DEVICES` empty. No Jenkinsfile changes
are required beyond that existing parameter.

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

### `--testdir`

Run FIO on the OS disk (`/root/testdir` on Linux, `c:/testdir` on Windows). Skips
data-disk format/mount/provision and Windows copy of FIO to `d:`. YAML
`storage` / `windows.storage_win` device mappings are ignored (see
[OS-disk load (`--testdir`)](#os-disk-load---testdir)).

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="vm-{1..10}" \
  -e USE_TESTDIR=true \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
```

### `--raw-disk`

Run FIO against the attached data disk as a raw block device (no filesystem).
Requires `DEVICES` / `WIN_DEVICES`. Mutually exclusive with `--testdir`
(see [Raw-disk load (`--raw-disk`)](#raw-disk-load---raw-disk)).

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="vm-{1..10}" \
  -e DEVICES="vm-{1..10}=vdb" \
  -e USE_RAW_DISK=true \
  -v /root/fio-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/fio-benchmark:latest
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

**Existing VMs** (`CREATE_VMS=false`): `virtctl ssh` uses `/root/.ssh/id_rsa`
inside the container. Mount your private key:

```bash
-v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro
```

If your key is at a non-standard path, mount it to the expected location:

```bash
-v /home/user/.ssh/my-key:/root/.ssh/id_rsa:ro
```

**On-demand VMs** (`CREATE_VMS=true`): an ephemeral ed25519 keypair is generated
under `/tmp/fio-ssh-<runid>/` and injected into the guests (Linux cloud-init /
Windows `#ps1_sysnative`). Mount `/tmp/fio-ssh:/host-tmp:Z` to also keep a copy
on the bastion for inspection — see [On-demand VM provision](#on-demand-vm-provision---create-vms).

## Container Layout

```
/work/
  fio-tests.py            (CLI shim — entrypoint runs this)
  fio_tests/              (implementation package)
    cli.py                (argparse + orchestration)
    config.py             (YAML load, FioTestConfig, --testdir overrides)
    executor.py           (SSH / virtctl remote commands)
    packages.py           (FIO install / Windows FIO provision)
    storage.py            (format / mount / guest disk prep)
    provision.py          (on-demand VM create / SSH keys / cleanup)
  templates/              (Linux/Windows DV+VM manifest shapes)
    dataset.py            (pre-write test data)
    fio_run.py            (performance FIO jobs + mid-test migrate)
    migration.py          (virtctl migrate / monitor)
    results.py            (collect + combine JSON)
    util.py / constants.py
  entrypoint.sh           (kubeconfig check, env→YAML, exec shim)
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
- **Default mode:** a separate data disk for testing, added to the VM at creation time
- **`--testdir` mode:** OS disk only; writable `/root/testdir`
- **`--raw-disk` mode:** separate data disk used as a raw block device (no filesystem)
- JSON results always under `/root/fio-results` on the OS disk
- Passwordless SSH access enabled -- the public SSH key must be baked into the VM image at creation time
- FIO dependencies (`fio`, `xfsprogs`, `util-linux`):
  - **Default (`fio_installed: false`)**: installed automatically via dnf if missing
  - **Golden image (`fio_installed: true`)**: must be pre-installed in the VM image (see `../imagecreator.sh`)

### Windows VMs

- FIO must be pre-installed at `C:\tools\fio`
- **Default mode:** a separate data disk; the script copies FIO to the data drive and provisions it if needed
- **`--testdir` mode:** OS disk only; `c:\testdir` for data; uses `c:\tools\fio\fio.exe` in place
- **`--raw-disk` mode:** separate Disk ID as `\\.\PhysicalDriveN` (no filesystem); FIO from `c:\tools\fio`
- JSON results always under `c:\fio-results` on the OS disk
- Passwordless SSH access enabled (OpenSSH server with public key authentication) -- the public SSH key must be baked into the VM image at creation time

### All VMs

- Must be accessible via `virtctl ssh` (OpenShift VMs) or plain `ssh` (baremetal/KVM)
- The SSH key used by the container must match the public key in the VM image
- Mount the matching private key into the container: `-v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro`

## Notes

- Implementation lives in the `fio_tests/` package; `fio-tests.py` is a thin CLI shim
  (`python3 fio-tests.py` and `python3 -m fio_tests` both work). Entrypoint unchanged.
- If a config file is mounted, it is used as-is. If not, env vars generate one. No merging.
- Env-var mode supports Linux-only, Windows-only, or mixed Linux+Windows testing.
- Use `WIN_*` prefixed env vars for Windows hosts (set `WIN_HOSTS` or `WIN_HOST_PATTERN` to activate).
- `--privileged` is needed for the virtctl SSH proxy to work.
- `/root/.kube/config` mount gives the container access to the OCP cluster.
- Existing hosts: mount SSH private key to `/root/.ssh/id_rsa`. Create-vms: ephemeral
  key under `/tmp/fio-ssh-<runid>/` (+ bastion mirror via `/tmp/fio-ssh:/host-tmp:Z`).
- `--yes-i-mean-it` is passed automatically to skip the device formatting confirmation prompt.
- Use `--testdir` or `USE_TESTDIR=true` for OS-disk load. YAML `storage` /
  `windows.storage_win` devices and mount points (and Windows `fio_win.run_dir`)
  are ignored; leave Jenkins `DEVICES` / `WIN_DEVICES` empty.
- Use `--raw-disk` or `USE_RAW_DISK=true` for raw block-device load (keep
  `DEVICES` / `WIN_DEVICES` unless `CREATE_VMS`). Mutually exclusive with `--testdir`.
- Use `--create-vms` / `CREATE_VMS=true` to provision + test in one step
  (`VMS`/`WIN_VMS`, `STORAGE_CLASS`, prefixes/images; leave host patterns empty;
  namespace must already exist; optional `CLEANUP`). Recipes:
  [On-demand VM provision](#on-demand-vm-provision---create-vms).
- Remote FIO JSON is always on the OS disk: `/root/fio-results` (Linux) and
  `c:/fio-results` (Windows), for every storage mode.
- Set `fio_installed: true` (or `FIO_INSTALLED=true`) when using golden Linux images with FIO pre-baked; leave `false` for stock cloud images.
- Optional `FSYNC` / `WIN_FSYNC` (or YAML `fio.fsync` / `windows.fio_win.fsync`) adds FIO `--fsync=N`. Empty/unset disables it (default). Values like `1` or `32` call `fsync` after every N writes — this measures durable-write cost and typically lowers write IOPS/BW sharply. Applied to dataset pre-write and performance jobs when set.
- The entrypoint always prints the config before running, so you can see exactly what values are used.
- Example configs are available inside the container at `/work/examples/`.

## Optional FIO fsync

By default FIO runs **without** `--fsync` (throughput-oriented). To simulate commit-style durable writes:

**Podman / env:**
```bash
-e FSYNC=32          # Linux → --fsync=32
-e WIN_FSYNC=1       # Windows → --fsync=1
```

**YAML:**
```yaml
fio:
  fsync: "32"        # Linux

windows:
  fio_win:
    fsync: 1         # Windows
```

**Jenkins:** set parameter `FSYNC` or `WIN_FSYNC` (leave empty to disable).

`0`, empty, `null`, or omitted → no `--fsync` flag.
