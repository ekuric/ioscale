# mssqlwin-container (modular `mssqlwin_tests`)

Run HammerDB TPCC tests against Windows MSSQL VMs from a container.
Template files are baked in. Configuration can be provided three ways:
mount a config file, use environment variables, or both.

Code layout matches MariaDB/PostgreSQL containers:

```text
mssqlwin.py                 # thin shim → python3 -m mssqlwin_tests
mssqlwin_tests/
  cli.py, config.py, executor.py, provision.py, storage.py,
  service.py, build.py, workload.py, results.py, migration.py,
  perfmon.py, …
perfmon-counters-default.txt   # default logman counter list
```

## Building

```bash
cd mssqlwin-container
podman build -t quay.io/ekuric/mssqlwin-benchmark:latest .
```

Override the virtctl version at build time:

```bash
podman build --build-arg VIRTCTL_VERSION=v1.8.0 -t quay.io/ekuric/mssqlwin-benchmark:latest .
```

## Two Usage Modes

### Mode 1: Config file (recommended)

Mount your `mssql-configwin.yaml`. Everything comes from the file.

```bash
podman run --rm --pids-limit=-1 \
  -v ./mssql-configwin.yaml:/work/mssql-configwin.yaml \
  -v /root/mssqlwin-results:/work/results \
  -v /root/.ssh:/root/.ssh:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest
```

### Mode 2: Env vars only (CI/CD)

No config file needed. The entrypoint generates it from env vars.
At minimum, one host selection variable is required (`HOSTS`, `HOST_PATTERN`,
`HOST_LABELS`, or `PIN_NODES`).

Working example (single Windows VM):

```bash
podman run --rm --pids-limit=-1 \
  -e HOST_PATTERN="winm-1" \
  -e NAMESPACE="default" \
  -e WAREHOUSE_COUNT=100 \
  -e BUILD_USERS=10 \
  -e USER_COUNT="1" \
  -e TEST_DURATION=1 \
  -e MSSQL_TOTAL_ITERATIONS=10000000 \
  -e RAMPUP_TIME="1" \
  -e MSSQL_PASS="YourSaPassword" \
  -e MAX_SERVER_MEMORY_MB=38000 \
  -e DISK_ID="1" \
  -e REBUILDDB=true \
  -e REBUILD_ALWAYS=false \
  -e TEST_ONLY=false \
  -e DESCRIPTION="jenkins-mssql-test-new-db-test" \
  -v /root/mssqlwin-results:/work/results \
  -v /root/.ssh:/root/.ssh:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest
```

Multi-VM pattern example:

```bash
podman run --rm --pids-limit=-1 \
  -e HOST_PATTERN="winm-{1..10}" \
  -e NAMESPACE="default" \
  -e WAREHOUSE_COUNT=100 \
  -e BUILD_USERS=10 \
  -e USER_COUNT="1 10" \
  -e TEST_DURATION=15 \
  -e RAMPUP_TIME="1" \
  -e MSSQL_PASS="YourSaPassword" \
  -e MAX_SERVER_MEMORY_MB=38000 \
  -e DISK_ID="1" \
  -e REBUILDDB=true \
  -e REBUILD_ALWAYS=false \
  -e TEST_ONLY=false \
  -e DESCRIPTION="mssqlwin-scale-test" \
  -v /root/mssqlwin-results:/work/results \
  -v /root/.ssh:/root/.ssh:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest
```

**How it works:** If a config file exists at the `CONFIG` path, it is used
as-is. If no file is found, the entrypoint generates one from env vars.
No merging or patching -- it is one or the other.

**Important:**
- Use `--pids-limit=-1` (HammerDB / nested tools can exceed the default PID limit).
- Mount `/work/results` to a host directory (e.g. `/root/mssqlwin-results`) to keep
  results after the container exits. Without this mount and with `--rm`, results are lost.
- Mount the full SSH directory (`-v /root/.ssh:/root/.ssh:ro`) so `virtctl ssh` can
  authenticate to Windows VMs (pre-created hosts). With `CREATE_VMS=true`, an ephemeral
  key is injected via Cloudbase-Init; optionally mirror it with
  `-v /tmp/mssqlwin-ssh:/host-tmp:Z`.
- Set `DISK_ID` to the Windows data disk number (`Get-Disk` on the guest). Often `1`
  (OS is disk `0`); some images with an extra device use `2`.
- For TPCC on the OS disk instead of D:, set `USE_OSDISK=true` or pass `--osdisk`
  (uses `create_dbC.sql` → `C:\mssql\data`; skips data-disk format). Ensure C: has
  enough free space for warehouse-scaled MDF/LDF sizes.

## On-demand Windows VMs (`--create-vms` / `CREATE_VMS=true`)

Same idea as fio-windows / `winCdisk --golden-snapshot`: **VolumeSnapshot → clone root DV**.

1. Wipe prior managed resources labeled `mssqlwin-container.ioscale/*`
2. Build a golden OS VolumeSnapshot via **one of**:
   - `WIN_VM_IMAGE` — import source DataVolume from HTTP qcow2, then snapshot its PVC
   - `GOLDEN_SNAPSHOT` — use an existing ready VolumeSnapshot
   - `ORIG_VM` — snapshot the root PVC of an existing Windows VM
3. Create `VMS` VirtualMachines; each root disk is a DataVolume **cloned from that snapshot**
4. Optional blank data disk (unless `USE_OSDISK=true`)
5. Inject SSH via Cloudbase-Init Secret (`#ps1_sysnative`), wait Ready + SSH
6. Run prepare → HammerDB as usual (`db_hosts` filled automatically)

Example (import image → snapshot → 3 clones):

```bash
podman run --rm --pids-limit=-1 \
  -e CREATE_VMS=true \
  -e VMS=3 \
  -e NAMESPACE=default \
  -e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization \
  -e WIN_VM_IMAGE="https://example.com/win2k25-mssql.qcow2" \
  -e WIN_ROOT_SIZE=100Gi \
  -e WIN_DATA_SIZE=40Gi \
  -e VM_CPU=8 \
  -e VM_MEMORY=16Gi \
  -e WAREHOUSE_COUNT=100 \
  -e USER_COUNT="1 10" \
  -e MSSQL_PASS="YourSaPassword" \
  -e DESCRIPTION="mssqlwin-create-vms" \
  -v /root/mssqlwin-results:/work/results \
  -v /tmp/mssqlwin-ssh:/host-tmp:Z \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest
```

Example (clone from existing snapshot):

```bash
-e CREATE_VMS=true -e VMS=5 \
-e GOLDEN_SNAPSHOT=windv-snap \
-e STORAGE_CLASS=ocs-storagecluster-ceph-rbd-virtualization
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CONFIG` | `/work/mssql-configwin.yaml` | Path to the YAML config file inside the container |
| `HOSTS` | -- | Space-separated VM names (pick one of HOSTS / HOST_PATTERN / HOST_LABELS / PIN_NODES) |
| `HOST_PATTERN` | -- | Brace-expansion pattern, e.g. `winm-{1..10}` or a single name `winm-1` |
| `HOST_LABELS` | -- | OCP label selector for VMs |
| `PIN_NODES` | -- | Alias for HOST_LABELS |
| `NAMESPACE` | `default` | OpenShift namespace |
| `WAREHOUSE_COUNT` | `50` | TPCC warehouse count |
| `BUILD_USERS` | `50` | Virtual users for schema build |
| `USER_COUNT` | `1 10 20 50 100` | Space-separated user counts (e.g. `"1 10"`, not comma-separated) |
| `TEST_DURATION` | `15` | Test duration in minutes |
| `RAMPUP_TIME` | -- | Ramp-up time in minutes |
| `MSSQL_TOTAL_ITERATIONS` | `10000000` | HammerDB per-VU transaction cap (not sweep count) |
| `ITERATIONS` | `1` | Repeat full `user_count` sweep N times (`*_run1`…`*_runN` via `HAMMERDB_RUN`) |
| `ITERATION_SLEEP` | `60` | Seconds between sweep iterations (`0` = off) |
| `VU_SLEEP` | `30` | Seconds between VU levels within an iteration (`0` = off) |
| `MAX_PARALLEL` | `0` | Max concurrent hosts (`0`/unset = all hosts; positive N = cap) |
| `MSSQL_PASS` | -- | MSSQL server password |
| `MAX_SERVER_MEMORY_MB` | -- | Optional SQL Server `max server memory` cap in MB (e.g. `38000` on a ~50GB VM). Omit/empty = unlimited. Minimum `2048`. Set via `sp_configure` (no restart). |
| `HAMMERDB_PATH` | `C:\tools\Hammerdb-4.12` | HammerDB install path on Windows |
| `DISK_ID` | `1` | Data disk ID to format (`Get-Disk` number); ignored when `USE_OSDISK=true` |
| `USE_OSDISK` | `false` | `true` = TPCC on `C:\mssql\data` (skip D: provision/copy; uses `create_dbC.sql`) |
| `SSH_USER` | `Administrator` | SSH user on Windows VMs |
| `REBUILDDB` | `true` | Rebuild database before test |
| `REBUILD_ONLY` | `false` | Only rebuild, skip test |
| `TEST_ONLY` | `false` | Skip prepare/format and rebuild; run tests only (reuse existing TPCC DB) |
| `REBUILD_ALWAYS` | `false` | Rebuild before every user-count iteration |
| `PERFMON` | `false` | Enable Windows PerfMon collectors during the test (`--perfmon`) |
| `PERFMON_COUNTERS_FILE` | image default | Override counter `.txt` / logman `.xml` path |
| `PERFMON_SAMPLE_INTERVAL` | `5` | Sample interval seconds |
| `PERFMON_UNTIL_ITERATION` | -- | Stop collectors after sweep iteration N (empty = all) |
| `PERFMON_NO_EXPORT_CSV` | `false` | Skip post-test `relog` BLG→CSV |
| `DESCRIPTION` | -- | Test run description |
| `KUBEADMIN_PASSWORD` | -- | If set, runs `oc login` before the test |
| `API_URL` | `https://api.ocp.example.com:6443` | Cluster API URL for `oc login` |
| `CREATE_VMS` | `false` | Provision Windows VMs (snapshot → clone) before the test |
| `CLEANUP` | `false` | Delete managed VMs after the test |
| `VMS` | `1` | Number of Windows VMs when `CREATE_VMS=true` |
| `VM_PREFIX` | `mssql-win` | VM name prefix (`mssql-win-1` …) |
| `VM_CPU` / `VM_MEMORY` | `8` / `16Gi` | Guest size |
| `STORAGE_CLASS` | -- | Required with `CREATE_VMS` |
| `ACCESS_MODE` | `ReadWriteMany` | PVC access mode |
| `WIN_VM_IMAGE` | -- | HTTP qcow2 URL → source DV → snapshot |
| `GOLDEN_SNAPSHOT` | -- | Existing VolumeSnapshot name (skip import) |
| `ORIG_VM` | -- | Snapshot this VM’s root PVC, then clone |
| `SNAPSHOT_CLASS` | auto | VolumeSnapshotClass override |
| `WIN_ROOT_SIZE` / `WIN_DATA_SIZE` | `100Gi` / `40Gi` | Disk sizes |

### Optional SQL Server max memory

Cap the SQL Server buffer pool so HammerDB and Windows keep RAM:

**YAML** (`database` section):
```yaml
database:
  max_server_memory_mb: 38000   # MB; omit = unlimited; min 2048
```

**Env / Jenkins:** `-e MAX_SERVER_MEMORY_MB=38000` (parameter `MAX_SERVER_MEMORY_MB`).

Applied after disk prepare and before rebuild/tests with `sp_configure 'max server memory'` (no restart). Uses Windows auth (`sqlcmd -E`) by default, or SA when `mssql_pass` / `MSSQL_PASS` is set. Leave more OS headroom on Windows than on Linux (typically 15–25% of RAM).

## Config File Reference

Edit `mssql-configwin.yaml` to configure your test. A copy is baked into the
image at `/work/examples/mssql-configwin.yaml`. Key sections:

```yaml
description: "mssqlwin-example"

database:
  host_pattern: "winm-{1..10}"   # or hosts, host_labels, host_file
  namespace: "default"
  warehouse_count: 100
  build_users: 10
  user_count: "1 10"             # space-separated, NOT comma-separated
  test_duration: 15
  rampup_time: 1
  mssql_total_iterations: 10000000
  iterations: 1                  # full user_count sweeps (*_runN)
  iteration_sleep_sec: 60        # between sweeps (0=off)
  vu_sleep_sec: 30               # between VU levels (0=off)
  mssql_pass: "YourSaPassword"
  # max_server_memory_mb: 38000  # optional; omit = unlimited

# max_parallel_workers: 0        # 0/omit = all hosts; positive N = cap

windows:
  hammerdb_path: "C:\\tools\\Hammerdb-4.12"
  disk_id: "1"
  ssh_user: "Administrator"
  rebuilddb: true
  rebuild_always: false
  rebuild_only: false
  test_only: false               # true: skip prepare/format + rebuild

# perfmon:
#   enabled: true
#   sample_interval: 5
#   counters_file: /work/perfmon-counters-default.txt
#   until_iteration: 1           # stop after sweep N (needs iterations > 1)
#   export_csv: true
```

See the full example for all available fields including timeouts and
`mssql_service_name`.

## mssqlwin.py CLI Parameters

The entrypoint passes `--virtctl-only` and the template file paths automatically.
Any extra arguments after the image name are forwarded to `mssqlwin.py`.

### `-v`, `--verbose`

Enable verbose/debug output.

```bash
podman run --rm --pids-limit=-1 \
  -v ./mssql-configwin.yaml:/work/mssql-configwin.yaml \
  -v /root/mssqlwin-results:/work/results \
  -v /root/.ssh:/root/.ssh:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest \
  --verbose
```

### `--dry-run`

Validate configuration and show what would be done without executing.

```bash
podman run --rm --pids-limit=-1 \
  -v ./mssql-configwin.yaml:/work/mssql-configwin.yaml \
  -v /root/.ssh:/root/.ssh:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest \
  --dry-run
```

### `--copy-results`

Only copy results from hosts (skip prepare/format, rebuild, and tests).

```bash
podman run --rm --pids-limit=-1 \
  -v ./mssql-configwin.yaml:/work/mssql-configwin.yaml \
  -v /root/mssqlwin-results:/work/results \
  -v /root/.ssh:/root/.ssh:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest \
  --copy-results
```

### `--ssh-only`

Force plain SSH for all hosts (overrides the default `--virtctl-only`).

```bash
podman run --rm --pids-limit=-1 \
  -v ./mssql-configwin.yaml:/work/mssql-configwin.yaml \
  -v /root/.ssh:/root/.ssh:ro \
  quay.io/ekuric/mssqlwin-benchmark:latest \
  --ssh-only
```

### `--generate-only`

Only generate per-user config files locally and exit (no prepare/format, no SSH, no tests).

```bash
podman run --rm --pids-limit=-1 \
  -v ./mssql-configwin.yaml:/work/mssql-configwin.yaml \
  -v /root/mssqlwin-results:/work/results \
  quay.io/ekuric/mssqlwin-benchmark:latest \
  --generate-only
```

### `--rebuild-always`

Rebuild the database before every user-count iteration (not just once).

```bash
podman run --rm --pids-limit=-1 \
  -v ./mssql-configwin.yaml:/work/mssql-configwin.yaml \
  -v /root/mssqlwin-results:/work/results \
  -v /root/.ssh:/root/.ssh:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest \
  --rebuild-always
```

### `--prepare-machine`

Prepare Windows machines by formatting the data disk and exit (no tests).
Note: a normal full run already prepares disks before rebuild/test.
`--copy-results`, `--generate-only`, and `windows.test_only` / `TEST_ONLY=true` never call prepare/format.

```bash
podman run --rm --pids-limit=-1 \
  -v ./mssql-configwin.yaml:/work/mssql-configwin.yaml \
  -v /root/.ssh:/root/.ssh:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest \
  --prepare-machine
```

### `--iterations` / `--vu-sleep` / `--iteration-sleep`

Repeat the full `user_count` list N times. Result files use `HAMMERDB_RUN`
(`mssqls_tprocc_010vu_run1.*`, `*_run2.*`, …). Sleeps default to 30s between VU
levels and 60s between sweeps; pass `0` to disable.

```bash
podman run --rm --pids-limit=-1 \
  -e HOST_PATTERN="winm-1" \
  -e USER_COUNT="1 10" \
  -e ITERATIONS=2 \
  -e VU_SLEEP=0 \
  -e ITERATION_SLEEP=0 \
  ...
  quay.io/ekuric/mssqlwin-benchmark:latest
# or:  ... latest --iterations 2 --vu-sleep 0 --iteration-sleep 0
```

### `--max-parallel`

Cap concurrent host workers for rebuild/test/collect. Default / `0` = all hosts
(no hard 50 ceiling). Example: `--max-parallel 2` or `-e MAX_PARALLEL=2`.

### `--perfmon`

Start Windows PerfMon (`logman`) collectors during the test; BLG under
`windows.result_dir`, optional `relog` → `perfmon.csv` after stop.
Also: `PERFMON=true`, YAML `perfmon.enabled`, `--perfmon-until-iteration N`.

## Usage Examples

### Config file only

```bash
podman run --rm --pids-limit=-1 \
  -v ./mssql-configwin.yaml:/work/mssql-configwin.yaml \
  -v /root/mssqlwin-results:/work/results \
  -v /root/.ssh:/root/.ssh:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest
```

### Env vars only (known-good CLI)

```bash
podman run --rm --pids-limit=-1 \
  -e HOST_PATTERN="winm-1" \
  -e NAMESPACE="default" \
  -e WAREHOUSE_COUNT=100 \
  -e BUILD_USERS=10 \
  -e USER_COUNT="1" \
  -e TEST_DURATION=1 \
  -e MSSQL_TOTAL_ITERATIONS=10000000 \
  -e RAMPUP_TIME="1" \
  -e MSSQL_PASS="YourSaPassword" \
  -e DISK_ID="1" \
  -e REBUILDDB=true \
  -e REBUILD_ALWAYS=false \
  -e TEST_ONLY=false \
  -e DESCRIPTION="jenkins-mssql-test-new-db-test" \
  -v /root/mssqlwin-results:/work/results \
  -v /root/.ssh:/root/.ssh:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest
```

### Dry run with verbose output

```bash
podman run --rm --pids-limit=-1 \
  -v ./mssql-configwin.yaml:/work/mssql-configwin.yaml \
  -v /root/.ssh:/root/.ssh:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest \
  --dry-run --verbose
```

### Override template files at runtime

Mount your own files over the baked-in defaults:

```bash
podman run --rm --pids-limit=-1 \
  -v ./mssql-configwin.yaml:/work/mssql-configwin.yaml \
  -v ./my-rebuild.ps1:/work/templates/rebuild-db.ps1 \
  -v ./my-create-db.sql:/work/templates/create_db.sql \
  -v /root/mssqlwin-results:/work/results \
  -v /root/.ssh:/root/.ssh:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest
```

### With oc login

```bash
podman run --rm --pids-limit=-1 \
  -e KUBEADMIN_PASSWORD="my-password" \
  -e API_URL="https://api.mycluster.example.com:6443" \
  -e HOST_PATTERN="winm-1" \
  -e NAMESPACE="default" \
  -v /root/mssqlwin-results:/work/results \
  -v /root/.ssh:/root/.ssh:ro \
  --privileged \
  quay.io/ekuric/mssqlwin-benchmark:latest
```

## SSH Key

`virtctl ssh` uses keys under `/root/.ssh` inside the container. Preferred mount
(matches the working CLI / Jenkins style):

```bash
-v /root/.ssh:/root/.ssh:ro
```

If you only mount a single key:

```bash
-v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro
```

If your key is at a non-standard path, mount it to the expected location:

```bash
-v /root/mno/my-key:/root/.ssh/id_rsa:ro
```

## Container Layout

```
/work/
  mssqlwin.py
  mssqlwin_tests/                (modular package)
  entrypoint.sh
  perfmon-counters-default.txt   (default logman counter list)
  examples/
    mssql-configwin.yaml         (baked-in reference config)
  mssql-configwin.yaml           (mounted by user, or generated from env)
  templates/
    hammerdb-sa-test.ps1         (uses $env:HAMMERDB_RUN for *_runN names)
    mssqls_tprocc_run.tcl
    mssqls_tprocc_buildschema.tcl
    rebuild-db.ps1
    create_db.sql                 (D:\mssql\data — default)
    create_dbC.sql                (C:\mssql\data — USE_OSDISK / --osdisk)
  results/                       (output directory)
```

## Notes

- If a config file is mounted, it is used as-is. If not, env vars generate one. No merging.
  CLI/env flags such as `--iterations` / `ITERATIONS` and `--perfmon` / `PERFMON` still apply on top.
- `--privileged` is needed for the virtctl SSH proxy to work.
- `/root/.kube/config` mount gives the container access to the OCP cluster.
- Mount `/root/.ssh` (or at least `id_rsa`) so `virtctl ssh` can authenticate.
- Default prepare formats the Windows data disk via `C:\tools\setup\provision-data-disk.ps1`
  using `DISK_ID`, then copies HammerDB tools to `D:` when present.
- `--copy-results`, `--generate-only`, and `TEST_ONLY` / `windows.test_only` never format disks.
- `USE_OSDISK=true` / `--osdisk` / YAML `windows.use_osdisk: true`: skip that prepare path;
  create `C:\mssql\data` and use `create_dbC.sql` (HammerDB stays under `HAMMERDB_PATH` on C:).
- Template files baked into `/work/templates/` at build time are silently overridden when you mount a file to the same path.
- `user_count` is space-separated (e.g. `"1 10 50 100"`), not comma-separated.
- `MSSQL_TOTAL_ITERATIONS` is the HammerDB transaction cap; `ITERATIONS` / `database.iterations` is how many times the VU list is repeated.
- The entrypoint always prints the config before running, so you can see exactly what values are used.
- Optional `MAX_SERVER_MEMORY_MB` / YAML `database.max_server_memory_mb` sets SQL Server `max server memory` (MB) on all hosts before rebuild/tests via `sp_configure` (no restart). Omit for unlimited. Minimum `2048`. Prefer leaving OS headroom on Windows (e.g. `38000` on a ~50GB VM).
- Jenkinsfile parameters map to the same env vars (`ITERATIONS`, `VU_SLEEP`, `ITERATION_SLEEP`, `MAX_PARALLEL`, `PERFMON`, …).
