"""CLI entrypoint and phase orchestration for MariaDB HammerDB tests."""
import argparse
import glob
import logging
import os
import re
import sys
from datetime import datetime

from mariadb_tests.build import build_database
from mariadb_tests.config import (
    ConfigLoader,
    MariaDBConfigError,
    MariaDBTestConfig,
    check_dependencies,
    display_config,
    validate_inputs,
)
from mariadb_tests.deploy import deploy_scripts
from mariadb_tests.executor import CommandExecutor
from mariadb_tests.migration import VMMigrationMonitor
from mariadb_tests.packages import install_dependencies
from mariadb_tests.provision import cleanup_provisioned, provision_vms, remove_ssh_key_dir
from mariadb_tests.results import collect_results
from mariadb_tests.service import stop_mariadb
from mariadb_tests.storage import install_mariadb
from mariadb_tests.workload import run_tests

logger = logging.getLogger("mariadb_tests")

# Root logger setup (same style as original monolith)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def prepare_hosts(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Preparation-only function"""
    logger.info("=== HOST PREPARATION MODE ===")
    logger.info("Starting host preparation phase")
    logger.info("This will install packages, clone repositories, and setup MariaDB")
    logger.info("Performance tests will NOT be executed")

    if config.dry_run:
        logger.info("DRY RUN MODE: Host preparation configuration validated successfully")
        logger.info("Would execute the following preparation steps:")
        logger.info("  1. Install dependencies on VMs")
        logger.info("  2. Deploy HammerDB scripts")
        logger.info("  3. Install MariaDB")
        logger.info("Use without --dry-run to execute the actual preparation")
        return

    logger.info("Running host preparation steps...")
    install_dependencies(config, executor)
    deploy_scripts(config, executor)
    install_mariadb(config, executor)

    logger.info("=== HOST PREPARATION COMPLETED ===")
    logger.info("Host preparation completed successfully")
    logger.info("All hosts are now ready for performance testing")
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. Run the script without --prepare-hosts to execute performance tests")
    logger.info("  2. Or run with --dry-run to validate the test configuration")
    logger.info("")
    logger.info(f"Example: python3 mariadb.py -c {config.config_file}  # Run full performance test")


def main():
    """Main function"""
    parser = argparse.ArgumentParser(
        description="MariaDB HammerDB TPCC Testing Script (YAML Configuration Version)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
    python3 mariadb.py                          # Use default config.yaml
    python3 mariadb.py -c test-config.yaml      # Use custom configuration file
    python3 mariadb.py -c config.yaml -v        # Use default config with verbose output
    python3 mariadb.py --prepare-hosts          # Only prepare hosts (install packages, MariaDB)
    python3 mariadb.py --prepare-hosts -v       # Prepare hosts with verbose output
    python3 mariadb.py --copy-results           # Only copy results from hosts (skip all other steps)
    python3 mariadb.py --create-vms --vms 3 --storage-class ocs-storagecluster-ceph-rbd

YAML CONFIGURATION:
    See config.yaml for configuration file format and examples.

NOTES:
    - Requires PyYAML for YAML parsing
    - Script supports both virtctl (OpenShift VMs) and SSH (baremetal/KVM) access
    - Use --ssh-only for baremetal/KVM hosts, --virtctl-only for OpenShift VMs
    - All operations are performed as root on target hosts

WORKFLOW:
    For large deployments, you can split the process into phases:
    1. Preparation: python3 mariadb.py --prepare-hosts    # Install packages, MariaDB
    2. Testing:     python3 mariadb.py                   # Run performance tests
    3. Copy Results: python3 mariadb.py --copy-results   # Re-copy results without re-running tests

    Or provision + test in one step:
    python3 mariadb.py --create-vms --vms 3 --storage-class <sc> [--cleanup]
        """
    )
    parser.add_argument('-c', '--config', default='config.yaml',
                       help='Path to YAML configuration file (default: config.yaml)')
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Verbose output')
    parser.add_argument('--dry-run', action='store_true',
                       help='Validate configuration and show what would be done without executing')
    parser.add_argument('--prepare-hosts', action='store_true',
                       help='Only run preparation steps (install packages, deploy scripts, MariaDB setup)')
    parser.add_argument('--copy-results', action='store_true',
                       help='Only copy results from hosts (skip installation, building, and testing)')
    parser.add_argument('--ssh-only', action='store_true',
                       help='Force SSH for all hosts (baremetal/KVM, no virtctl)')
    parser.add_argument('--virtctl-only', action='store_true',
                       help='Force virtctl for all hosts (OpenShift VMs)')
    parser.add_argument('--monitor-vm', action='store_true',
                       help='Monitor VM node placement during tests and log migrations')
    parser.add_argument('--monitor-vm-interval', type=int, default=10,
                       help='VM monitor polling interval in seconds (default: 10)')

    # On-demand VM provision (Linux only)
    parser.add_argument(
        '--create-vms',
        action='store_true',
        help='Provision managed Linux VMs before the test. Requires --storage-class and --vms.',
    )
    parser.add_argument(
        '--cleanup',
        action='store_true',
        help='Delete managed VMs/DataVolumes after the test (default: keep)',
    )
    parser.add_argument('--vms', type=int, default=None, help='Number of Linux VMs to create')
    parser.add_argument('--vm-cpu', default=None, help='vCPU cores per VM')
    parser.add_argument('--vm-memory', default=None, help='Memory per VM (e.g. 4Gi)')
    parser.add_argument('--vm-image', default=None, help='Guest OS containerdisk (registry)')
    parser.add_argument('--storage-class', default=None, help='StorageClass for DV/PVC')
    parser.add_argument('--access-mode', default=None, help='PVC access mode (default ReadWriteMany)')
    parser.add_argument('--root-volume-size', default=None, help='Root PVC size')
    parser.add_argument('--data-disk-size', default=None, help='Blank data disk size')
    parser.add_argument('--vm-prefix', default=None, help='VM name prefix (→ prefix-1..N)')
    parser.add_argument(
        '--ssh-key-path',
        default=None,
        help='Directory for ephemeral SSH keypair (default: /tmp/mariadb-ssh-<runid>)',
    )
    parser.add_argument('--namespace', default=None, help='Override OpenShift namespace')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.prepare_hosts:
        logger.info("Starting MariaDB HammerDB TPCC testing script (PREPARATION MODE)")
    else:
        logger.info("Starting MariaDB HammerDB TPCC testing script (FULL TEST MODE)")

    config = MariaDBTestConfig()
    config.config_file = args.config
    config.dry_run = args.dry_run
    config.verbose = args.verbose
    config.use_virtctl = None if not (args.ssh_only or args.virtctl_only) else (not args.ssh_only)
    config.prepare_hosts = args.prepare_hosts
    config.copy_results = args.copy_results
    config.monitor_vm = args.monitor_vm
    config.monitor_vm_interval = args.monitor_vm_interval
    config.create_vms = args.create_vms
    config.cleanup_vms = args.cleanup
    if args.vms is not None:
        config.vms_count = args.vms
    if args.vm_cpu is not None:
        config.vm_cpu = args.vm_cpu
    if args.vm_memory is not None:
        config.vm_memory = args.vm_memory
    if args.vm_image is not None:
        config.vm_image = args.vm_image
    if args.storage_class is not None:
        config.storage_class = args.storage_class
    if args.access_mode is not None:
        config.access_mode = args.access_mode
    if args.root_volume_size is not None:
        config.root_volume_size = args.root_volume_size
    if args.data_disk_size is not None:
        config.data_disk_size = args.data_disk_size
    if args.vm_prefix is not None:
        config.vm_prefix = args.vm_prefix
    if args.ssh_key_path is not None:
        config.ssh_key_path = args.ssh_key_path

    log_file = None
    try:
        config_loader = ConfigLoader(config)
        config_loader.load_config()
        if args.namespace:
            config.namespace = args.namespace

        # Set up log file early so provision + wait appear in the same run log
        log_date = datetime.now().strftime('%Y%m%d')
        sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
        sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')

        if sanitized_desc:
            log_file = f"mariadb-{log_date}-{sanitized_desc}.txt"
        else:
            log_file = f"mariadb-{log_date}.txt"

        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s',
                                                    datefmt='%Y-%m-%d %H:%M:%S'))
        logging.getLogger().addHandler(file_handler)

        logger.info(f"Logging all output to: {log_file}")
        if config.description:
            logger.info("=" * 80)
            logger.info(f"TEST DESCRIPTION: {config.description}")
            logger.info("=" * 80)

        check_dependencies(config)

        # Phase 1: provision + wait Ready/SSH BEFORE guest work
        if config.create_vms and not config.copy_results:
            if config.use_virtctl is False:
                logger.error("CRITICAL: --create-vms requires virtctl mode (not --ssh-only)")
                return 1
            logger.info("=" * 80)
            logger.info(
                f"PHASE 1: Provision VMs (linux={config.vms_count}) "
                "— wait until Ready and SSH before continuing"
            )
            logger.info("=" * 80)
            provision_vms(config)
            logger.info(f"PHASE 1 complete: hosts ready: {' '.join(config.db_hosts)}")

        if not config.db_hosts and not config.dry_run and not config.copy_results:
            raise MariaDBConfigError("CRITICAL: No hosts configured after load/provision")

        display_config(config)
        validate_inputs(config)

    except MariaDBConfigError as e:
        logger.error(str(e))
        remove_ssh_key_dir(getattr(config, "ssh_key_path", None), also_bastion_mirror=False)
        return 1
    except SystemExit:
        remove_ssh_key_dir(getattr(config, "ssh_key_path", None), also_bastion_mirror=False)
        raise
    except Exception:
        remove_ssh_key_dir(getattr(config, "ssh_key_path", None), also_bastion_mirror=False)
        raise

    executor = CommandExecutor(config)

    def _maybe_cleanup():
        try:
            if config.create_vms and config.cleanup_vms:
                cleanup_provisioned(config)
            if config.create_vms:
                remove_ssh_key_dir(
                    getattr(config, "ssh_key_path", None),
                    also_bastion_mirror=bool(config.cleanup_vms),
                )
        except Exception as cleanup_err:
            logger.warning(f"Cleanup warning: {cleanup_err}")

    if config.dry_run:
        logger.info("DRY RUN MODE: Configuration validated successfully")
        if config.create_vms:
            logger.info(
                f"Would provision VMs: linux={config.vms_count}, "
                f"storage_class={config.storage_class}, cleanup_after={config.cleanup_vms}"
            )
        if config.copy_results:
            logger.info("Would execute the following steps:")
            logger.info("  1. Collect test results from all VMs")
            logger.info("  2. Copy log file to results directory (if found)")
        elif config.prepare_hosts:
            logger.info("Would execute the following preparation steps:")
            step = 1
            if config.create_vms:
                logger.info(f"  {step}. Provision managed Linux VMs (--create-vms)")
                step += 1
            logger.info(f"  {step}. Install dependencies on VMs")
            step += 1
            logger.info(f"  {step}. Deploy HammerDB scripts")
            step += 1
            logger.info(f"  {step}. Install MariaDB")
            if config.create_vms and config.cleanup_vms:
                step += 1
                logger.info(f"  {step}. Delete managed VMs/DataVolumes (--cleanup)")
        else:
            logger.info("Would execute the following steps:")
            step = 1
            if config.create_vms:
                logger.info(f"  {step}. Provision managed Linux VMs (--create-vms)")
                step += 1
            logger.info(f"  {step}. Install dependencies on VMs")
            step += 1
            logger.info(f"  {step}. Deploy HammerDB scripts")
            step += 1
            logger.info(f"  {step}. Install MariaDB")
            step += 1
            logger.info(f"  {step}. Build TPCC database")
            step += 1
            logger.info(f"  {step}. Run performance tests")
            step += 1
            logger.info(f"  {step}. Collect test results from all VMs")
            step += 1
            logger.info(f"  {step}. Stop MariaDB instances and cleanup storage")
            if config.create_vms and config.cleanup_vms:
                step += 1
                logger.info(f"  {step}. Delete managed VMs/DataVolumes (--cleanup)")
        logger.info("Use without --dry-run to execute the actual tests")
        return 0

    try:
        if config.prepare_hosts:
            prepare_hosts(config, executor)
            return 0

        if config.copy_results:
            logger.info("=== COPY RESULTS MODE ===")
            logger.info("Only copying results from hosts (skipping all other steps)")

            results_timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
            sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')

            if sanitized_desc:
                final_results_dir = f"./mariadb-results-{results_timestamp}-{sanitized_desc}"
            else:
                final_results_dir = f"./mariadb-results-{results_timestamp}"

            log_file_to_copy = None
            if sanitized_desc:
                pattern = f"mariadb-*-{sanitized_desc}.txt"
            else:
                pattern = f"mariadb-*.txt"

            matching_logs = glob.glob(pattern)
            if matching_logs:
                matching_logs.sort(key=os.path.getmtime, reverse=True)
                log_file_to_copy = matching_logs[0]
                logger.info(f"Found existing log file: {log_file_to_copy}")
            else:
                logger.info("No existing log file found matching pattern")

            collect_results(config, executor, final_results_dir, log_file_to_copy)

            logger.info("=== COPY RESULTS COMPLETED ===")
            logger.info(f"Results have been copied to localhost: {final_results_dir}")
            logger.info("Each VM's results are in separate subdirectories with extracted files")
            return 0

        # Full test execution (guest work only after VMs exist when create_vms)
        install_dependencies(config, executor)
        deploy_scripts(config, executor)
        install_mariadb(config, executor)
        build_database(config, executor)

        migration_monitor = None
        if config.monitor_vm:
            migration_monitor = VMMigrationMonitor(
                namespace=config.namespace,
                interval=config.monitor_vm_interval,
                vm_hosts=config.db_hosts
            )
            migration_monitor.start()

        run_tests(config, executor, migration_monitor=migration_monitor)

        if migration_monitor:
            migration_monitor.stop()

        results_timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        sanitized_desc = re.sub(r'[^a-z0-9]', '_', config.description.lower()) if config.description else ""
        sanitized_desc = re.sub(r'_+', '_', sanitized_desc).strip('_')

        if sanitized_desc:
            final_results_dir = f"./mariadb-results-{results_timestamp}-{sanitized_desc}"
        else:
            final_results_dir = f"./mariadb-results-{results_timestamp}"

        collect_results(config, executor, final_results_dir, log_file)

        if migration_monitor:
            migration_log_path = os.path.join(final_results_dir, "migration-events.log")
            migration_monitor.write_report(migration_log_path)

        stop_mariadb(config, executor)

        logger.info("MariaDB performance testing completed successfully")
        logger.info(f"Results have been copied to localhost: {final_results_dir}")
        logger.info("Each VM's results are in separate subdirectories with extracted files")
        return 0
    finally:
        _maybe_cleanup()


if __name__ == "__main__":
    sys.exit(main())
