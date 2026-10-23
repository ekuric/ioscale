"""CLI entrypoint and phase orchestration for MSSQL Windows HammerDB tests."""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime

from mssqlwin_tests.build import build_database_windows, generate_test_files
from mssqlwin_tests.config import ConfigLoader, MSSQLWinConfig, display_config
from mssqlwin_tests.executor import CommandExecutor
from mssqlwin_tests.migration import VMMigrationMonitor
from mssqlwin_tests.perfmon import (
    export_perfmon_csv_on_hosts,
    start_perfmon_on_hosts,
    stop_perfmon_on_hosts,
)
from mssqlwin_tests.provision import cleanup_provisioned, provision_vms, remove_ssh_key_dir
from mssqlwin_tests.results import _move_log_to_results, collect_results
from mssqlwin_tests.service import configure_mssql_memory_windows
from mssqlwin_tests.storage import apply_osdisk_overrides, prepare_windows_machines
from mssqlwin_tests.workload import run_tests_windows

logger = logging.getLogger("mssqlwin_tests")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MSSQL HammerDB Windows Testing Script (YAML Configuration Version)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
    python3 -m mssqlwin_tests -c mssql-configwin.yaml
    python3 -m mssqlwin_tests --create-vms --vms 3 --storage-class ocs-storagecluster-ceph-rbd \\
        --win-vm-image https://example.com/win2k25.qcow2
    python3 -m mssqlwin_tests --create-vms --vms 5 --golden-snapshot windv-snap \\
        --storage-class ocs-storagecluster-ceph-rbd
    python3 -m mssqlwin_tests --create-vms --vms 3 --orig-vm gold-win --storage-class ...
        """,
    )
    parser.add_argument(
        "-c",
        "--config",
        default="mssql-config.yaml",
        help="Path to YAML configuration file (default: mssql-config.yaml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration without executing remote steps",
    )
    parser.add_argument(
        "--copy-results",
        action="store_true",
        help="Only copy results from hosts (skip rebuild and tests)",
    )
    parser.add_argument(
        "--ssh-only",
        action="store_true",
        help="Force SSH for all hosts (baremetal/KVM, no virtctl)",
    )
    parser.add_argument(
        "--virtctl-only",
        action="store_true",
        help="Force virtctl for all hosts (OpenShift VMs)",
    )
    parser.add_argument(
        "--test-script",
        dest="test_script",
        default=None,
        help="Local test script to copy to Windows hosts and run",
    )
    parser.add_argument(
        "--build-schema-file",
        dest="build_schema_file",
        default=None,
        help="Local build schema TCL to customize and copy to Windows hosts",
    )
    parser.add_argument(
        "--rebuild-script",
        dest="rebuild_script",
        default=None,
        help="Local rebuild script to copy to Windows hosts and run",
    )
    parser.add_argument(
        "--create-db",
        dest="create_db",
        default=None,
        help="Local create_db.sql to copy to Windows hosts",
    )
    parser.add_argument(
        "--hammerdb-test-script",
        dest="hammerdb_test_script",
        default=None,
        help="Local HammerDB test script to copy to Windows hosts",
    )
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="Only generate per-user files locally and exit",
    )
    parser.add_argument(
        "--rebuild-always",
        action="store_true",
        help="Rebuild database before each user-count test run",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=None,
        dest="max_parallel",
        help=(
            "Max concurrent hosts for rebuild/test/collect "
            "(default/0: all hosts, no hard 50 cap; positive N: min(hosts, N))"
        ),
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        dest="iterations",
        help="Repeat full user_count sweep N times (database.iterations)",
    )
    parser.add_argument(
        "--iteration-sleep",
        type=int,
        default=None,
        dest="iteration_sleep",
        help="Seconds to sleep between sweep iterations (0=off). Alias: --sweep-sleep",
    )
    parser.add_argument(
        "--sweep-sleep",
        type=int,
        default=None,
        dest="sweep_sleep",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--vu-sleep",
        type=int,
        default=None,
        dest="vu_sleep",
        help="Seconds to sleep between VU levels within an iteration (0=off)",
    )
    parser.add_argument(
        "--prepare-machine",
        action="store_true",
        help="Prepare Windows machines by formatting the data disk and exit",
    )
    parser.add_argument(
        "--osdisk",
        action="store_true",
        help=(
            "Run TPCC on OS disk (C:\\mssql\\data): skip data-disk provision and "
            "HammerDB copy to D:; use create_dbC.sql"
        ),
    )
    parser.add_argument(
        "--monitor-vm",
        action="store_true",
        help="Monitor VM node placement during tests and log migrations",
    )
    parser.add_argument(
        "--monitor-vm-interval",
        type=int,
        default=10,
        help="VM monitor polling interval in seconds (default: 10)",
    )
    parser.add_argument(
        "--perfmon",
        action="store_true",
        help="Enable Windows PerfMon collectors during the test",
    )
    parser.add_argument(
        "--perfmon-counters-file",
        dest="perfmon_counters_file",
        default=None,
        help="Path to PerfMon counters .txt or logman .xml template",
    )
    parser.add_argument(
        "--perfmon-until-iteration",
        type=int,
        default=None,
        dest="perfmon_until_iteration",
        help="Stop PerfMon after sweep iteration N (needs database.iterations)",
    )
    parser.add_argument(
        "--perfmon-sample-interval",
        type=int,
        default=None,
        dest="perfmon_sample_interval",
        help="PerfMon sample interval seconds",
    )
    parser.add_argument(
        "--perfmon-no-export-csv",
        action="store_true",
        dest="perfmon_no_export_csv",
        help="Skip post-test relog BLG→CSV export (default: export CSV)",
    )

    # --- create-vms (Windows snapshot → clone) ---
    parser.add_argument(
        "--create-vms",
        action="store_true",
        help=(
            "Provision Windows VMs before the test: import/source → VolumeSnapshot "
            "→ clone root DataVolume per VM (fio-windows / winCdisk style)"
        ),
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete managed VMs after the test (default: keep)",
    )
    parser.add_argument(
        "--vms",
        type=int,
        default=None,
        help="Number of Windows VMs to create when --create-vms is set",
    )
    parser.add_argument(
        "--vm-prefix",
        default=None,
        help="VM name prefix (default: mssql-win → mssql-win-1 …)",
    )
    parser.add_argument("--vm-cpu", default=None, help="vCPU cores per VM (default: 8)")
    parser.add_argument(
        "--vm-memory", default=None, help="Memory per VM (default: 16Gi)"
    )
    parser.add_argument(
        "--storage-class",
        default=None,
        help="StorageClass for root/data DataVolumes (required with --create-vms)",
    )
    parser.add_argument(
        "--access-mode",
        default=None,
        help="PVC access mode (default: ReadWriteMany)",
    )
    parser.add_argument(
        "--win-vm-image",
        default=None,
        help="HTTP(S) URL of Windows qcow2 → source DV → VolumeSnapshot → clones",
    )
    parser.add_argument(
        "--win-root-size",
        default=None,
        help="Root disk size for source DV / clones (default: 100Gi)",
    )
    parser.add_argument(
        "--win-data-size",
        default=None,
        help="Blank data disk size when not using --osdisk (default: 40Gi)",
    )
    parser.add_argument(
        "--golden-snapshot",
        default=None,
        help="Existing VolumeSnapshot name to clone root disks from (skip image import)",
    )
    parser.add_argument(
        "--orig-vm",
        default=None,
        help="Snapshot root PVC of an existing VM, then clone N VMs from that snapshot",
    )
    parser.add_argument(
        "--snapshot-class",
        default=None,
        help="VolumeSnapshotClass (auto-detected from PVC StorageClass if omitted)",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info("Starting MSSQL HammerDB Windows testing script")

    config = MSSQLWinConfig()
    config.config_file = args.config
    config.dry_run = args.dry_run
    config.verbose = args.verbose
    config.use_virtctl = (
        None if not (args.ssh_only or args.virtctl_only) else (not args.ssh_only)
    )
    config.copy_results = args.copy_results
    config.monitor_vm = args.monitor_vm
    config.monitor_vm_interval = args.monitor_vm_interval
    config.generate_only = args.generate_only
    if args.rebuild_always:
        config.windows_rebuild_always = True
    if args.osdisk:
        config.windows_use_osdisk = True

    # create-vms flags before load_config so empty hosts are allowed
    if args.create_vms:
        config.create_vms = True
        config.use_virtctl = True
    if args.cleanup:
        config.cleanup_vms = True
    if args.vms is not None:
        config.vms_count = args.vms
    if args.vm_prefix:
        config.vm_prefix = args.vm_prefix
    if args.vm_cpu:
        config.vm_cpu = args.vm_cpu
    if args.vm_memory:
        config.vm_memory = args.vm_memory
    if args.storage_class:
        config.storage_class = args.storage_class
    if args.access_mode:
        config.access_mode = args.access_mode
    if args.win_vm_image:
        config.win_vm_image = args.win_vm_image
    if args.win_root_size:
        config.win_root_volume_size = args.win_root_size
    if args.win_data_size:
        config.win_data_disk_size = args.win_data_size
    if args.golden_snapshot:
        config.golden_snapshot = args.golden_snapshot
    if args.orig_vm:
        config.orig_vm = args.orig_vm
    if args.snapshot_class:
        config.snapshot_class = args.snapshot_class

    prepare_machine = args.prepare_machine
    if args.test_script:
        if not os.path.exists(args.test_script):
            logger.error(f"Test script not found: {args.test_script}")
            return 1
        config.windows_test_script_local = args.test_script
    if args.build_schema_file:
        if not os.path.exists(args.build_schema_file):
            logger.error(f"Build schema file not found: {args.build_schema_file}")
            return 1
        config.windows_build_schema_file_local = args.build_schema_file
    if args.rebuild_script:
        if not os.path.exists(args.rebuild_script):
            logger.error(f"Rebuild script not found: {args.rebuild_script}")
            return 1
        config.windows_rebuild_script_local = args.rebuild_script
    if args.create_db:
        if not os.path.exists(args.create_db):
            logger.error(f"create_db.sql not found: {args.create_db}")
            return 1
        config.windows_create_db_sql_local = args.create_db
    if args.hammerdb_test_script:
        if not os.path.exists(args.hammerdb_test_script):
            logger.error(f"HammerDB test script not found: {args.hammerdb_test_script}")
            return 1
        config.windows_hammerdb_test_script_local = args.hammerdb_test_script

    if config.create_vms and args.ssh_only:
        logger.error("--create-vms requires virtctl mode (not --ssh-only)")
        return 1

    loader = ConfigLoader(config)
    loader.load_config()
    if args.osdisk:
        config.windows_use_osdisk = True
    # CLI create-vms knobs win over YAML when set
    if args.create_vms:
        config.create_vms = True
    if args.vms is not None:
        config.vms_count = args.vms
    if args.perfmon:
        config.perfmon_enabled = True
    if args.perfmon_counters_file:
        config.perfmon_counters_file = args.perfmon_counters_file
        config.perfmon_enabled = True
    if args.perfmon_until_iteration is not None:
        if args.perfmon_until_iteration < 1:
            logger.error("--perfmon-until-iteration must be >= 1")
            return 1
        config.perfmon_until_iteration = args.perfmon_until_iteration
        config.perfmon_enabled = True
    if args.perfmon_sample_interval is not None:
        if args.perfmon_sample_interval < 1:
            logger.error("--perfmon-sample-interval must be >= 1")
            return 1
        config.perfmon_sample_interval = args.perfmon_sample_interval
    if args.perfmon_no_export_csv:
        config.perfmon_export_csv = False
    if args.iterations is not None:
        if args.iterations < 1:
            logger.error("--iterations must be >= 1")
            return 1
        config.test_iterations = args.iterations
    cli_iter_sleep = args.iteration_sleep
    if cli_iter_sleep is None:
        cli_iter_sleep = args.sweep_sleep
    if cli_iter_sleep is not None:
        if cli_iter_sleep < 0:
            logger.error("--iteration-sleep must be >= 0")
            return 1
        config.iteration_sleep_sec = cli_iter_sleep
    if args.vu_sleep is not None:
        if args.vu_sleep < 0:
            logger.error("--vu-sleep must be >= 0")
            return 1
        config.vu_sleep_sec = args.vu_sleep
    if args.max_parallel is not None:
        if args.max_parallel < 0:
            logger.error("--max-parallel must be >= 0")
            return 1
        config.max_parallel_workers = args.max_parallel
    apply_osdisk_overrides(config)

    log_date = datetime.now().strftime("%Y%m%d")
    sanitized_desc = (
        re.sub(r"[^a-z0-9]", "_", config.description.lower()) if config.description else ""
    )
    sanitized_desc = re.sub(r"_+", "_", sanitized_desc).strip("_")
    log_file = (
        f"mssqlwin-{log_date}-{sanitized_desc}.txt"
        if sanitized_desc
        else f"mssqlwin-{log_date}.txt"
    )
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")
    )
    logging.getLogger().addHandler(file_handler)
    logger.info(f"Logging all output to: {log_file}")

    display_config(config)
    if config.create_vms:
        logger.info(
            f"CREATE_VMS: vms={config.vms_count} prefix={config.vm_prefix} "
            f"storage_class={config.storage_class} "
            f"image={config.win_vm_image or '-'} "
            f"golden_snapshot={config.golden_snapshot or '-'} "
            f"orig_vm={config.orig_vm or '-'}"
        )

    if config.create_vms and not config.copy_results:
        try:
            provision_vms(config)
        except Exception as e:
            logger.error(f"VM provision failed: {e}")
            return 1
        if not config.db_hosts and not config.dry_run:
            logger.error("CREATE_VMS finished but no hosts were provisioned")
            return 1

    executor = CommandExecutor(config)
    if config.use_virtctl is None and config.namespace_from_config:
        logger.info(
            f"Namespace '{config.namespace}' provided in config; forcing virtctl mode."
        )
        config.use_virtctl = True
    if config.use_virtctl is None and not config.generate_only and not config.create_vms:
        non_vm_hosts = [h for h in config.db_hosts if not executor.is_vm_host(h)]
        if non_vm_hosts:
            logger.error(
                "Hosts not detected as VMs in the current OpenShift context: "
                f"{', '.join(non_vm_hosts)}"
            )
            logger.error(
                "If these are OpenShift VMs, set database.namespace correctly "
                "or re-run with --virtctl-only. For baremetal/KVM, use --ssh-only."
            )
            return 1

    try:
        if prepare_machine:
            prepare_windows_machines(config, executor)
            logger.info(
                "Prepare-machine mode complete; skipping tests and result collection"
            )
            return 0

        if not config.windows_hammerdb_path:
            logger.error("windows.hammerdb_path is required for Windows testing")
            return 1
        results_date = datetime.now().strftime("%Y%m%d-%H%M%S")
        sanitized_desc = (
            re.sub(r"[^a-z0-9]", "_", config.description.lower())
            if config.description
            else ""
        )
        sanitized_desc = re.sub(r"_+", "_", sanitized_desc).strip("_")
        if sanitized_desc:
            results_dir = f"mssql-results-{results_date}-{sanitized_desc}"
        else:
            results_dir = f"mssql-results-{results_date}"

        # Collect-only: never format/prepare disks.
        if config.copy_results:
            collect_results(config, executor, results_dir)
            _move_log_to_results(log_file, results_dir)
            return 0

        # Generate local scripts only: never format/prepare remote disks.
        if config.generate_only:
            if (
                config.windows_rebuild_only
                or config.windows_test_only
                or config.copy_results
            ):
                logger.warning(
                    "Generate-only mode ignores rebuild_only, test_only, and copy_results flags."
                )
            logger.info(
                "generate-only enabled: skipping prepare/format; writing local test files only"
            )
            generate_test_files(config)
            run_tests_windows(config, executor)
            logger.info("Generate-only mode complete; skipping remote execution")
            return 0

        # test_only: skip prepare/format so an existing TPCC DB can be reused.
        if config.windows_test_only:
            logger.info(
                "windows.test_only enabled: skipping prepare and build file generation"
            )
        else:
            prepare_windows_machines(config, executor)
            generate_test_files(config)

        if config.windows_rebuild_only and config.windows_test_only:
            logger.error(
                "windows.rebuild_only and windows.test_only cannot both be enabled"
            )
            return 1

        if config.windows_rebuild_only:
            if not config.windows_rebuilddb:
                logger.error(
                    "windows.rebuild_only is enabled but windows.rebuilddb is false"
                )
                return 1
            configure_mssql_memory_windows(config, executor)
            build_database_windows(config, executor)
            logger.info("Rebuild-only mode complete; skipping tests and result collection")
            return 0

        configure_mssql_memory_windows(config, executor)
        if not config.windows_test_only:
            build_database_windows(config, executor)

        migration_monitor = None
        if config.monitor_vm:
            migration_monitor = VMMigrationMonitor(
                namespace=config.namespace,
                interval=config.monitor_vm_interval,
                vm_hosts=config.db_hosts,
            )
            migration_monitor.start()

        test_error = None
        try:
            if config.perfmon_enabled:
                start_perfmon_on_hosts(config, executor)
            run_tests_windows(config, executor, migration_monitor=migration_monitor)
        except Exception as exc:
            test_error = exc
            logger.error(f"Test phase failed: {exc}; still collecting results")
        finally:
            if config.perfmon_enabled and config.perfmon_running:
                try:
                    stop_perfmon_on_hosts(config, executor)
                except Exception as exc:
                    logger.warning(f"perfmon stop failed: {exc}")
            elif config.perfmon_enabled:
                logger.info("Perfmon already stopped earlier; skipping final stop")
                if config.perfmon_export_csv:
                    try:
                        export_perfmon_csv_on_hosts(config, executor)
                    except Exception as exc:
                        logger.warning(f"perfmon CSV export failed: {exc}")
            if migration_monitor:
                migration_monitor.stop()
            try:
                collect_results(config, executor, results_dir)
            except Exception as exc:
                logger.error(f"Result collection failed: {exc}")
                if test_error is None:
                    test_error = exc

        if migration_monitor:
            migration_log_path = os.path.join(results_dir, "migration-events.log")
            migration_monitor.write_report(migration_log_path)

        _move_log_to_results(log_file, results_dir)
        if test_error is not None:
            raise test_error
        return 0
    finally:
        if config.create_vms and config.cleanup_vms:
            try:
                cleanup_provisioned(config)
            except Exception as e:
                logger.warning(f"Cleanup after create-vms failed: {e}")
        elif config.create_vms and not config.cleanup_vms:
            # Keep bastion key for inspection; drop in-container key dir only if cleanup
            remove_ssh_key_dir(config.ssh_key_path, also_bastion_mirror=False)


if __name__ == "__main__":
    raise SystemExit(main())
