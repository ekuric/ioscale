# postgresql.py Container

Run HammerDB TPCC tests against PostgreSQL on Linux VMs from a container.
Configuration can be provided by mounting a YAML config file or by setting
environment variables.

## Building

```bash
cd db/postgresql/postgresql-container
podman build -t quay.io/ekuric/postgresql-benchmark:latest .
```

Override the virtctl version at build time:

```bash
podman build --build-arg VIRTCTL_VERSION=v1.8.0 -t quay.io/ekuric/postgresql-benchmark:latest .
```

## Two Usage Modes

### Mode 1: Config file (recommended)

Mount your `postgresql-config.yaml`. Everything comes from the file.

```bash
podman run --rm \
  -v ./postgresql-config.yaml:/work/postgresql-config.yaml \
  -v /root/postgresql-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest
```

Or point to the baked-in example config:

```bash
podman run --rm \
  -e CONFIG=/work/examples/postgresql-config.yaml \
  -v /root/postgresql-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest
```

### Mode 2: Env vars only (CI/CD)

No config file needed. The entrypoint generates it from env vars.
At minimum, one host selection variable is required.

```bash
podman run --rm \
  -e HOST_PATTERN="pg-{1..10}" \
  -e NAMESPACE="default" \
  -e DESCRIPTION="postgresql perf test" \
  -e DISK_LIST="/dev/vdc" \
  -e WAREHOUSE_COUNT=50 \
  -e TEST_DURATION=15 \
  -e USER_COUNT="1 10 20 50" \
  -e HAMMERDB_REPO="https://github.com/ekuric/fusion-access.git" \
  -e HAMMERDB_PATH="/root/hammerdb-tpcc-wrapper-scripts" \
  -e HAMMERDB_INSTALL_DIR="/usr/local/HammerDB" \
  -v /root/postgresql-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest
```

**How it works:** If a config file exists at the `CONFIG` path, it is used
as-is. If no file is found, the entrypoint generates one from env vars.
No merging or patching -- it is one or the other.

**Important:** Mount `/work/results` to a host directory to keep results after
the container exits. Without this mount and with `--rm`, results are lost.

## Environment Variables

Env vars are used when no config file is mounted (Mode 2).
`CONFIG`, `KUBEADMIN_PASSWORD`, and `API_URL` apply to both modes.

| Variable | Default | Description |
|---|---|---|
| `CONFIG` | `/work/postgresql-config.yaml` | Path to the YAML config file inside the container |
| `HOSTS` | -- | Space-separated host names (pick one of HOSTS / HOST_PATTERN / HOST_LABELS) |
| `HOST_PATTERN` | -- | Brace-expansion pattern, e.g. `pg-{1..10}` |
| `HOST_LABELS` | -- | OCP label selector for VMs |
| `NAMESPACE` | `default` | OpenShift namespace |
| `DESCRIPTION` | -- | Test run description |
| `DISK_LIST` | `/dev/vdc` | Block device for PostgreSQL data |
| `MOUNT_POINT` | -- | Mount point (if set, `DISK_LIST` is ignored) |
| `PERSISTENT` | -- | Set to `true` for persistent mounts via /etc/fstab |
| `WAREHOUSE_COUNT` | `50` | TPCC warehouse count |
| `TEST_DURATION` | `15` | Test duration in minutes |
| `USER_COUNT` | `1` | Space-separated user counts (e.g. `"1 10 20 50"`) |
| `HAMMERDB_REPO` | `https://github.com/ekuric/fusion-access.git` | Git repo with HammerDB wrapper scripts |
| `HAMMERDB_PATH` | `/root/hammerdb-tpcc-wrapper-scripts` | Clone path on remote hosts |
| `HAMMERDB_INSTALL_DIR` | `/usr/local/HammerDB` | HammerDB installation directory on remote hosts |
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
podman run --rm \
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
podman run --rm \
  -e HOST_PATTERN="pg-{1..10}" \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest \
  --prepare-hosts
```

### `--copy-results`

Only copy results from hosts (skip everything else).

```bash
podman run --rm \
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

### Config file only

```bash
podman run --rm \
  -v ./postgresql-config.yaml:/work/postgresql-config.yaml \
  -v /root/postgresql-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest
```

### Env vars only (all parameters)

```bash
podman run --rm \
  -e HOST_PATTERN="pg-{1..10}" \
  -e NAMESPACE="default" \
  -e DESCRIPTION="postgresql perf test" \
  -e DISK_LIST="/dev/vdc" \
  -e PERSISTENT="" \
  -e WAREHOUSE_COUNT=50 \
  -e TEST_DURATION=15 \
  -e USER_COUNT="1 10 20 50" \
  -e HAMMERDB_REPO="https://github.com/ekuric/fusion-access.git" \
  -e HAMMERDB_PATH="/root/hammerdb-tpcc-wrapper-scripts" \
  -e HAMMERDB_INSTALL_DIR="/usr/local/HammerDB" \
  -e RETRY_INTERVAL=30 \
  -e MAX_RETRIES=10 \
  -e MONITOR_INTERVAL=60 \
  -e MIGRATE_USER_COUNTS="4 8" \
  -e MIGRATE_INTERVAL=0 \
  -v /root/postgresql-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest
```

### Dry run with verbose output

```bash
podman run --rm \
  -e HOST_PATTERN="pg-{1..5}" \
  -e DISK_LIST="/dev/vdc" \
  -v /root/.kube/config:/root/.kube/config \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest \
  --dry-run --verbose
```

### With oc login

```bash
podman run --rm \
  -e KUBEADMIN_PASSWORD="my-password" \
  -e API_URL="https://api.mycluster.example.com:6443" \
  -v ./postgresql-config.yaml:/work/postgresql-config.yaml \
  -v /root/postgresql-results:/work/results \
  -v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  --privileged \
  quay.io/ekuric/postgresql-benchmark:latest
```

## SSH Key

`virtctl ssh` uses `/root/.ssh/id_rsa` inside the container by default. Mount
your private key:

```bash
-v /root/.ssh/id_rsa:/root/.ssh/id_rsa:ro
```

Or mount the entire `.ssh` directory:

```bash
-v /root/.ssh:/root/.ssh:ro
```

## Container Layout

```
/work/
  postgresql.py
  entrypoint.sh
  postgresql-config.yaml      (mounted by user, or generated from env vars)
  examples/
    postgresql-config.yaml    (baked-in reference config)
  results/                    (output directory)
```

## VM Live Migration During Tests

The script can live-migrate OpenShift/KubeVirt VMs **while HammerDB tests are
running** to measure the impact of VM migration on database performance. Unlike
the FIO container (which triggers migration by I/O pattern), PostgreSQL
migration is triggered by **user count** -- specific test iterations where
migration should occur.

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
- Mount your SSH private key to `/root/.ssh/id_rsa` so `virtctl ssh` can authenticate.
- Templates are not baked in -- they come from the git repo cloned on remote hosts at runtime.
- The entrypoint always prints the config before running, so you can see exactly what values are used.
