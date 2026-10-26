"""MariaDB test configuration and YAML loading."""
import logging
import os
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from mariadb_tests.util import _find_mariadb_templates_dir

logger = logging.getLogger("mariadb_tests")


class MariaDBConfigError(Exception):
    """Configuration / validation error."""

class MariaDBTestConfig:
    """Configuration class for MariaDB tests"""
    
    def __init__(self):
        self.config_file = "config.yaml"
        self.dry_run = False
        self.verbose = False
        self.prepare_hosts = False
        self.use_virtctl = None  # None = auto-detect, True = force virtctl, False = force SSH
        self.namespace = None
        self.db_hosts = []
        self.mount_point = None
        self.disk_list = None
        self.warehouse_count = None
        self.user_count = []
        self.hammerdb_source = "bundled"
        self.hammerdb_bundled_path = "/work/hammerdb-bundled"
        self.hammerdb_repo = None
        self.hammerdb_path = None
        self.hammerdb_dir = None
        self.test_duration = None
        self.rampup_time = None
        self.log_level = "INFO"
        self.description = ""  # Test description for logging and output naming
        self.migrate_user_counts = []  # User counts that should trigger migration
        self.migrate_interval = 0  # Interval between migrations (0 = parallel)
        self.persistent_mount = False  # Whether to create /etc/fstab entries
        self.copy_results = False  # Whether to only copy results (skip all other steps)
        # Retry configuration
        self.retry_interval = 30  # Retry interval in seconds
        self.max_retries = 10  # Maximum number of retry attempts
        self.skip_connectivity_test = False  # Skip initial connectivity test
        # Monitoring configuration
        self.task_monitor_interval = 60  # Check task status every N seconds for long-running tasks
        self.build_time_limit = 14400  # Hard limit for database build (4 hours)
        self.build_poll_interval = 30  # Seconds between build status polls
        self.build_check_timeout = 60  # Per-poll virtctl/SSH timeout during build monitoring
        self.build_check_max_retries = 1  # Avoid retry storms during build polling
        self.monitor_vm = False
        self.monitor_vm_interval = 10
        # Sweep iterations (repeat full user_count list). Same knobs as mssql-container.
        self.test_iterations = 1
        self.iteration_sleep_sec = 60
        self.vu_sleep_sec = 30
        # On-demand VM provision (--create-vms)
        self.create_vms = False
        self.cleanup_vms = False
        self.vms_count = 0
        self.vm_cpu = None
        self.vm_memory = None
        self.vm_image = None
        self.storage_class = None
        self.access_mode = None
        self.root_volume_size = None
        self.data_disk_size = None
        self.vm_prefix = None
        self.ssh_key_path = None
        self.ssh_identity_file = "/root/.ssh/id_rsa"
        self.ssh_key_bastion_path = None
        self.provision_run_id = None

    def max_pool_workers(self, task_count: int) -> int:
        """Cap thread-pool parallelism by fleet size: <100→50, 100–500→100, >500→200."""
        fleet_size = len(self.db_hosts)
        if fleet_size > 500:
            cap = 200
        elif fleet_size >= 100:
            cap = 100
        else:
            cap = 50
        return min(task_count, cap)


class ConfigLoader:
    """Loads and validates configuration from YAML file"""
    
    def __init__(self, config: MariaDBTestConfig):
        self.config = config
    
    def load_config(self) -> None:
        """Load configuration from YAML file"""
        if not os.path.exists(self.config.config_file):
            logger.error(f"Configuration file '{self.config.config_file}' not found")
            sys.exit(1)
        
        with open(self.config.config_file, 'r') as f:
            yaml_data = yaml.safe_load(f)
        
        # Load namespace
        if self.config.use_virtctl is not False:
            self.config.namespace = yaml_data.get('database', {}).get('namespace', 'default')
            if self.config.namespace == "null" or not self.config.namespace:
                self.config.namespace = "default"
        else:
            self.config.namespace = "N/A"
        
        # Load VM hosts. With --create-vms, hosts come from provision after Ready+SSH.
        if self.config.create_vms:
            self.config.db_hosts = []
            logger.info(
                "CREATE_VMS: deferring host list until VMs are Ready and SSH-reachable "
                "(YAML host_pattern / hosts / host_labels ignored)"
            )
        else:
            self.config.db_hosts = self._get_db_hosts(yaml_data)
        
        # Load storage configuration
        storage = yaml_data.get('storage', {})
        self.config.mount_point = storage.get('mount_point')
        if self.config.mount_point == "null" or not self.config.mount_point:
            self.config.mount_point = "none"
        
        self.config.disk_list = storage.get('disk_list')
        if self.config.disk_list == "null" or not self.config.disk_list:
            self.config.disk_list = "none"
        # Device map filled after provision when create_vms
        if self.config.create_vms and self.config.disk_list == "none" and self.config.mount_point == "none":
            logger.info(
                "CREATE_VMS: storage.disk_list/mount_point unset — will default to data disk "
                "device after provision"
            )
        
        # Load persistent mount option (optional, defaults to False)
        persistent = storage.get('persistent', False)
        if persistent == "true" or persistent is True:
            self.config.persistent_mount = True
        else:
            self.config.persistent_mount = False
        
        # Load database configuration
        database = yaml_data.get('database', {})
        self.config.warehouse_count = database.get('warehouse_count')
        self.config.test_duration = database.get('test_duration')
        self.config.rampup_time = database.get('rampup_time')
        # Sweep iterations (repeat full user_count list)
        raw_iters = database.get("iterations", database.get("test_iterations"))
        if raw_iters is not None and raw_iters != "null":
            try:
                self.config.test_iterations = max(1, int(raw_iters))
            except (TypeError, ValueError):
                logger.warning(f"Invalid database.iterations={raw_iters!r}; using 1")
                self.config.test_iterations = 1
        raw_sleep = database.get(
            "iteration_sleep_sec",
            database.get(
                "iteration_sleep",
                database.get(
                    "sweep_sleep_sec",
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

        # Load test configuration
        test = yaml_data.get('test', {})
        user_count = test.get('user_count')
        if isinstance(user_count, str):
            self.config.user_count = user_count.split()
        elif isinstance(user_count, list):
            self.config.user_count = [str(u) for u in user_count]
        else:
            self.config.user_count = [str(user_count)] if user_count else []
        
        self.config.log_level = test.get('log_level', 'INFO')
        if self.config.log_level == "null" or not self.config.log_level:
            self.config.log_level = "INFO"
        
        # Load migration configuration
        migrate = yaml_data.get('migrate')
        if migrate is None or migrate == "null":
            self.config.migrate_user_counts = []
            self.config.migrate_interval = 0
        else:
            migrate_user_counts = migrate.get('user_counts', '')
            if migrate_user_counts and migrate_user_counts != "null":
                if isinstance(migrate_user_counts, str):
                    self.config.migrate_user_counts = migrate_user_counts.split()
                elif isinstance(migrate_user_counts, list):
                    self.config.migrate_user_counts = [str(u) for u in migrate_user_counts]
                else:
                    self.config.migrate_user_counts = []
            else:
                self.config.migrate_user_counts = []
            
            migrate_interval = migrate.get('interval', 0)
            if migrate_interval == "null" or not migrate_interval:
                self.config.migrate_interval = 0
            else:
                self.config.migrate_interval = int(migrate_interval)
        
        # Load description (top-level)
        self.config.description = yaml_data.get('description', '')
        if self.config.description == "null" or not self.config.description:
            self.config.description = ""
        # run_name deprecated; description is the single source for labeling
        
        # Load HammerDB configuration
        hammerdb = yaml_data.get('hammerdb', {})
        hammerdb_source = hammerdb.get('source', 'bundled')
        if hammerdb_source == "null" or not hammerdb_source:
            hammerdb_source = 'bundled'
        self.config.hammerdb_source = str(hammerdb_source).lower()
        self.config.hammerdb_bundled_path = hammerdb.get('bundled_path', '/work/hammerdb-bundled')
        if self.config.hammerdb_bundled_path == "null" or not self.config.hammerdb_bundled_path:
            self.config.hammerdb_bundled_path = "/work/hammerdb-bundled"
        self.config.hammerdb_repo = hammerdb.get('repo')
        self.config.hammerdb_path = hammerdb.get('path', '/root/hammerdb-tpcc-wrapper-scripts')
        if self.config.hammerdb_path == "null" or not self.config.hammerdb_path:
            self.config.hammerdb_path = '/root/hammerdb-tpcc-wrapper-scripts'
        self.config.hammerdb_dir = hammerdb.get('install_dir', '/usr/local/HammerDB')
        if self.config.hammerdb_dir == "null" or not self.config.hammerdb_dir:
            self.config.hammerdb_dir = "/usr/local/HammerDB"
        if self.config.hammerdb_source == 'remote_git' and not self.config.hammerdb_repo:
            logger.error("hammerdb.repo is required when hammerdb.source is remote_git")
            sys.exit(1)
        
        # Load retry configuration
        retry = yaml_data.get('retry', {})
        self.config.retry_interval = retry.get('interval', 30)
        if self.config.retry_interval == "null" or not self.config.retry_interval:
            self.config.retry_interval = 30
        else:
            self.config.retry_interval = int(self.config.retry_interval)
        
        self.config.max_retries = retry.get('max_retries', 10)
        if self.config.max_retries == "null" or not self.config.max_retries:
            self.config.max_retries = 10
        else:
            self.config.max_retries = int(self.config.max_retries)
        
        skip_connectivity = retry.get('skip_connectivity_test', False)
        if skip_connectivity == "true" or skip_connectivity is True:
            self.config.skip_connectivity_test = True
        else:
            self.config.skip_connectivity_test = False
        
        # Load monitoring configuration
        monitoring = yaml_data.get('monitoring', {})
        self.config.task_monitor_interval = monitoring.get('task_monitor_interval', 60)
        if self.config.task_monitor_interval == "null" or not self.config.task_monitor_interval:
            self.config.task_monitor_interval = 60
        else:
            self.config.task_monitor_interval = int(self.config.task_monitor_interval)
        
        # Load build time limit (hard timeout for database build monitoring)
        self.config.build_time_limit = monitoring.get('build_time_limit', 14400)
        if self.config.build_time_limit == "null" or not self.config.build_time_limit:
            self.config.build_time_limit = 14400
        else:
            self.config.build_time_limit = int(self.config.build_time_limit)

        self.config.build_poll_interval = monitoring.get('build_poll_interval', 30)
        if self.config.build_poll_interval == "null" or not self.config.build_poll_interval:
            self.config.build_poll_interval = 30
        else:
            self.config.build_poll_interval = int(self.config.build_poll_interval)

        self.config.build_check_timeout = monitoring.get('build_check_timeout', 60)
        if self.config.build_check_timeout == "null" or not self.config.build_check_timeout:
            self.config.build_check_timeout = 60
        else:
            self.config.build_check_timeout = int(self.config.build_check_timeout)

        self.config.build_check_max_retries = monitoring.get('build_check_max_retries', 1)
        if self.config.build_check_max_retries == "null" or self.config.build_check_max_retries == "":
            self.config.build_check_max_retries = 1
        else:
            self.config.build_check_max_retries = int(self.config.build_check_max_retries)
    
    def _get_db_hosts(self, yaml_data: Dict) -> List[str]:
        """Get database hosts from various methods"""
        database = yaml_data.get('database', {})
        
        # Method 1: Host pattern
        host_pattern = database.get('host_pattern')
        if host_pattern:
            logger.info(f"Using host pattern: {host_pattern}")
            # Expand pattern like db{1..200}
            if '{' in host_pattern and '..' in host_pattern:
                match = re.search(r'([\w-]+)\{(\d+)\.\.(\d+)\}', host_pattern)
                if match:
                    prefix = match.group(1)
                    start = int(match.group(2))
                    end = int(match.group(3))
                    expanded = [f"{prefix}{i}" for i in range(start, end + 1)]
                    logger.info(f"Expanded pattern to {len(expanded)} hosts")
                    return expanded
                else:
                    logger.warning(f"Could not parse host pattern '{host_pattern}' - using as-is")
            return [host_pattern]
        
        # Method 2: Host labels
        host_labels = database.get('host_labels')
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
                        timeout=30
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        hosts = result.stdout.strip().split()
                        logger.info(f"Found {len(hosts)} VMs matching labels: {host_labels}")
                        return hosts
                except Exception as e:
                    logger.warning(f"Failed to query VMs by labels: {e}")
            else:
                logger.info(f"Dry-run mode: Would query VMs with labels: {host_labels}")
                return ["example-db1", "example-db2"]
        
        # Method 3: Host file
        host_file = database.get('host_file')
        if host_file:
            logger.info(f"Using host file: {host_file}")
            if os.path.exists(host_file):
                hosts = []
                with open(host_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            # Handle patterns in file
                            if '{' in line and '..' in line:
                                match = re.search(r'([\w-]+)\{(\d+)\.\.(\d+)\}', line)
                                if match:
                                    prefix = match.group(1)
                                    start = int(match.group(2))
                                    end = int(match.group(3))
                                    hosts.extend([f"{prefix}{i}" for i in range(start, end + 1)])
                            else:
                                hosts.append(line)
                if hosts:
                    logger.info(f"Loaded {len(hosts)} hosts from file: {host_file}")
                    return hosts
        
        # Method 4: Simple host list
        hosts = database.get('hosts')
        if hosts:
            logger.info(f"Using simple host list: {hosts}")
            if isinstance(hosts, str):
                return hosts.split()
            return hosts
        
        logger.error(f"No hosts specified in configuration. Use one of: hosts, host_pattern, host_labels, or host_file")
        sys.exit(1)


def check_dependencies(config: MariaDBTestConfig) -> None:
    """Check if required tools are installed"""
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
        if config.create_vms and not shutil.which("ssh-keygen"):
            missing_tools.append("ssh-keygen")
    
    if missing_tools:
        logger.error("The following required tools are missing:")
        for tool in missing_tools:
            if tool == "virtctl":
                logger.error("  - virtctl: Install from https://kubevirt.io/user-guide/operations/virtctl_client_tool/")
                logger.error("    Or if using kubectl: 'kubectl krew install virt'")
            elif tool == "oc":
                logger.error("  - oc: Install OpenShift CLI from https://openshift.com/download")
            elif tool == "ssh":
                logger.error("  - ssh: Install OpenSSH client")
            elif tool == "ssh-keygen":
                logger.error("  - ssh-keygen: Required for --create-vms ephemeral SSH keys")
        logger.error("")
        logger.error("Install the missing tools and try again.")
        sys.exit(1)
    

def validate_inputs(config: MariaDBTestConfig) -> None:
    """Validate configuration inputs"""
    if config.create_vms:
        if not config.storage_class:
            logger.error("CRITICAL: --storage-class / STORAGE_CLASS is required for --create-vms")
            sys.exit(1)
        if config.vms_count < 1:
            logger.error("CRITICAL: --create-vms requires --vms / VMS >= 1")
            sys.exit(1)
        if config.use_virtctl is False:
            logger.error("CRITICAL: --create-vms requires virtctl mode (not --ssh-only)")
            sys.exit(1)

    # After provision (or pre-created path) storage must be set
    if config.mount_point == "none" and config.disk_list == "none":
        if config.create_vms and not config.db_hosts:
            # Still deferred until provision fills disk_list
            pass
        else:
            logger.error("Either storage.disk_list or storage.mount_point must be specified in config")
            sys.exit(1)

    if not config.dry_run and config.hammerdb_source == 'bundled':
        try:
            _find_mariadb_templates_dir(config.hammerdb_bundled_path)
        except FileNotFoundError as exc:
            logger.error(str(exc))
            logger.error("Rebuild the container image or set hammerdb.source to remote_git")
            sys.exit(1)

    if not config.db_hosts:
        if config.dry_run and config.create_vms:
            logger.info("Skipping host validation (CREATE_VMS dry-run — hosts not provisioned yet)")
            return
        if not config.create_vms:
            logger.error("No database hosts configured")
            sys.exit(1)

    # Validate hosts are reachable (skip in dry-run mode and SSH-only mode)
    if not config.dry_run and config.use_virtctl is not False and config.db_hosts:
        from mariadb_tests.executor import CommandExecutor
        executor = CommandExecutor(config)
        for host in config.db_hosts:
            if executor.is_vm_host(host):
                try:
                    result = subprocess.run(
                        ["oc", "get", "vm", host, "-n", config.namespace],
                        capture_output=True,
                        timeout=10
                    )
                    if result.returncode != 0:
                        logger.error(f"Virtual machine '{host}' not found in namespace '{config.namespace}'")
                        sys.exit(1)
                except Exception as e:
                    logger.error(f"Failed to validate VM '{host}': {e}")
                    sys.exit(1)
    else:
        if config.dry_run:
            logger.info("Skipping host validation in dry-run mode")
        elif config.use_virtctl is False:
            logger.info("Skipping VM validation in SSH-only mode")


def display_config(config: MariaDBTestConfig) -> None:
    """Display configuration"""
    logger.info(f"Configuration loaded from: {config.config_file}")
    logger.info(f"Hosts: {' '.join(config.db_hosts) if config.db_hosts else '(pending provision)'}")
    if config.use_virtctl is not False:
        logger.info(f"Namespace: {config.namespace}")
    else:
        logger.info("Namespace: N/A (SSH-only mode)")
    if config.create_vms:
        logger.info(
            f"Provision mode: create-vms (vms={config.vms_count}, "
            f"cleanup_after={'yes' if config.cleanup_vms else 'no/keep'})"
        )
        logger.info(f"SSH identity file: {config.ssh_identity_file}")
        if config.ssh_key_bastion_path:
            logger.info(
                f"Bastion SSH key (for virtctl from host): "
                f"{config.ssh_key_bastion_path}/id_ed25519"
            )
    logger.info(f"Warehouse count: {config.warehouse_count}")
    logger.info(f"User counts: {' '.join(config.user_count)}")
    logger.info(f"Test duration: {config.test_duration} minutes")
    logger.info(f"Sweep iterations: {config.test_iterations}")
    logger.info(
        f"Sleeps: vu_sleep={config.vu_sleep_sec}s between VU levels, "
        f"iteration_sleep={config.iteration_sleep_sec}s between iterations"
    )
    if config.rampup_time:
        logger.info(f"Rampup time: {config.rampup_time} minutes")
    else:
        logger.info("Rampup time: 2 minutes (default)")
    if config.disk_list != "none":
        logger.info(f"Disk device: {config.disk_list}")
    if config.mount_point != "none":
        logger.info(f"Mount point: {config.mount_point}")
    logger.info(f"Persistent mount: {'ENABLED (will create /etc/fstab entries)' if config.persistent_mount else 'DISABLED (temporary mounts only)'}")
    logger.info(f"HammerDB source: {config.hammerdb_source}")
    if config.hammerdb_source == 'bundled':
        logger.info(f"HammerDB bundled path: {config.hammerdb_bundled_path}")
    elif config.hammerdb_repo:
        logger.info(f"HammerDB repo: {config.hammerdb_repo}")
    logger.info(f"HammerDB path: {config.hammerdb_path}")
    logger.info(f"HammerDB install dir: {config.hammerdb_dir}")
    logger.info(f"Log level: {config.log_level}")
    logger.info(f"Retry interval: {config.retry_interval}s")
    logger.info(f"Thread pool cap: {config.max_pool_workers(len(config.db_hosts))} "
                f"(fleet size {len(config.db_hosts)})")
    logger.info(f"Max retries: {config.max_retries}")
    logger.info(f"Skip connectivity test: {'ENABLED' if config.skip_connectivity_test else 'DISABLED'}")
    logger.info(f"Task monitor interval: {config.task_monitor_interval}s")
    logger.info(f"Build time limit: {config.build_time_limit}s")
    logger.info(f"Build poll interval: {config.build_poll_interval}s")
    logger.info(f"Build check timeout: {config.build_check_timeout}s (max_retries={config.build_check_max_retries})")
    if config.migrate_user_counts:
        if config.migrate_interval > 0:
            logger.info(f"VM Migration: ENABLED for user_counts: {' '.join(config.migrate_user_counts)} "
                      f"(sequential with {config.migrate_interval}s interval)")
        else:
            logger.info(f"VM Migration: ENABLED for user_counts: {' '.join(config.migrate_user_counts)} (parallel)")
    else:
        logger.info("VM Migration: DISABLED")
