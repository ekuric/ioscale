# mssqldb.py Container (Linux MSSQL Server)

Run HammerDB TPCC tests against MSSQL Server on **Linux** VMs from a container.
This is the Linux-based MSSQL test (not the Windows `mssqlwin.py` variant).

Code lives in the `mssql_tests/` package (`python3 -m mssql_tests` or the thin
`mssqldb.py` shim). Hosts can be **pre-created** (`HOST_PATTERN` / `HOSTS` /
`HOST_LABELS`) or **provisioned on demand** (`CREATE_VMS=true`).

## Building

```bash
cd ioscale_files/db/mssql/mssql-container
podman build -t quay.io/ekuric/mssqllin-benchmark:latest .
```

HammerDB MSSQL wrapper scripts from [ioscale](https://github.com/ekuric/ioscale) are
baked into the image at `/work/hammerdb-bundled` at build time. Pin a specific version:

```bash
podman build \
  --build-arg IOSCALE_REF=main \
  -t quay.io/ekuric/mssqllin-benchmark:latest .
```

Override the virtctl version at build time (base image):

```bash
podman build --build-arg VIRTCTL_VERSION=v1.8.0 -t quay.io/ekuric/mssqllin-benchmark:latest .
```

## Usage Modes

### Mode 1: Config file (recommended)

Mount your `mssql-config.yaml`. Everything comes from the file.

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./mssql-config.yaml:/work/mssql-config.yaml \
  -v /root/mssqldb-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqllin-benchmark:latest
```

Or point to the baked-in example config:

```bash
podman run --rm --init --pids-limit=-1 \
  -e CONFIG=/work/examples/mssql-config.yaml \
  -v /root/mssqldb-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqllin-benchmark:latest
```

### Mode 2: Env vars only (CI/CD) — pre-created hosts

No config file needed. The entrypoint generates it from env vars.
At minimum, one host selection variable is required (unless `CREATE_VMS=true`).

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="mssql-{1..10}" \
  -e NAMESPACE="default" \
  -e DESCRIPTION="mssql perf test" \
  -e DISK_LIST="/dev/vdc" \
  -e WAREHOUSE_COUNT=50 \
  -e BUILD_USERS=50 \
  -e TEST_DURATION=15 \
  -e USER_COUNT="1 10 20 50" \
  -e MSSQL_PASS="mssqlpasswd1!" \
  -e MAX_SERVER_MEMORY_MB=38000 \
  -e REBUILDDB=true \
  -e HAMMERDB_SOURCE=bundled \
  -e HAMMERDB_PATH="/root/hammerdb-tpcc-wrapper-scripts" \
  -v /root/mssqldb-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqllin-benchmark:latest
```

### Mode 3: On-demand VMs (`CREATE_VMS=true`)

Provision Linux VMs, wait until Ready + SSH, then run the same install/build/HammerDB
flow. Leave `HOST_PATTERN` / `HOSTS` / `HOST_LABELS` empty — they are ignored.
Namespace must **already exist**. Managed label:
`mssql-container.ioscale/managed=true` (does not touch fio/mariadb-managed VMs).

```bash
mkdir -p /tmp/mssql-ssh /root/mssqldb-results
podman run --rm --init --pids-limit=-1 \
  -e CREATE_VMS=true \
  -e VMS=3 \
  -e VM_PREFIX=mssql \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e NAMESPACE=default \
  -e DESCRIPTION="mssql create-vms" \
  -e WAREHOUSE_COUNT=50 \
  -e BUILD_USERS=50 \
  -e TEST_DURATION=15 \
  -e USER_COUNT="1 10" \
  -e MSSQL_PASS="mssqlpasswd1!" \
  -e CLEANUP=false \
  -v /root/mssqldb-results:/work/results:Z \
  -v /root/.kube:/root/.kube:ro,Z \
  -v /tmp/mssql-ssh:/host-tmp:Z \
  --privileged \
  quay.io/ekuric/mssqllin-benchmark:latest
```

After the run (with `CLEANUP=false`), inspect from the bastion:

```bash
ls /tmp/mssql-ssh/
virtctl ssh root@vmi/mssql-1 -n default -i /tmp/mssql-ssh/mssql-ssh-<runid>/id_ed25519
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
| `CONFIG` | `/work/mssql-config.yaml` | Path to the YAML config file inside the container |
| `CREATE_VMS` | `false` | Provision Linux VMs before the test; leave host patterns empty |
| `CLEANUP` | `false` | Delete managed VMs after the test (default: keep) |
| `VMS` | `0` | Number of Linux VMs when `CREATE_VMS=true` |
| `VM_PREFIX` | `mssql` | Name prefix → `mssql-1..N` |
| `VM_CPU` / `VM_MEMORY` | `2` / `4Gi` | Guest size |
| `VM_IMAGE` | `quay.io/containerdisks/fedora:41` | Registry containerdisk |
| `STORAGE_CLASS` | -- | **Required** when `CREATE_VMS=true` |
| `ROOT_VOLUME_SIZE` / `DATA_DISK_SIZE` | `20Gi` / `40Gi` | Root + blank data disk |
| `SSH_KEY_PATH` | `/tmp/mssql-ssh-<runid>` | In-container ephemeral keypair directory |
| `HOSTS` | -- | Space-separated host names (pre-created path) |
| `HOST_PATTERN` | -- | Brace-expansion pattern, e.g. `mssql-{1..10}` (ignored if `CREATE_VMS`) |
| `HOST_LABELS` | -- | OCP label selector for VMs (ignored if `CREATE_VMS`) |
| `NAMESPACE` | `default` | OpenShift namespace (must already exist for create-vms) |
| `DESCRIPTION` | -- | Test run description |
| `DISK_LIST` | `/dev/vdc` | Block device for MSSQL data (`vdb` when create-vms) |
| `MOUNT_POINT` | -- | Mount point (if set, `DISK_LIST` is ignored) |
| `PERSISTENT` | -- | Set to `true` for persistent mounts via /etc/fstab |
| `WAREHOUSE_COUNT` | `50` | TPCC warehouse count |
| `BUILD_USERS` | `50` | Virtual users for schema build step |
| `TEST_DURATION` | `15` | Test duration in minutes |
| `USER_COUNT` | `1` | Space-separated user counts (e.g. `"1 10 20 50"`) |
| `ITERATIONS` | `1` | Repeat full `user_count` sweep N times (`*_run1`…`*_runN`) |
| `ITERATION_SLEEP` | `60` | Seconds between sweep iterations (`0` = off) |
| `VU_SLEEP` | `30` | Seconds between VU levels within an iteration (`0` = off) |
| `MSSQL_PASS` | `mssqlpasswd1!` | SA password for sqlcmd/ODBC |
| `MAX_SERVER_MEMORY_MB` | -- | Optional SQL Server `max server memory` cap in MB (e.g. `38000` on a ~50GB VM). Omit/empty = unlimited. Minimum `2048`. Set via `sp_configure` (no restart). |
| `REBUILDDB` | `true` | Build database before tests (true/false) |
| `REBUILD_ONLY` | `false` | Only rebuild database, skip tests (true/false) |
| `TEST_ONLY` | `false` | Only run tests on existing database (true/false) |
| `HAMMERDB_SOURCE` | `bundled` | Script source: `bundled` (from image) or `remote_git` (clone on each VM) |
| `HAMMERDB_BUNDLED_PATH` | `/work/hammerdb-bundled` | Path to bundled scripts inside the container |
| `HAMMERDB_PATH` | `/root/hammerdb-tpcc-wrapper-scripts` | Deploy path on remote hosts |
| `HAMMERDB_REPO` | `https://github.com/ekuric/ioscale.git` | Only used when `HAMMERDB_SOURCE=remote_git` |
| `MIGRATE_USER_COUNTS` | -- | User counts that trigger VM migration (e.g. `"4 8"`) |
| `MIGRATE_INTERVAL` | `0` | Seconds between migrations (0 = parallel) |
| `RETRY_INTERVAL` | `30` | Retry interval in seconds |
| `MAX_RETRIES` | `10` | Maximum retry attempts |
| `MONITOR_INTERVAL` | `60` | Task monitor interval in seconds |
| `MONITOR_VM` | `false` | Log VM node placement during tests |
| `KUBEADMIN_PASSWORD` | -- | If set, runs `oc login` before the test |
| `API_URL` | `https://api.ocp.example.com:6443` | Cluster API URL for `oc login` |

### Optional SQL Server max memory

Cap the SQL Server buffer pool so HammerDB and the OS keep RAM:

**YAML** (`database` section):
```yaml
database:
  max_server_memory_mb: 38000   # MB; omit = unlimited; min 2048
```

**Env / Jenkins:** `-e MAX_SERVER_MEMORY_MB=38000` (parameter `MAX_SERVER_MEMORY_MB`).

Applied after MSSQL setup with `sp_configure 'max server memory'` (no restart).

## mssqldb.py CLI Parameters

The entrypoint passes `--virtctl-only` automatically. Any extra arguments
after the image name are forwarded to `mssqldb.py`.

### `--dry-run`

Validate configuration and show what would be done without executing.

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./mssql-config.yaml:/work/mssql-config.yaml \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqllin-benchmark:latest \
  --dry-run
```

Create-vms dry-run (validates provision args; does not create VMs):

```bash
podman run --rm --init --pids-limit=-1 \
  -e CREATE_VMS=true \
  -e VMS=3 \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e NAMESPACE=default \
  -v /root/.kube:/root/.kube:ro,Z \
  --privileged \
  quay.io/ekuric/mssqllin-benchmark:latest \
  --dry-run
```

### `-v`, `--verbose`

Enable verbose/debug output.

### `--prepare-hosts`

Only install packages, clone repo, and set up MSSQL Server. Skip testing.

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="mssql-{1..10}" \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqllin-benchmark:latest \
  --prepare-hosts
```

### `--copy-results`

Only copy results from hosts (skip everything else).

```bash
podman run --rm --init --pids-limit=-1 \
  -v ./mssql-config.yaml:/work/mssql-config.yaml \
  -v /root/mssqldb-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqllin-benchmark:latest \
  --copy-results
```

### `--create-vms` / `--cleanup`

Provision managed Linux VMs before the test; optionally delete them afterward.
Prefer env vars `CREATE_VMS` / `CLEANUP` when using the container entrypoint.

### `--iterations` / `--vu-sleep` / `--iteration-sleep`

Repeat the full `user_count` list N times (same as mssqlwin). Result files use
`*_runN.out` (e.g. `test_mssql_…_10_run2.out`). Defaults: 60s between sweeps,
30s between VU levels; pass `0` to disable.

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="mssql-{1..3}" \
  -e USER_COUNT="1 10" \
  -e ITERATIONS=2 \
  -e VU_SLEEP=0 \
  -e ITERATION_SLEEP=0 \
  ...
  quay.io/ekuric/mssqllin-benchmark:latest
# or:  ... latest --iterations 2 --vu-sleep 0 --iteration-sleep 0
```

### `--ssh-only`

Force plain SSH (overrides the default `--virtctl-only`). Incompatible with `--create-vms`.

## Usage Examples

### Env vars only (all parameters) — pre-created hosts

```bash
podman run --rm --init --pids-limit=-1 \
  -e HOST_PATTERN="mssql-{1..10}" \
  -e NAMESPACE="default" \
  -e DESCRIPTION="mssql linux perf test" \
  -e DISK_LIST="/dev/vdc" \
  -e PERSISTENT="" \
  -e WAREHOUSE_COUNT=50 \
  -e BUILD_USERS=50 \
  -e TEST_DURATION=15 \
  -e USER_COUNT="1 10 20 50" \
  -e MSSQL_PASS="mssqlpasswd1!" \
  -e MAX_SERVER_MEMORY_MB=38000 \
  -e REBUILDDB=true \
  -e REBUILD_ONLY=false \
  -e TEST_ONLY=false \
  -e HAMMERDB_SOURCE=bundled \
  -e HAMMERDB_BUNDLED_PATH="/work/hammerdb-bundled" \
  -e HAMMERDB_PATH="/root/hammerdb-tpcc-wrapper-scripts" \
  -e RETRY_INTERVAL=30 \
  -e MAX_RETRIES=10 \
  -e MONITOR_INTERVAL=60 \
  -e MIGRATE_USER_COUNTS="4 8" \
  -e MIGRATE_INTERVAL=0 \
  -v /root/mssqldb-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqllin-benchmark:latest
```

### Create-vms and keep guests

```bash
mkdir -p /tmp/mssql-ssh /root/mssqldb-results
podman run --rm --init --pids-limit=-1 \
  -e CREATE_VMS=true \
  -e VMS=3 \
  -e VM_PREFIX=mssql \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e NAMESPACE=default \
  -e DESCRIPTION="mssql-create-vms-keep" \
  -e WAREHOUSE_COUNT=50 \
  -e BUILD_USERS=50 \
  -e TEST_DURATION=15 \
  -e USER_COUNT="1 10" \
  -e MSSQL_PASS="mssqlpasswd1!" \
  -e CLEANUP=false \
  -v /root/mssqldb-results:/work/results:Z \
  -v /root/.kube:/root/.kube:ro,Z \
  -v /tmp/mssql-ssh:/host-tmp:Z \
  --privileged \
  quay.io/ekuric/mssqllin-benchmark:latest
```

**Jenkins:** use the repo `Jenkinsfile` — set `CREATE_VMS=true`, `VMS`, `STORAGE_CLASS`,
leave `HOST_PATTERN` empty; or `CREATE_VMS=false` + `HOST_PATTERN` for pre-created VMs.
Pipeline mounts `/tmp/mssql-ssh:/host-tmp:Z`.

## VM Live Migration During Tests

The script can live-migrate OpenShift/KubeVirt VMs **while HammerDB tests are
running** to measure the impact of VM migration on MSSQL Server performance.
Migration is triggered by **user count** -- specific test iterations where
migration should occur.

### Configuration

```yaml
migrate:
  user_counts: "4 8 16"   # user counts that trigger migration
  interval: 0             # 0 = parallel, >0 = sequential with delay (seconds)
```

Migration occurs at the **midpoint of the actual test runtime, after the rampup
period**:

```
migration_time = rampup_time + (test_duration / 2)
```

### Env var mode

```bash
-e MIGRATE_USER_COUNTS="4 8 16" \
-e MIGRATE_INTERVAL=2
```

## Difference from mssqlwin.py

| | mssqldb.py (this) | mssqlwin.py |
|---|---|---|
| Target OS | Linux | Windows |
| SSH user | root | Administrator |
| DB install | Script installs MSSQL on Linux | Pre-installed on Windows |
| CREATE_VMS | Linux DV + blank data disk | Windows VolumeSnapshot / golden snap |
| PerfMon | No | Yes (`perfmon.py`) |
| HammerDB | Installed on remote hosts | Pre-installed on Windows |

## SSH Key

**Pre-created hosts** (`CREATE_VMS=false`): `virtctl ssh` uses `/root/.ssh/id_rsa`.
Mount your private key:

```bash
-v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro
```

**On-demand VMs** (`CREATE_VMS=true`): an ephemeral ed25519 keypair is generated
and mirrored to the bastion via `/host-tmp` → `/tmp/mssql-ssh/`. The container’s
`virtctl ssh` / `scp` use that key (`-i`) automatically — bastion access with the
mirrored key works the same way.

| Location | Path | Lifetime |
|---|---|---|
| In-container | `/tmp/mssql-ssh-<runid>/id_ed25519` | Removed when the container exits |
| Bastion mirror | `/tmp/mssql-ssh/mssql-ssh-<runid>/id_ed25519` | Kept if VMs are kept; removed with `--cleanup` |

## Container Layout

```
/work/
  mssqldb.py                  # thin shim → mssql_tests.cli.main
  mssql_tests/                # package (config, provision, build, workload, …)
  templates/                  # reference linux-source-dv.yaml / linux-vm.yaml
  entrypoint.sh
  hammerdb-bundled/           # ioscale templates/mssql, baked at image build
  mssql-config.yaml           # mounted by user, or generated from env vars
  examples/
    mssql-config.yaml
  results/                    # output directory
```

## Notes

- If a config file is mounted, it is used as-is. If not, env vars generate one. No merging.
- `--privileged` is needed for the virtctl SSH proxy to work.
- `/root/.kube/config` mount gives the container access to the OCP cluster.
- Mount your SSH private key to `/root/.ssh/id_rsa` so `virtctl ssh` can authenticate (pre-created path).
- For create-vms also mount `/tmp/mssql-ssh:/host-tmp:Z`.
- The entrypoint always prints the config before running, so you can see exactly what values are used.
- `mssql_pass` is required for MSSQL Server SA authentication via sqlcmd/ODBC.
- Optional `MAX_SERVER_MEMORY_MB` / YAML `database.max_server_memory_mb` sets SQL Server `max server memory` (MB) on all hosts after setup via `sp_configure` (no restart). Omit for unlimited. Use a value below VM RAM so HammerDB and the OS keep headroom (e.g. `38000` on a ~50GB VM). Minimum `2048`.
- Jenkins: set `CREATE_VMS=true`, `VMS`, `STORAGE_CLASS`, leave `HOST_PATTERN` empty; or keep `CREATE_VMS=false` + `HOST_PATTERN` for the pre-created path.
