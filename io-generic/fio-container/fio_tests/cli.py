"""CLI entrypoint and orchestration for FIO remote testing."""

import glob

import argparse
import logging
import os
import re
import shutil
import sys
from datetime import datetime

from fio_tests.config import (
    ConfigLoader,
    FioConfigError,
    FioTestConfig,
    apply_testdir_overrides,
)
from fio_tests.dataset import write_test_data
from fio_tests.executor import CommandExecutor
from fio_tests.fio_run import run_fio_tests
from fio_tests.migration import VMMigrationMonitor, run_migration_report
from fio_tests.packages import ensure_packages_installed, prepare_machine
from fio_tests.results import collect_results, generate_combined_results
from fio_tests.storage import cleanup_storage, prepare_storage

# Configure logging early (before dependency checks) — same format as legacy script
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fio_tests")

def check_dependencies(config: FioTestConfig) -> None:
    """
    Check if required tools are installed.

    Validates that necessary command-line tools are available based on
    the connection mode (SSH-only, virtctl-only, or auto-detection).

    Args:
        config: FIO test configuration object.

    Raises:
        FioConfigError: If any required tools are missing.
    """
    missing_tools = []
    
    if not config.dry_run:
        if config.use_virtctl is True:
            # Force virtctl mode
            if not shutil.which("virtctl"):
                missing_tools.append("virtctl")
            if not shutil.which("oc"):
                missing_tools.append("oc")
        elif config.use_virtctl is False:
            # Force SSH mode
            if not shutil.which("ssh"):
                missing_tools.append("ssh")
        else:
            # Auto-detection mode
            if not shutil.which("virtctl"):
                missing_tools.append("virtctl")
            if not shutil.which("oc"):
                missing_tools.append("oc")
            if not shutil.which("ssh"):
                missing_tools.append("ssh")
    
    if missing_tools:
        raise FioConfigError(f"Required tools missing: {', '.join(missing_tools)}")


def main():
    """
    Main entry point for FIO remote testing script.

    Parses command-line arguments, loads configuration, and orchestrates
    the test execution workflow:
    1. Validate dependencies and configuration
    2. Prepare storage (format and mount devices)
    3. Install FIO and dependencies on hosts
    4. Write initial test dataset
    5. Run FIO performance tests (with optional VM migrations)
    6. Collect and combine results
    7. Clean up storage

    Supports multiple modes via command-line flags:
    - Normal test execution
    - --prepare-machine (FIO installation only)
    - --copy-results (result collection only)
    - --migration-report (query historical migrations)
    - --dry-run (validate configuration without execution)

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    parser = argparse.ArgumentParser(
        description="FIO Remote Testing Script (Python version)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example (OS-disk load on /root/testdir or c:/testdir, no data-disk format):\n"
            "  python3 fio-tests.py -c fio-config.yaml --testdir --yes-i-mean-it"
        ),
    )
    parser.add_argument('-c', '--config', default='fio-config.yaml',
                       help='Path to YAML configuration file (default: fio-config.yaml)')
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Verbose output')
    parser.add_argument('--dry-run', action='store_true',
                       help='Validate configuration and show what would be done without executing')
    parser.add_argument('--ssh-only', action='store_true',
                       help='Force SSH for all hosts')
    parser.add_argument('--virtctl-only', action='store_true',
                       help='Force virtctl for all hosts')
    parser.add_argument('--yes-i-mean-it', action='store_true',
                       help='Skip confirmation prompt for device formatting')
    parser.add_argument('--prepare-machine', action='store_true',
                       help='Only install FIO dependencies on machines, skip all testing')
    parser.add_argument('--interval', type=int,
                       help='Override retry interval in seconds (from config file)')
    parser.add_argument('--max-retries', type=int,
                       help='Override maximum number of retry attempts (from config file)')
    parser.add_argument('--skip-connectivity-test', action='store_true',
                       help='Skip connectivity test and proceed directly to command execution')
    parser.add_argument('--monitor-interval', type=int,
                       help='Override task monitor interval in seconds (from config file)')
    parser.add_argument('--debug', action='store_true',
                       help='Show detailed configuration parsing debug information')
    parser.add_argument('--copy-results', action='store_true',
                       help='Only copy results from hosts (skip installation, preparation, and testing)')
    parser.add_argument('--monitor-vm', action='store_true',
                       help='Monitor VM node placement during tests and log migrations')
    parser.add_argument('--monitor-vm-interval', type=int, default=10,
                       help='VM monitor polling interval in seconds (default: 10)')
    parser.add_argument('--migration-report', action='store_true',
                       help='Query and display historical VM migration data from cluster (post-hoc)')
    parser.add_argument('--max-workers', type=int, default=None,
                       help='Override default max workers per pool (default: 50)')
    parser.add_argument(
        '--testdir',
        action='store_true',
        help=(
            'Run FIO on OS disk: Linux /root/testdir, Windows c:/testdir; '
            'use c:/tools/fio in place (no copy to data disk); results on '
            'c:/fio-results. Skips format/mount/provision of YAML storage devices.'
        ),
    )

    args = parser.parse_args()
    
    # Set up logging
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    logger.info("Starting FIO remote testing script (Python version)")
    
    # Initialize configuration
    config = FioTestConfig()
    config.config_file = args.config
    config.dry_run = args.dry_run
    config.verbose = args.verbose
    config.use_virtctl = None if not (args.ssh_only or args.virtctl_only) else (not args.ssh_only)
    config.skip_confirmation = args.yes_i_mean_it
    config.prepare_machine = args.prepare_machine
    config.debug_config = args.debug
    config.copy_results = args.copy_results
    config.monitor_vm = args.monitor_vm
    config.monitor_vm_interval = args.monitor_vm_interval
    config.migration_report = args.migration_report
    config.max_workers_cli = args.max_workers
    config.use_testdir = args.testdir

    # Load configuration (YAML sets defaults)
    try:
        config_loader = ConfigLoader(config)
        config_loader.load_config()
        apply_testdir_overrides(config)

        # Override config values with command-line arguments after loading YAML
        # (CLI args take precedence over YAML config)
        if args.interval is not None:
            config.retry_interval = args.interval
        if args.max_retries is not None:
            config.max_retries = args.max_retries
        if args.skip_connectivity_test:
            config.skip_connectivity_test = True
        if args.monitor_interval is not None:
            config.task_monitor_interval = args.monitor_interval
        
        # Handle migration-report mode (early exit, no FIO testing needed)
        if config.migration_report:
            return run_migration_report(config)
        
        # Set up log file with description in filename
        log_timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
        sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')
        
        if sanitized_desc:
            log_file = f"fio-test-{sanitized_desc}-{log_timestamp}.txt"
        else:
            log_file = f"fio-test-{log_timestamp}.txt"
        
        # Add file handler
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s',
                                                    datefmt='%Y-%m-%d %H:%M:%S'))
        logging.getLogger().addHandler(file_handler)
        
        logger.info(f"Logging all output to: {log_file}")
        
        # Add description to log file header
        if config.description:
            logger.info("=" * 80)
            logger.info(f"TEST DESCRIPTION: {config.description}")
            logger.info("=" * 80)
        
        # Check dependencies
        check_dependencies(config)
    except FioConfigError as e:
        logger.error(str(e))
        return 1
    
    # Display configuration
    logger.info(f"Configuration loaded from: {config.config_file}")
    logger.info(f"VMs: {' '.join(config.vm_hosts)}")
    if config.use_virtctl is not False:
        logger.info(f"Namespace: {config.namespace}")
    else:
        logger.info("Namespace: N/A (SSH-only mode)")
    
    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()

    if config.use_testdir:
        logger.info("Storage: TESTDIR MODE (OS disk — YAML storage devices not used)")
        if linux_hosts:
            logger.info(f"  Linux FIO data directory: {config.mount_point}")
            logger.info(f"  Linux results directory: {config.output_dir}")
        if windows_hosts:
            logger.info(f"  Windows FIO data directory: {config.windows_mount_point}")
            logger.info(f"  Windows FIO directory: {config.windows_fio_dir}")
            logger.info(f"  Windows results directory: {config.windows_output_dir}")
    else:
        logger.info(f"Storage device configuration:")
        for host in linux_hosts:
            device = config.storage_devices.get(host, "N/A")
            logger.info(f"  {host} (Linux): /dev/{device}")
        for host in windows_hosts:
            device = config.windows_storage_devices.get(host, "N/A")
            logger.info(f"  {host} (Windows): Disk {device}")

        if linux_hosts:
            logger.info(f"Mount point (Linux): {config.mount_point}")
            logger.info(f"Filesystem (Linux): {config.filesystem}")
        if windows_hosts:
            logger.info(f"Mount point (Windows): {config.windows_mount_point}")
            logger.info(f"FIO directory (Windows): {config.windows_fio_dir}")
        logger.info(
            f"Persistent mount: {'ENABLED (will create /etc/fstab entries)' if config.persistent_mount else 'DISABLED (temporary mounts only)'}"
        )
    logger.info(f"Test size: {config.test_size}")
    logger.info(
        "Dataset write: always size-based (full --size, no --runtime); "
        "runtime below applies to FIO performance tests only"
    )
    if config.test_runtime:
        logger.info(f"Test runtime (Linux): {config.test_runtime}s (--time_based)")
    elif linux_hosts:
        logger.info("Test runtime (Linux): omitted (size-based — complete --size then stop)")
    if windows_hosts:
        if config.windows_test_runtime:
            logger.info(f"Test runtime (Windows): {config.windows_test_runtime}s (--time_based)")
        else:
            logger.info("Test runtime (Windows): omitted (size-based — complete --size then stop)")
    logger.info(f"Block sizes: {' '.join(config.block_sizes)}")
    logger.info(f"I/O patterns: {' '.join(config.io_patterns)}")
    if linux_hosts:
        logger.info(
            "FIO packages: "
            f"{'pre-installed (skip package check/install)' if config.fio_installed else 'install via dnf if missing'}"
        )
        logger.info(f"IO engine (Linux): {config.ioengine}")
        logger.info(
            f"fsync (Linux): {config.fsync} (--fsync={config.fsync})"
            if config.fsync else "fsync (Linux): disabled (no --fsync)"
        )
    if windows_hosts:
        logger.info(
            f"fsync (Windows): {config.windows_fsync} (--fsync={config.windows_fsync})"
            if config.windows_fsync else "fsync (Windows): disabled (no --fsync)"
        )
    
    if config.migrate_workloads:
        if config.migrate_interval > 0:
            logger.info(f"VM Migration: ENABLED for patterns: {' '.join(config.migrate_workloads)} "
                       f"(sequential with {config.migrate_interval}s interval)")
        else:
            logger.info(f"VM Migration: ENABLED for patterns: {' '.join(config.migrate_workloads)} (parallel)")
    else:
        logger.info("VM Migration: DISABLED")
    
    if config.dry_run:
        logger.info("DRY RUN MODE: Configuration validated successfully")
        if config.copy_results:
            logger.info("Would execute the following steps:")
            logger.info("  1. Collect test results from all VMs")
            logger.info("  2. Copy log file to results directory (if found)")
        else:
            logger.info("Would execute the following steps:")
            if config.fio_installed:
                logger.info("  1. Skip FIO package check on Linux VMs (fio_installed=true)")
            else:
                logger.info("  1. Install FIO and dependencies on VMs")
            if config.use_testdir:
                logger.info("  2. Create test directories on OS disk (testdir mode, no format)")
            else:
                logger.info("  2. Prepare storage (format and mount devices)")
            logger.info("  3. Write initial test dataset")
            logger.info("  4. Run FIO performance tests")
            logger.info("  5. Collect test results")
            logger.info("  6. Clean up test environment")
        return 0
    
    # Handle copy-results mode
    if config.copy_results:
        logger.info("=== COPY RESULTS MODE ===")
        logger.info("Only copying results from hosts (skipping all other steps)")
        
        # Initialize executor
        executor = CommandExecutor(config)
        
        # Construct results directory name (same as normal flow)
        results_dir = config.get_results_dir_name()
        
        # Try to find existing log file matching the pattern
        log_file_to_copy = None
        sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
        sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')
        if sanitized_desc:
            pattern = f"fio-test-{sanitized_desc}-*.txt"
        else:
            pattern = f"fio-test-*.txt"
        
        # Look for most recent matching log file
        matching_logs = glob.glob(pattern)
        if matching_logs:
            # Sort by modification time, most recent first
            matching_logs.sort(key=os.path.getmtime, reverse=True)
            log_file_to_copy = matching_logs[0]
            logger.info(f"Found existing log file: {log_file_to_copy}")
        else:
            logger.info("No existing log file found matching pattern")
        
        # Copy results only
        collect_results(config, executor, results_dir)
        generate_combined_results(results_dir, config)
        
        # Copy log file to results directory if found
        if log_file_to_copy and os.path.exists(log_file_to_copy):
            try:
                log_destination = os.path.join(results_dir, os.path.basename(log_file_to_copy))
                shutil.copy2(log_file_to_copy, log_destination)
                logger.info(f"Copied log file to results directory: {os.path.basename(log_file_to_copy)}")
            except Exception as e:
                logger.warning(f"Failed to copy log file to results directory: {e}")
        
        logger.info("=== COPY RESULTS COMPLETED ===")
        logger.info(f"Results have been copied to localhost: {results_dir}")
        logger.info("Each VM's results are in separate subdirectories with extracted files")
        return 0
    
    # Handle prepare-machine mode
    if config.prepare_machine:
        if config.fio_installed and not config.get_windows_hosts():
            logger.info("PREPARE MACHINE MODE: Skipping Linux package install (fio_installed=true)")
            logger.info("Golden image already includes FIO and dependencies")
            return 0

        logger.info("PREPARE MACHINE MODE: Installing FIO dependencies only")
        logger.info(f"Using retry configuration: interval={config.retry_interval}s, max_retries={config.max_retries}")
        if not config.skip_connectivity_test:
            logger.info(f"Connectivity checking: ENABLED (will retry up to {config.max_retries} times with {config.retry_interval}s interval)")
        else:
            logger.info("Connectivity checking: DISABLED (--skip-connectivity-test enabled)")
        
        executor = CommandExecutor(config)
        prepare_machine(config, executor)
        logger.info("Machine preparation completed successfully")
        logger.info("FIO and dependencies are now installed on all hosts")
        logger.info("You can now run the full test suite without --prepare-machine")
        return 0
    
    # Initialize executor (single instance reused throughout to share VM host cache)
    executor = CommandExecutor(config)
    
    # Confirmation prompt
    if not config.skip_confirmation:
        print("\n")
        if config.use_testdir:
            logger.warning(
                "WARNING: TESTDIR MODE — FIO will run on the OS disk (not separate data disks)."
            )
            logger.warning(f"Hosts: {' '.join(config.vm_hosts)}")
            if config.get_linux_hosts():
                logger.warning(
                    f"  Linux: data under {config.mount_point}, results under {config.output_dir}"
                )
            if config.get_windows_hosts():
                logger.warning(
                    f"  Windows: data under {config.windows_mount_point}, "
                    f"results under {config.windows_output_dir}"
                )
            logger.warning(
                "Test artifacts under those paths may be removed during cleanup. "
                "YAML storage devices will NOT be formatted."
            )
        else:
            logger.warning("WARNING: This script will format storage devices on all hosts!")
            logger.warning(f"Hosts: {' '.join(config.vm_hosts)}")
            logger.warning("Devices to be formatted:")
            for host in config.vm_hosts:
                if executor.is_windows_host(host):
                    device = config.windows_storage_devices.get(host, "N/A")
                    logger.warning(f"  {host}: {device}")
                else:
                    device = config.storage_devices.get(host, "N/A")
                    logger.warning(f"  {host}: /dev/{device}")
        print("\n")
        confirm = input("Are you sure you want to continue? (yes/no): ")
        if confirm != "yes":
            logger.info("Operation cancelled by user")
            return 0
    
    # Prepare storage FIRST (this formats disks, which would wipe FIO if installed before)
    # For Windows: prepare_storage formats the data disk (d:\), so FIO must be installed AFTER
    # For Linux: FIO is installed to system directories (/usr/bin), so order doesn't matter, but we do it after for consistency
    prepare_storage(config, executor)
    
    # Ensure required packages are installed AFTER storage is prepared
    # This is critical for Windows where FIO is copied to d:\ which gets formatted
    ensure_packages_installed(config, executor)
    
    # Write test data
    write_test_data(config, executor)
    
    # Start VM migration monitor if enabled
    migration_monitor = None
    if config.monitor_vm:
        migration_monitor = VMMigrationMonitor(
            namespace=config.namespace,
            interval=config.monitor_vm_interval,
            vm_hosts=config.vm_hosts
        )
        migration_monitor.start()
    
    # Run FIO tests
    run_fio_tests(config, executor, migration_monitor=migration_monitor)
    
    # Stop VM migration monitor and save report
    if migration_monitor:
        migration_monitor.stop()
    
    # Collect results
    results_dir = config.get_results_dir_name()
    
    collect_results(config, executor, results_dir)
    
    # Write migration report to results directory
    if migration_monitor:
        migration_log_path = os.path.join(results_dir, "migration-events.log")
        migration_monitor.write_report(migration_log_path)
    generate_combined_results(results_dir, config)
    
    # Copy log file to results directory
    log_file_path = None
    # Find the log file that was created (it should be in the current directory)
    sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
    sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')
    if sanitized_desc:
        pattern = f"fio-test-{sanitized_desc}-*.txt"
    else:
        pattern = f"fio-test-*.txt"
    
    # Look for most recent matching log file
    matching_logs = glob.glob(pattern)
    if matching_logs:
        # Sort by modification time, most recent first
        matching_logs.sort(key=os.path.getmtime, reverse=True)
        log_file_path = matching_logs[0]
    
    if log_file_path and os.path.exists(log_file_path):
        try:
            log_destination = os.path.join(results_dir, os.path.basename(log_file_path))
            shutil.copy2(log_file_path, log_destination)
            logger.info(f"Copied log file to results directory: {os.path.basename(log_file_path)}")
        except Exception as e:
            logger.warning(f"Failed to copy log file to results directory: {e}")
    else:
        logger.warning(f"Log file not found (pattern: {pattern}) - skipping log file copy")
    
    # Cleanup
    cleanup_storage(config, executor)
    
    logger.info("FIO performance testing completed successfully")
    logger.info(f"Results have been copied to localhost: {results_dir}")
    return 0



