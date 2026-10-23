"""Configuration loading for MSSQL Windows HammerDB tests."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from typing import Dict, List

import yaml

from mssqlwin_tests.constants import (
    DEFAULT_PREPARE_TIMEOUT,
    DEFAULT_SCP_TIMEOUT,
    DEFAULT_TEST_TIMEOUT,
    DEFAULT_TIMEOUT,
    LABEL_QUERY_TIMEOUT,
    OC_COMMAND_TIMEOUT,
    VM_LOOKUP_TIMEOUT,
)

logger = logging.getLogger("mssqlwin_tests")


class MSSQLWinConfigError(Exception):
    """Configuration / provision error for MSSQL Windows tests."""


class MSSQLWinConfig:
    """Configuration class for Windows MSSQL tests"""

    def __init__(self):
        self.config_file = "mssql-config.yaml"
        self.dry_run = False
        self.verbose = False
        self.use_virtctl = None  # None = auto-detect, True = force virtctl, False = force SSH
        self.namespace = None
        self.namespace_from_config = False
        self.db_hosts: List[str] = []
        self.warehouse_count = None
        self.build_users = None
        self.mssql_total_iterations = None
        self.user_count: List[str] = []
        # How many times to repeat the full user_count sweep (1 = single pass).
        # Distinct from mssql_total_iterations (HammerDB per-VU transaction cap).
        self.test_iterations = 1
        # Seconds to sleep after a full user_count iteration before the next one.
        # Skipped after the final iteration. 0 disables.
        self.iteration_sleep_sec = 60
        # Seconds to sleep between VU levels within an iteration (e.g. 10→20).
        # Skipped after the last VU in the list. 0 disables.
        self.vu_sleep_sec = 30
        self.test_duration = None
        self.rampup_time = None
        self.log_level = "INFO"
        self.description = ""
        self.copy_results = False
        self.windows_hammerdb_path = None
        self.windows_test_script = None
        self.windows_test_script_local = None
        self.windows_ssh_user = "Administrator"
        self.windows_rebuilddb = True
        self.windows_result_dir = None
        self.windows_rebuild_script = None
        self.windows_rebuild_script_local = None
        self.windows_create_db_sql = None
        self.windows_create_db_sql_local = None
        self.windows_build_schema_file = None
        self.windows_build_schema_file_local = None
        self.windows_hammerdb_test_script = None
        self.windows_hammerdb_test_script_local = None
        self.windows_rebuild_only = False
        self.windows_test_only = False
        self.windows_rebuild_always = False
        self.generate_only = False
        self.windows_mssql_pass = None
        self.max_server_memory_mb = None
        self.windows_disk_id = "1"
        self.windows_use_osdisk = False  # True: TPCC on C: (skip D: provision/copy)
        self.windows_rebuild_timeout = None
        self.windows_mssql_service_name = None
        self.timeout_default = DEFAULT_TIMEOUT
        self.timeout_scp = DEFAULT_SCP_TIMEOUT
        self.timeout_test = DEFAULT_TEST_TIMEOUT
        self.timeout_prepare = DEFAULT_PREPARE_TIMEOUT
        self.timeout_vm_lookup = VM_LOOKUP_TIMEOUT
        self.timeout_label_query = LABEL_QUERY_TIMEOUT
        self.timeout_oc_command = OC_COMMAND_TIMEOUT
        self.max_retries = 10
        self.retry_interval = 30
        self.monitor_vm = False
        self.monitor_vm_interval = 10
        self.perfmon_enabled = False
        self.perfmon_sample_interval = 5
        # Optional path to counter list (.txt) or logman template (.xml)
        self.perfmon_counters_file = None
        # Capture through sweep iteration N (inclusive). None = all iterations.
        self.perfmon_until_iteration = None
        self.perfmon_collector_name = "mssqlwin-perfmon"
        # After stop, run relog BLG→CSV on each guest (post-test only).
        self.perfmon_export_csv = True
        # True while collectors are expected to be running (avoids double-stop noise).
        self.perfmon_running = False
        # None or 0 = all db_hosts (no hard 50 ceiling). Positive int limits pool size.
        self.max_parallel_workers = None
        # --create-vms (Windows only; snapshot → clone DV)
        self.create_vms = False
        self.cleanup_vms = False
        self.vms_count = 0
        self.vm_prefix = "mssql-win"
        self.vm_cpu = "8"
        self.vm_memory = "16Gi"
        self.storage_class = None
        self.access_mode = "ReadWriteMany"
        self.win_vm_image = None
        self.win_root_volume_size = "100Gi"
        self.win_data_disk_size = "40Gi"
        self.golden_snapshot = None
        self.orig_vm = None
        self.snapshot_class = None
        self.provision_run_id = None
        self.ssh_key_path = None
        self.ssh_identity_file = None
        self.ssh_key_bastion_path = None


def pool_max_workers(config: MSSQLWinConfig) -> int:
    """Worker count for per-host pools (rebuild, test, collect).

    Unset/0 → len(db_hosts) with no historical min(..., 50) cap.
    Positive max_parallel_workers → min(hosts, cap).
    """
    n = len(config.db_hosts)
    if n <= 0:
        return 1
    cap = config.max_parallel_workers
    if cap is None or cap <= 0:
        return n
    return min(n, cap)


class ConfigLoader:
    """Loads and validates configuration from YAML file"""

    def __init__(self, config: MSSQLWinConfig):
        self.config = config

    def load_config(self) -> None:  # type: ignore[override]
        if not os.path.exists(self.config.config_file):
            logger.error(f"Configuration file '{self.config.config_file}' not found")
            sys.exit(1)

        with open(self.config.config_file, "r") as f:
            yaml_data = yaml.safe_load(f)

        database = yaml_data.get("database", {})
        namespace_value = database.get("namespace")
        if self.config.use_virtctl is not False:
            if namespace_value not in (None, "null", ""):
                self.config.namespace_from_config = True
                self.config.namespace = namespace_value
            else:
                self.config.namespace = "default"
            if not self.config.namespace:
                self.config.namespace = "default"
        else:
            self.config.namespace = "N/A"

        # create-vms / provision knobs (YAML provision: section + env filled by entrypoint)
        provision = yaml_data.get("provision", {}) or {}
        if provision.get("create_vms") in (True, "true", "True", 1, "1"):
            self.config.create_vms = True
        if provision.get("cleanup") in (True, "true", "True", 1, "1"):
            self.config.cleanup_vms = True
        if provision.get("vms") not in (None, "", "null"):
            self.config.vms_count = int(provision.get("vms"))
        if provision.get("vm_prefix"):
            self.config.vm_prefix = str(provision.get("vm_prefix"))
        if provision.get("vm_cpu"):
            self.config.vm_cpu = str(provision.get("vm_cpu"))
        if provision.get("vm_memory"):
            self.config.vm_memory = str(provision.get("vm_memory"))
        if provision.get("storage_class"):
            self.config.storage_class = str(provision.get("storage_class"))
        if provision.get("access_mode"):
            self.config.access_mode = str(provision.get("access_mode"))
        if provision.get("win_vm_image"):
            self.config.win_vm_image = str(provision.get("win_vm_image"))
        if provision.get("win_root_volume_size"):
            self.config.win_root_volume_size = str(provision.get("win_root_volume_size"))
        if provision.get("win_data_disk_size"):
            self.config.win_data_disk_size = str(provision.get("win_data_disk_size"))
        if provision.get("golden_snapshot"):
            self.config.golden_snapshot = str(provision.get("golden_snapshot"))
        if provision.get("orig_vm"):
            self.config.orig_vm = str(provision.get("orig_vm"))
        if provision.get("snapshot_class"):
            self.config.snapshot_class = str(provision.get("snapshot_class"))

        self.config.db_hosts = self._get_db_hosts(yaml_data)

        self.config.warehouse_count = database.get("warehouse_count")
        build_users = database.get("build_users")
        if build_users not in (None, "", "null"):
            self.config.build_users = str(build_users)
        self.config.mssql_total_iterations = database.get("mssql_total_iterations")
        # Sweep iterations (repeat full user_count list). Not mssql_total_iterations.
        raw_iters = database.get("iterations", database.get("test_iterations"))
        if raw_iters is not None and raw_iters != "null":
            try:
                self.config.test_iterations = max(1, int(raw_iters))
            except (TypeError, ValueError):
                logger.warning(
                    f"Invalid database.iterations={raw_iters!r}; using 1"
                )
                self.config.test_iterations = 1
        raw_sleep = database.get(
            "iteration_sleep_sec",
            database.get(
                "iteration_sleep",
                database.get(
                    "sweep_sleep_sec",  # backward-compatible alias
                    database.get("sweep_sleep", database.get("inter_sweep_sleep")),
                ),
            ),
        )
        if raw_sleep is not None and raw_sleep != "null":
            try:
                self.config.iteration_sleep_sec = max(0, int(raw_sleep))
            except (TypeError, ValueError):
                logger.warning(
                    f"Invalid database.iteration_sleep_sec={raw_sleep!r}; using "
                    f"{self.config.iteration_sleep_sec}"
                )
        raw_vu_sleep = database.get(
            "vu_sleep_sec",
            database.get("vu_sleep", database.get("user_count_sleep_sec")),
        )
        if raw_vu_sleep is not None and raw_vu_sleep != "null":
            try:
                self.config.vu_sleep_sec = max(0, int(raw_vu_sleep))
            except (TypeError, ValueError):
                logger.warning(
                    f"Invalid database.vu_sleep_sec={raw_vu_sleep!r}; using "
                    f"{self.config.vu_sleep_sec}"
                )
        self.config.test_duration = database.get("test_duration")
        self.config.rampup_time = database.get("rampup_time")
        db_mssql_pass = database.get("mssql_pass")
        if db_mssql_pass == "null":
            db_mssql_pass = None
        if db_mssql_pass:
            self.config.windows_mssql_pass = db_mssql_pass
        max_server_memory_mb = database.get("max_server_memory_mb")
        if max_server_memory_mb not in (None, "", "null"):
            self.config.max_server_memory_mb = int(max_server_memory_mb)

        test = yaml_data.get("test", {})
        user_count = test.get("user_count")
        if isinstance(user_count, str):
            self.config.user_count = user_count.split()
        elif isinstance(user_count, list):
            self.config.user_count = [str(u) for u in user_count]
        else:
            self.config.user_count = [str(user_count)] if user_count else []

        if not self.config.user_count:
            db_user_count = database.get("user_count")
            if isinstance(db_user_count, str):
                self.config.user_count = db_user_count.split()
            elif isinstance(db_user_count, list):
                self.config.user_count = [str(u) for u in db_user_count]
            else:
                self.config.user_count = [str(db_user_count)] if db_user_count else []

        self.config.log_level = test.get("log_level", "INFO")
        if self.config.log_level == "null" or not self.config.log_level:
            self.config.log_level = "INFO"

        self.config.description = yaml_data.get("description", "")
        if self.config.description == "null" or not self.config.description:
            self.config.description = ""

        raw_max_parallel = yaml_data.get(
            "max_parallel_workers",
            database.get("max_parallel_workers"),
        )
        if raw_max_parallel not in (None, "", "null"):
            try:
                self.config.max_parallel_workers = int(raw_max_parallel)
            except (TypeError, ValueError):
                logger.warning(
                    f"Invalid max_parallel_workers={raw_max_parallel!r}; using all hosts"
                )
                self.config.max_parallel_workers = None

        windows_cfg = yaml_data.get("windows", {})
        windows_hammerdb_path = windows_cfg.get("hammerdb_path")
        if windows_hammerdb_path in ("null", ""):
            windows_hammerdb_path = None
        if not self.config.windows_hammerdb_path and windows_hammerdb_path:
            self.config.windows_hammerdb_path = windows_hammerdb_path
        windows_test_script = windows_cfg.get("test_script")
        if windows_test_script in ("null", ""):
            windows_test_script = None
        if not self.config.windows_test_script and windows_test_script:
            self.config.windows_test_script = windows_test_script
        if self.config.windows_test_script and os.path.exists(self.config.windows_test_script):
            self.config.windows_test_script_local = self.config.windows_test_script
        windows_result_dir = windows_cfg.get("result_dir")
        if windows_result_dir in ("null", ""):
            windows_result_dir = None
        if not self.config.windows_result_dir and windows_result_dir:
            self.config.windows_result_dir = windows_result_dir
        windows_rebuild_script = windows_cfg.get("rebuild_script")
        if windows_rebuild_script in ("null", ""):
            windows_rebuild_script = None
        if not self.config.windows_rebuild_script and windows_rebuild_script:
            self.config.windows_rebuild_script = windows_rebuild_script
        if self.config.windows_rebuild_script and os.path.exists(self.config.windows_rebuild_script):
            self.config.windows_rebuild_script_local = self.config.windows_rebuild_script
        windows_create_db_sql = windows_cfg.get("create_db_sql")
        if windows_create_db_sql in ("null", ""):
            windows_create_db_sql = None
        if not self.config.windows_create_db_sql and windows_create_db_sql:
            self.config.windows_create_db_sql = windows_create_db_sql
        if self.config.windows_create_db_sql and os.path.exists(self.config.windows_create_db_sql):
            self.config.windows_create_db_sql_local = self.config.windows_create_db_sql
        if self.config.windows_create_db_sql and not self.config.windows_create_db_sql_local:
            config_dir = os.path.dirname(os.path.abspath(self.config.config_file)) if self.config.config_file else os.getcwd()
            candidate = os.path.join(config_dir, os.path.basename(self.config.windows_create_db_sql))
            if os.path.exists(candidate):
                self.config.windows_create_db_sql_local = candidate
                logger.info(f"Found local create_db.sql for copy: {candidate}")
        windows_build_schema_file = windows_cfg.get("build_schema_file")
        if windows_build_schema_file in ("null", ""):
            windows_build_schema_file = None
        if not self.config.windows_build_schema_file and windows_build_schema_file:
            self.config.windows_build_schema_file = windows_build_schema_file
        if self.config.windows_build_schema_file and os.path.exists(self.config.windows_build_schema_file):
            self.config.windows_build_schema_file_local = self.config.windows_build_schema_file
        windows_hammerdb_test_script = windows_cfg.get("hammerdb_test_script")
        if windows_hammerdb_test_script in ("null", ""):
            windows_hammerdb_test_script = None
        if not self.config.windows_hammerdb_test_script and windows_hammerdb_test_script:
            self.config.windows_hammerdb_test_script = windows_hammerdb_test_script
        if self.config.windows_hammerdb_test_script and os.path.exists(self.config.windows_hammerdb_test_script):
            self.config.windows_hammerdb_test_script_local = self.config.windows_hammerdb_test_script
        windows_mssql_pass = windows_cfg.get("mssql_pass")
        if windows_mssql_pass == "null":
            windows_mssql_pass = None
        if windows_mssql_pass:
            self.config.windows_mssql_pass = windows_mssql_pass
        windows_mssql_service_name = windows_cfg.get("mssql_service_name")
        if windows_mssql_service_name in ("null", ""):
            windows_mssql_service_name = None
        if windows_mssql_service_name:
            self.config.windows_mssql_service_name = windows_mssql_service_name
        windows_disk_id = windows_cfg.get("disk_id")
        if windows_disk_id in ("null", "", None):
            windows_disk_id = "1"
        self.config.windows_disk_id = str(windows_disk_id)
        use_osdisk = windows_cfg.get("use_osdisk")
        if use_osdisk in (True, "true", "True", 1, "1", "yes", "on"):
            self.config.windows_use_osdisk = True
        windows_rebuild_only = windows_cfg.get("rebuild_only")
        if windows_rebuild_only == "true" or windows_rebuild_only is True:
            self.config.windows_rebuild_only = True
        windows_rebuild_always = windows_cfg.get("rebuild_always")
        if windows_rebuild_always == "true" or windows_rebuild_always is True:
            self.config.windows_rebuild_always = True
        windows_rebuild_timeout = windows_cfg.get("rebuild_timeout")
        if windows_rebuild_timeout not in (None, "", "null"):
            try:
                self.config.windows_rebuild_timeout = int(windows_rebuild_timeout)
            except (TypeError, ValueError):
                logger.error(f"Invalid windows.rebuild_timeout: {windows_rebuild_timeout}")
                sys.exit(1)
        windows_test_only = windows_cfg.get("test_only")
        if windows_test_only == "true" or windows_test_only is True:
            self.config.windows_test_only = True
        windows_ssh_user = windows_cfg.get("ssh_user")
        if windows_ssh_user and windows_ssh_user != "null":
            self.config.windows_ssh_user = windows_ssh_user
        windows_rebuilddb = windows_cfg.get("rebuilddb")
        if windows_rebuilddb == "false" or windows_rebuilddb is False:
            self.config.windows_rebuilddb = False

        timeouts_cfg = yaml_data.get("timeouts", {})
        if timeouts_cfg:
            self.config.timeout_default = self._parse_timeout(timeouts_cfg, "default", self.config.timeout_default)
            self.config.timeout_scp = self._parse_timeout(timeouts_cfg, "scp", self.config.timeout_scp)
            self.config.timeout_test = self._parse_timeout(timeouts_cfg, "test", self.config.timeout_test)
            self.config.timeout_prepare = self._parse_timeout(timeouts_cfg, "prepare", self.config.timeout_prepare)
            self.config.timeout_vm_lookup = self._parse_timeout(timeouts_cfg, "vm_lookup", self.config.timeout_vm_lookup)
            self.config.timeout_label_query = self._parse_timeout(timeouts_cfg, "label_query", self.config.timeout_label_query)
            self.config.timeout_oc_command = self._parse_timeout(timeouts_cfg, "oc_command", self.config.timeout_oc_command)

        perfmon_cfg = yaml_data.get("perfmon", {}) or {}
        raw_en = perfmon_cfg.get("enabled", False)
        if isinstance(raw_en, str):
            self.config.perfmon_enabled = raw_en.strip().lower() in ("true", "1", "yes", "on")
        else:
            self.config.perfmon_enabled = bool(raw_en)
        try:
            self.config.perfmon_sample_interval = int(perfmon_cfg.get("sample_interval", 5) or 5)
        except (TypeError, ValueError):
            self.config.perfmon_sample_interval = 5
        counters_file = perfmon_cfg.get("counters_file")
        if counters_file not in (None, "", "null"):
            self.config.perfmon_counters_file = str(counters_file)
        until_iter = perfmon_cfg.get("until_iteration", perfmon_cfg.get("iterations"))
        if until_iter not in (None, "", "null"):
            try:
                self.config.perfmon_until_iteration = max(1, int(until_iter))
            except (TypeError, ValueError):
                logger.warning(
                    f"Invalid perfmon.until_iteration={until_iter!r}; capturing all iterations"
                )
                self.config.perfmon_until_iteration = None
        collector_name = perfmon_cfg.get("collector_name")
        if collector_name not in (None, "", "null"):
            self.config.perfmon_collector_name = str(collector_name)
        export_csv = perfmon_cfg.get("export_csv")
        if export_csv not in (None, "", "null"):
            if isinstance(export_csv, str):
                self.config.perfmon_export_csv = export_csv.strip().lower() in (
                    "true", "1", "yes", "on"
                )
            else:
                self.config.perfmon_export_csv = bool(export_csv)

    @staticmethod
    def _parse_timeout(timeouts_cfg: Dict, key: str, default: int) -> int:
        val = timeouts_cfg.get(key)
        if val in (None, "", "null"):
            return default
        try:
            return int(val)
        except (TypeError, ValueError):
            logger.warning(f"Invalid timeout.{key}: {val}, using default {default}")
            return default

    def _get_db_hosts(self, yaml_data: Dict) -> List[str]:
        database = yaml_data.get("database", {})
        host_pattern = database.get("host_pattern")
        if host_pattern:
            logger.info(f"Using host pattern: {host_pattern}")
            if "{" in host_pattern and ".." in host_pattern:
                match = re.search(r"([\w-]+)\{(\d+)\.\.(\d+)\}", host_pattern)
                if match:
                    prefix = match.group(1)
                    start = int(match.group(2))
                    end = int(match.group(3))
                    expanded = [f"{prefix}{i}" for i in range(start, end + 1)]
                    logger.info(f"Expanded pattern to {len(expanded)} hosts")
                    return expanded
                logger.warning(f"Could not parse host pattern '{host_pattern}' - using as-is")
            return [host_pattern]

        host_labels = database.get("host_labels")
        if host_labels:
            if self.config.use_virtctl is False:
                logger.error("Label-based host selection is not supported in SSH-only mode")
                sys.exit(1)
            logger.info(f"Using label selector: {host_labels}")
            if not self.config.dry_run:
                try:
                    result = subprocess.run(
                        ["oc", "get", "vms", "-n", self.config.namespace,
                         "-l", host_labels, "-o", "jsonpath={range .items[*]}{.metadata.name}{' '}{end}"],
                        capture_output=True,
                        text=True,
                        timeout=self.config.timeout_label_query
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        hosts = result.stdout.strip().split()
                        logger.info(f"Found {len(hosts)} VMs matching labels: {host_labels}")
                        return hosts
                except Exception as e:
                    logger.warning(f"Failed to query VMs by labels: {e}")
            else:
                logger.info(f"Dry-run mode: Would query VMs with labels: {host_labels}")
                return ["example-mssql1", "example-mssql2"]

        host_file = database.get("host_file")
        if host_file:
            logger.info(f"Using host file: {host_file}")
            if os.path.exists(host_file):
                hosts = []
                with open(host_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            if "{" in line and ".." in line:
                                match = re.search(r"([\w-]+)\{(\d+)\.\.(\d+)\}", line)
                                if match:
                                    prefix = match.group(1)
                                    start = int(match.group(2))
                                    end = int(match.group(3))
                                    hosts.extend([f"{prefix}{i}" for i in range(start, end + 1)])
                                else:
                                    hosts.append(line)
                            else:
                                hosts.append(line)
                if hosts:
                    logger.info(f"Found {len(hosts)} hosts in file")
                    return hosts
            logger.error(f"Host file '{host_file}' not found or empty")
            sys.exit(1)

        hosts = database.get("hosts")
        if hosts:
            if isinstance(hosts, str):
                host_list = hosts.split()
            elif isinstance(hosts, list):
                host_list = hosts
            else:
                host_list = [str(hosts)]
            logger.info(f"Using simple host list: {' '.join(host_list)}")
            return host_list

        if self.config.create_vms:
            logger.info(
                "CREATE_VMS: deferring host list until VMs are Ready and SSH-reachable"
            )
            return []

        logger.error("No database hosts specified in configuration")
        sys.exit(1)


def display_config(config: MSSQLWinConfig) -> None:
    logger.info(f"Configuration loaded from: {config.config_file}")
    if config.description:
        logger.info(f"Test description: {config.description}")
    logger.info(f"Hosts: {' '.join(config.db_hosts)}")
    mpw = pool_max_workers(config)
    if config.max_parallel_workers is None or config.max_parallel_workers <= 0:
        logger.info(f"Max parallel workers: {mpw} (all hosts, no cap)")
    else:
        logger.info(
            f"Max parallel workers: {mpw} (cap={config.max_parallel_workers})"
        )
    if config.use_virtctl is not False:
        logger.info(f"Namespace: {config.namespace}")
    else:
        logger.info("Namespace: N/A (SSH-only mode)")
    logger.info(f"Warehouse count: {config.warehouse_count}")
    if config.build_users:
        logger.info(f"Build users: {config.build_users}")
    logger.info(f"User counts: {' '.join(config.user_count)}")
    logger.info(f"Sweep iterations: {config.test_iterations}")
    logger.info(
        f"Sleeps: vu_sleep={config.vu_sleep_sec}s between VU levels, "
        f"iteration_sleep={config.iteration_sleep_sec}s between iterations"
    )
    logger.info(f"Test duration: {config.test_duration} minutes")
    if config.windows_hammerdb_path:
        logger.info(f"Windows HammerDB path: {config.windows_hammerdb_path}")
    if config.windows_test_script:
        logger.info(f"Windows test script: {config.windows_test_script}")
    if config.windows_test_script_local:
        logger.info(f"Windows test script (local override): {config.windows_test_script_local}")
    if config.windows_rebuild_script:
        logger.info(f"Windows rebuild script: {config.windows_rebuild_script}")
    if config.windows_rebuild_script_local:
        logger.info(f"Windows rebuild script (local override): {config.windows_rebuild_script_local}")
    if config.windows_create_db_sql:
        logger.info(f"Windows create_db_sql: {config.windows_create_db_sql}")
    if config.windows_create_db_sql_local:
        logger.info(f"Windows create_db_sql (local override): {config.windows_create_db_sql_local}")
    if config.windows_build_schema_file:
        logger.info(f"Windows build schema file: {config.windows_build_schema_file}")
    if config.windows_build_schema_file_local:
        logger.info(f"Windows build schema file (local override): {config.windows_build_schema_file_local}")
    if config.windows_hammerdb_test_script:
        logger.info(f"Windows hammerdb_test_script: {config.windows_hammerdb_test_script}")
    if config.windows_hammerdb_test_script_local:
        logger.info(f"Windows hammerdb_test_script (local override): {config.windows_hammerdb_test_script_local}")
    if config.windows_mssql_pass:
        logger.info("Windows mssql_pass: [SET]")
    if config.max_server_memory_mb is not None:
        logger.info(f"Max server memory: {config.max_server_memory_mb} MB")
    else:
        logger.info("Max server memory: not configured (SQL Server default/unlimited)")
    logger.info(f"Windows rebuild_only: {'ENABLED' if config.windows_rebuild_only else 'DISABLED'}")
    logger.info(f"Windows rebuild_always: {'ENABLED' if config.windows_rebuild_always else 'DISABLED'}")
    logger.info(f"Windows test_only: {'ENABLED' if config.windows_test_only else 'DISABLED'}")
    if config.windows_result_dir:
        logger.info(f"Windows result dir: {config.windows_result_dir}")
    logger.info(f"Windows SSH user: {config.windows_ssh_user}")
    logger.info(f"Windows disk_id: {config.windows_disk_id}")
    logger.info(
        f"Windows use_osdisk: {'ENABLED (TPCC on C:, skip data-disk provision)' if config.windows_use_osdisk else 'DISABLED (D: data disk)'}"
    )
    logger.info(f"Windows rebuilddb: {'ENABLED' if config.windows_rebuilddb else 'DISABLED'}")
    logger.info(f"Log level: {config.log_level}")
    logger.info(f"Timeouts - default: {config.timeout_default}s, scp: {config.timeout_scp}s, test: {config.timeout_test}s, prepare: {config.timeout_prepare}s")
    logger.info(f"Timeouts - vm_lookup: {config.timeout_vm_lookup}s, label_query: {config.timeout_label_query}s, oc_command: {config.timeout_oc_command}s")
    if config.perfmon_enabled:
        until = config.perfmon_until_iteration
        until_s = f"until_iteration={until}" if until else "until_iteration=all"
        cf = config.perfmon_counters_file or "default txt / builtin"
        logger.info(
            f"Perfmon: ENABLED (sample_interval={config.perfmon_sample_interval}s, "
            f"{until_s}, counters={cf}, collector={config.perfmon_collector_name}, "
            f"export_csv={config.perfmon_export_csv})"
        )
    else:
        logger.info("Perfmon: DISABLED")

