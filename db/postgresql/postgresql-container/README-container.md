# PostgreSQL container

Run HammerDB TPCC tests against PostgreSQL on Linux VMs from a container.
Configuration can be provided by mounting a YAML config file or by setting
environment variables.

Code lives in the `postgresql_tests/` package (`python3 -m postgresql_tests` or the
thin `postgresql.py` shim). Hosts can be **pre-created** (`HOST_PATTERN` / `HOSTS` /
`HOST_LABELS`) or **provisioned on demand** (`CREATE_VMS=true`).

## Building

```bash
cd db/postgresql/postgresql-container
podman build -t quay.io/ekuric/postgresql-benchmark:latest .
```

HammerDB PostgreSQL wrapper scripts from [ioscale](https://github.com/ekuric/ioscale) are
baked into the image at `/work/hammerdb-bundled` at build time. Pin a specific version:

```bash
podman build \
  --build-arg IOSCALE_REF=main \
  -t quay.io/ekuric/postgresql-benchmark:latest .
```

Override the virtctl version at build time:

```bash
podman build --build-arg VIRTCTL_VERSION=v1.8.0 -t quay.io/ekuric/postgresql-benchmark:latest .
```

## Usage Modes

### Mode 1: Config file (recommended)

Mount your `postgresql-config.yaml`. Everything comes from the file.

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./postgresql-config.yaml:/work/postgresql-config.yaml \
  -v /root/postgresql-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest
```

Or point to the baked-in example config:

```bash
podman run --rm --init --pids-limit=-1 \
  -e CONFIG=/work/examples/postgresql-config.yaml \
  -v /root/postgresql-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest
```

### Mode 2: Env vars only (CI/CD) — pre-created hosts

No config file needed. The entrypoint generates it from env vars.
At minimum, one host selection variable is required (unless `CREATE_VMS=true`).

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="vm-{1..10}" \
  -e NAMESPACE="default" \
  -e DESCRIPTION="postgresql perf test" \
  -e DISK_LIST="/dev/vdc" \
  -e WAREHOUSE_COUNT=50 \
  -e TEST_DURATION=15 \
  -e USER_COUNT="1 10 20 50" \
  -e HAMMERDB_SOURCE=bundled \
  -e HAMMERDB_PATH="/root/hammerdb-tpcc-wrapper-scripts" \
  -e HAMMERDB_INSTALL_DIR="/usr/local/HammerDB" \
  -v /root/postgresql-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest
```

### Mode 3: On-demand VMs (`CREATE_VMS=true`)

Provision Linux VMs, wait until Ready + SSH, then run the same install/build/HammerDB
flow. Leave `HOST_PATTERN` / `HOSTS` / `HOST_LABELS` empty — they are ignored.
Namespace must **already exist**. Managed label:
`postgresql-container.ioscale/managed=true` (does not touch fio-managed VMs).

```bash
mkdir -p /tmp/postgresql-ssh /root/postgresql-results
podman run --rm --init --pids-limit=-1 \
  -e CREATE_VMS=true \
  -e VMS=3 \
  -e VM_PREFIX=postgresql \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e NAMESPACE=default \
  -e DESCRIPTION="postgresql create-vms" \
  -e WAREHOUSE_COUNT=50 \
  -e TEST_DURATION=15 \
  -e USER_COUNT="1 10" \
  -e CLEANUP=false \
  -v /root/postgresql-results:/work/results:Z \
  -v /root/.kube:/root/.kube:ro,Z \
  -v /tmp/postgresql-ssh:/host-tmp:Z \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest
```

After the run (with `CLEANUP=false`), inspect from the bastion:

```bash
ls /tmp/postgresql-ssh/
virtctl ssh root@vmi/postgresql-1 -n default -i /tmp/postgresql-ssh/postgresql-ssh-<runid>/id_ed25519
```

Set `CLEANUP=true` to delete managed VMs/DVs after the test (default: keep).
The next `CREATE_VMS` run always wipes prior managed leftovers first.

**How it works:** If a config file exists at the `CONFIG` path, it is used
as-is (hosts still deferred when `CREATE_VMS=true`). If no file is found, the
entrypoint generates one from env vars. No merging or patching.

**Important:** Mount `/work/results` to a host directory to keep results after
the container exits. Without this mount and with `--rm`, results are lost.

## Environment Variables

Env vars are used when no config file is mounted (Mode 2).
`CONFIG`, `KUBEADMIN_PASSWORD`, and `API_URL` apply to both modes.

| Variable | Default | Description |
|---|---|---|
| `CONFIG` | `/work/postgresql-config.yaml` | Path to the YAML config file inside the container |
| `CREATE_VMS` | `false` | Provision Linux VMs before the test; leave host patterns empty |
| `CLEANUP` | `false` | Delete managed VMs after the test (default: keep) |
| `VMS` | `0` | Number of Linux VMs when `CREATE_VMS=true` |
| `VM_PREFIX` | `postgresql` | Name prefix → `postgresql-1..N` |
| `VM_CPU` / `VM_MEMORY` | `2` / `4Gi` | Guest size |
| `VM_IMAGE` | `quay.io/containerdisks/fedora:41` | Registry containerdisk |
| `STORAGE_CLASS` | -- | **Required** when `CREATE_VMS=true` |
| `ROOT_VOLUME_SIZE` / `DATA_DISK_SIZE` | `20Gi` / `40Gi` | Root + blank data disk |
| `SSH_KEY_PATH` | `/tmp/postgresql-ssh-<runid>` | In-container ephemeral keypair directory |
| `HOSTS` | -- | Space-separated host names (pre-created path) |
| `HOST_PATTERN` | -- | Brace-expansion pattern, e.g. `vm-{1..10}` (ignored if `CREATE_VMS`) |
| `HOST_LABELS` | -- | OCP label selector for VMs (ignored if `CREATE_VMS`) |
| `NAMESPACE` | `default` | OpenShift namespace (must already exist for create-vms) |
| `DESCRIPTION` | -- | Test run description |
| `DISK_LIST` | `/dev/vdc` | Block device for PostgreSQL data (`vdb` when create-vms) |
| `MOUNT_POINT` | -- | Mount point (if set, `DISK_LIST` is ignored) |
| `PERSISTENT` | -- | Set to `true` for persistent mounts via /etc/fstab |
| `WAREHOUSE_COUNT` | `50` | TPCC warehouse count |
| `TEST_DURATION` | `15` | Test duration in minutes |
| `USER_COUNT` | `1` | Space-separated user counts (e.g. `"1 10 20 50"`) |
| `HAMMERDB_SOURCE` | `bundled` | Script source: `bundled` (from image) or `remote_git` (clone on each VM) |
| `HAMMERDB_BUNDLED_PATH` | `/work/hammerdb-bundled` | Path to bundled scripts inside the container |
| `HAMMERDB_PATH` | `/root/hammerdb-tpcc-wrapper-scripts` | Deploy path on remote hosts |
| `HAMMERDB_INSTALL_DIR` | `/usr/local/HammerDB` | HammerDB installation directory on remote hosts |
| `HAMMERDB_REPO` | `https://github.com/ekuric/ioscale.git` | Only used when `HAMMERDB_SOURCE=remote_git` |
| `MIGRATE_USER_COUNTS` | -- | User counts that trigger VM migration (e.g. `"4 8"`) |
| `MIGRATE_INTERVAL` | `0` | Seconds between migrations (0 = parallel) |
| `RETRY_INTERVAL` | `30` | Retry interval in seconds |
| `MAX_RETRIES` | `10` | Maximum retry attempts |
| `MONITOR_INTERVAL` | `60` | Task monitor interval in seconds |
| `KUBEADMIN_PASSWORD` | -- | If set, runs `oc login` before the test |
| `API_URL` | `https://api.ocp.example.com:6443` | Cluster API URL for `oc login` |

## postgresql.py CLI Parameters

The entrypoint passes `--virtctl-only` automatically. Any extra arguments
after the image name are forwarded to `postgresql.py`.

### `--dry-run`

Validate configuration and show what would be done without executing.

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./postgresql-config.yaml:/work/postgresql-config.yaml \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest \
  --dry-run
```

### `-v`, `--verbose`

Enable verbose/debug output.

### `--prepare-hosts`

Only install packages, clone repo, and set up PostgreSQL. Skip testing.

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="vm-{1..10}" \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest \
  --prepare-hosts
```

### `--copy-results`

Only copy results from hosts (skip everything else).

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./postgresql-config.yaml:/work/postgresql-config.yaml \
  -v /root/postgresql-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest \
  --copy-results
```

### `--ssh-only`

Force plain SSH (overrides the default `--virtctl-only`).

## Usage Examples

Assume results and kubeconfig mounts on the bastion. For **pre-created** hosts also
mount `/root/.ssh/id_rsa`. For **create-vms** also mount `/tmp/postgresql-ssh:/host-tmp:Z`
(and skip the id_rsa mount — guests use the ephemeral key).

### 1) Pre-created hosts (config file)

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./postgresql-config.yaml:/work/postgresql-config.yaml \
  -v /root/postgresql-results:/work/results:Z \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro,Z \
  -v /root/.kube:/root/.kube:ro,Z \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest
```

### 2) Pre-created hosts (env vars)

```bash
podman run --rm --init --pids-limit=-1 \
  -e CREATE_VMS=false \
  -e HOST_PATTERN="vm-{1..10}" \
  -e NAMESPACE="default" \
  -e DESCRIPTION="postgresql perf test" \
  -e DISK_LIST="/dev/vdc" \
  -e WAREHOUSE_COUNT=50 \
  -e TEST_DURATION=15 \
  -e USER_COUNT="1 10 20 50" \
  -e HAMMERDB_SOURCE=bundled \
  -e HAMMERDB_PATH="/root/hammerdb-tpcc-wrapper-scripts" \
  -e HAMMERDB_INSTALL_DIR="/usr/local/HammerDB" \
  -e MIGRATE_USER_COUNTS="4 8" \
  -e MIGRATE_INTERVAL=0 \
  -v /root/postgresql-results:/work/results:Z \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro,Z \
  -v /root/.kube:/root/.kube:ro,Z \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest
```

Optional: clone scripts from git on each VM instead of the image bundle:

```bash
  -e HAMMERDB_SOURCE=remote_git \
  -e HAMMERDB_REPO="https://github.com/ekuric/ioscale.git"
```

### 3) Create VMs, run HammerDB, keep guests (`CLEANUP=false`)

```bash
mkdir -p /tmp/postgresql-ssh /root/postgresql-results
podman run --rm --init --pids-limit=-1 \
  -e CREATE_VMS=true \
  -e CLEANUP=false \
  -e VMS=3 \
  -e VM_PREFIX=postgresql \
  -e VM_CPU=4 \
  -e VM_MEMORY=8Gi \
  -e VM_IMAGE=quay.io/containerdisks/fedora:41 \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e ROOT_VOLUME_SIZE=20Gi \
  -e DATA_DISK_SIZE=40Gi \
  -e NAMESPACE=default \
  -e DESCRIPTION="postgresql-create-vms-keep" \
  -e WAREHOUSE_COUNT=50 \
  -e TEST_DURATION=15 \
  -e USER_COUNT="1 10 20" \
  -v /root/postgresql-results:/work/results:Z \
  -v /root/.kube:/root/.kube:ro,Z \
  -v /tmp/postgresql-ssh:/host-tmp:Z \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest

# Inspect after the run:
ls /tmp/postgresql-ssh/
virtctl ssh root@vmi/postgresql-1 -n default \
  -i /tmp/postgresql-ssh/postgresql-ssh-<runid>/id_ed25519
```

### 4) Create VMs, run HammerDB, delete guests (`CLEANUP=true`)

Same as (3) with `-e CLEANUP=true`. Managed VMs/DVs are removed after the test;
the bastion SSH mirror under `/tmp/postgresql-ssh/` is removed too.

### 5) Create VMs + prepare hosts only (no HammerDB run)

```bash
mkdir -p /tmp/postgresql-ssh
podman run --rm --init --pids-limit=-1 \
  -e CREATE_VMS=true \
  -e VMS=2 \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e NAMESPACE=default \
  -e DESCRIPTION="postgresql-prepare" \
  -v /root/postgresql-results:/work/results:Z \
  -v /root/.kube:/root/.kube:ro,Z \
  -v /tmp/postgresql-ssh:/host-tmp:Z \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest \
  --prepare-hosts
```

### 6) Dry-run (pre-created or create-vms)

```bash
# Pre-created
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="vm-{1..5}" \
  -e DISK_LIST="/dev/vdc" \
  -v /root/.kube:/root/.kube:ro,Z \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest \
  --dry-run --verbose

# Create-vms (validates provision args; does not create VMs)
podman run --rm --init --pids-limit=-1 \
  -e CREATE_VMS=true \
  -e VMS=3 \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -v /root/.kube:/root/.kube:ro,Z \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest \
  --dry-run --verbose
```

### 7) With oc login

```bash
podman run --rm --init --pids-limit=-1 \
  -e KUBEADMIN_PASSWORD="my-password" \
  -e API_URL="https://api.mycluster.example.com:6443" \
  -e CREATE_VMS=true \
  -e VMS=3 \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e NAMESPACE=default \
  -v /root/postgresql-results:/work/results:Z \
  -v /tmp/postgresql-ssh:/host-tmp:Z \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest
```

**Jenkins:** use the repo `Jenkinsfile` — set `CREATE_VMS=true`, `VMS`, `STORAGE_CLASS`,
leave `HOST_PATTERN` empty; or `CREATE_VMS=false` + `HOST_PATTERN` for pre-created VMs.
The pipeline mounts `/tmp/postgresql-ssh:/host-tmp:Z` and creates `/tmp/postgresql-ssh` on the bastion.

## SSH Key

**Pre-created hosts** (`CREATE_VMS=false`): `virtctl ssh` uses `/root/.ssh/id_rsa`.
Mount your private key:

```bash
-v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro
```

**On-demand VMs** (`CREATE_VMS=true`): an ephemeral ed25519 keypair is generated
under `/tmp/postgresql-ssh-<runid>/` and injected via cloud-init. Mount a bastion
subdir (not all of `/tmp` — SELinux) to keep a copy for inspection:

```bash
mkdir -p /tmp/postgresql-ssh
-v /tmp/postgresql-ssh:/host-tmp:Z
```

| Location | Path | Lifetime |
|---|---|---|
| In-container | `/tmp/postgresql-ssh-<runid>/id_ed25519` | Removed when the container exits |
| Bastion mirror | `/tmp/postgresql-ssh/postgresql-ssh-<runid>/id_ed25519` | Kept if VMs are kept; removed with `--cleanup` |

## Container Layout

```
/work/
  postgresql.py                  # thin shim → postgresql_tests.cli.main
  postgresql_tests/              # package (config, provision, build, workload, …)
  templates/                  # reference linux-source-dv.yaml / linux-vm.yaml
  entrypoint.sh
  hammerdb-bundled/           # ioscale templates/postgresql, baked at image build
  postgresql-config.yaml         # mounted by user, or generated from env vars
  examples/
    postgresql-config.yaml
  results/                    # output directory
```

## VM Live Migration During Tests

The script can live-migrate OpenShift/KubeVirt VMs **while HammerDB tests are
running** to measure the impact of VM migration on database performance. Unlike
the FIO container (which triggers migration by I/O pattern), PostgreSQL migration
is triggered by **user count** -- specific test iterations where migration
should occur.

### Configuration

```yaml
migrate:
  user_counts: "4 8 16"   # user counts that trigger migration
  interval: 0             # 0 = parallel, >0 = sequential with delay (seconds)
```

- `user_counts` -- space-separated list of user counts. Migration happens only
  during test iterations matching these values. Set to null or empty to disable.
- `interval` -- migration strategy:
  - `0` -- migrate all VMs simultaneously (parallel)
  - `>0` -- migrate VMs one at a time with this many seconds between each

### When Migration Triggers

Migration occurs at the **midpoint of the actual test runtime, after the rampup
period**. HammerDB has a rampup phase before the timed test begins, so the
migration timing accounts for this:

```
migration_time = rampup_time + (test_duration / 2)
```

For example, with a 2-minute rampup and 10-minute test duration, migration
triggers at 2 + 5 = 7 minutes after the HammerDB process starts.

### Flow for Each User Count

1. Start HammerDB test on all hosts (with the current user count)
2. If current `user_count` is in `migrate.user_counts`:
   - Wait for `rampup_time + (test_duration / 2)` seconds
   - Migrate all VMs (parallel or sequential per `interval`)
   - Verify HammerDB processes are still running after migration
3. Wait for the test to complete
4. Move to next user count

### Retry Logic

Same as FIO: failed migrations get one automatic retry. If retries also fail,
the error is logged but the test continues and results are still collected.

### Example

With `user_counts: "4 8"`, `interval: 2`, `test_duration: 15` (minutes),
rampup ~2 minutes, 10 VMs:

```
user_count=1: run test (no migration)
user_count=4: run test
  ├── Wait 7 min (2 min rampup + 5 min = midpoint of 10 min actual test)
  ├── Migrate vm-1 → 2s → vm-2 → 2s → ... → vm-10
  ├── Verify HammerDB still running
  └── Wait for test to finish
user_count=8: run test
  ├── Wait 7 min (midpoint)
  ├── Migrate all VMs (sequential)
  └── Wait for test to finish
```

### Env var mode

When using env vars (no config file), set:

```bash
-e MIGRATE_USER_COUNTS="4 8 16" \
-e MIGRATE_INTERVAL=2
```

### Notes on migration

- Only VMs are migrated -- baremetal/SSH hosts are skipped automatically
- Requires `--virtctl-only` mode (default in container) and a valid namespace
- Uses `virtctl -n <namespace> migrate <vm>` for each VM
- Migration does not stop the HammerDB test -- it runs concurrently
- After migration, the script verifies HammerDB processes are still running
- Results from migrated VMs are collected normally after the test

## Notes

- If a config file is mounted, it is used as-is. If not, env vars generate one. No merging.
- `--privileged` is needed for the virtctl SSH proxy to work.
- `/root/.kube/config` mount gives the container access to the OCP cluster.
- Pre-created path: mount SSH key to `/root/.ssh/id_rsa`. Create-vms path: ephemeral key + optional `/tmp/postgresql-ssh:/host-tmp:Z`.
- Default `HAMMERDB_SOURCE=bundled` deploys scripts from `/work/hammerdb-bundled` in the image. Use `remote_git` only when you want each VM to clone `HAMMERDB_REPO` at runtime.
- The entrypoint always prints the config before running, so you can see exactly what values are used.
- Jenkins: set `CREATE_VMS=true`, `VMS`, `STORAGE_CLASS`, leave `HOST_PATTERN` empty; or keep `CREATE_VMS=false` + `HOST_PATTERN` for the pre-created path. Pipeline mounts `/tmp/postgresql-ssh:/host-tmp:Z`.
