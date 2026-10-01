"""Configuration: FioTestConfig, ConfigLoader, and --testdir overrides."""

import logging
import os
import re
import sys
from datetime import datetime
from typing import Dict, List, Optional

try:
    import yaml
except ImportError:
    logging.getLogger("fio_tests").error("PyYAML is required but not installed.")
    logging.getLogger("fio_tests").error("Please install dependencies first:")
    logging.getLogger("fio_tests").error("  pip install -r requirements.txt")
    logging.getLogger("fio_tests").error("Or install PyYAML directly:")
    logging.getLogger("fio_tests").error("  pip install PyYAML>=5.4.1")
    sys.exit(1)

from fio_tests.constants import (
    CHECK_INTERVAL,
    CONNECTIVITY_TIMEOUT,
    DATASET_STALL_SECONDS,
    DATASET_WRITE_BUFFER,
    DATASET_WRITE_RETRIES,
    DEFAULT_LINUX_IOENGINE,
    DEFAULT_MAX_WORKERS,
    DEFAULT_TIMEOUT,
    LINUX_TESTDIR,
    MIGRATION_TIMEOUT,
    NOHUP_SETUP_TIMEOUT,
    PROCESS_CHECK_TIMEOUT,
    QUICK_TIMEOUT,
    RUNTIME_BUFFER,
    SCP_TIMEOUT,
    WINDOWS_FIO_DIR_TESTDIR,
    WINDOWS_OUTPUT_TESTDIR,
    WINDOWS_TESTDIR,
)
from fio_tests.util import (
    normalize_optional_fsync,
    normalize_windows_path,
    parse_bool,
    parse_optional_runtime,
)

logger = logging.getLogger("fio_tests")

class FioConfigError(Exception):
    """Raised when configuration loading or validation fails."""
    pass


def apply_testdir_overrides(config: "FioTestConfig") -> None:
    """
    Override FIO data/output paths for OS-disk testing (--testdir).

    storage / storage_win device mappings and mount points from YAML are ignored.
    """
    if not config.use_testdir:
        return

    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()

    # Drop any device maps loaded from YAML — unused in this mode.
    config.storage_devices = {h: "unused" for h in linux_hosts}
    config.windows_storage_devices = {}

    if linux_hosts:
        config.mount_point = LINUX_TESTDIR

    if windows_hosts:
        config.windows_mount_point = normalize_windows_path(WINDOWS_TESTDIR)
        config.windows_fio_dir = normalize_windows_path(WINDOWS_FIO_DIR_TESTDIR)
        if not config.windows_fio_dir.endswith('/'):
            config.windows_fio_dir += '/'
        config.windows_output_dir = normalize_windows_path(WINDOWS_OUTPUT_TESTDIR)

    logger.info(
        "TESTDIR MODE: FIO runs on OS disk — no separate disk format/provision. "
        "YAML storage / storage_win devices and mount points are ignored."
    )
    if linux_hosts:
        logger.info(f"  Linux FIO data directory: {LINUX_TESTDIR}")
        logger.info(f"  Linux results directory: {config.output_dir} (from YAML)")
    if windows_hosts:
        logger.info(f"  Windows FIO data directory: {config.windows_mount_point}")
        logger.info(f"  Windows FIO executable dir: {config.windows_fio_dir}")
        logger.info(f"  Windows results directory: {config.windows_output_dir}")




class FioTestConfig:
    """Configuration class for FIO tests"""
    
    def __init__(self):
        # These values must be set in YAML config (no defaults to avoid masking)
        self.config_file = "fio-config.yaml"
        self.dry_run = False
        self.verbose = False
        self.use_virtctl = None  # None = auto-detect, True = force virtctl, False = force SSH
        self.skip_confirmation = False
        self.prepare_machine = False
        self.retry_interval = None
        self.max_retries = None
        self.skip_connectivity_test = False
        self.task_monitor_interval = None
        self.debug_config = False
        self.namespace = None
        self.vm_hosts = []
        # These values must be set in YAML config (no defaults to avoid masking)
        self.mount_point = None
        self.filesystem = None
        self.test_size = None
        self.test_runtime = None
        self.block_sizes = []
        self.io_patterns = []
        self.numjobs = 1
        self.iodepth = 1
        self.ioengine = DEFAULT_LINUX_IOENGINE
        self.direct_io = "1"
        self.fsync = None  # Optional: FIO --fsync=N (None = omit)
        self.rate_iops = None
        self.fio_installed = False
        self.output_dir = None
        self.output_format = None
        self.description = ""
        self.migrate_workloads = []
        self.migrate_interval = 0
        self.storage_devices = {}  # host -> device mapping
        self.persistent_mount = False  # Whether to create /etc/fstab entries
        self.copy_results = False  # Whether to only copy results (skip all other steps)
        self.use_testdir = False  # OS-disk mode: fixed testdir paths, no data-disk format
        self.windows_hosts = set()  # Set of Windows hostnames
        # Windows-specific configuration (optional, only used if windows_hosts is set)
        self.windows_storage_devices = {}  # host -> device mapping for Windows
        self.windows_mount_point = None
        self.windows_fio_dir = None
        self.windows_test_size = None
        self.windows_test_runtime = None
        self.windows_block_sizes = []
        self.windows_io_patterns = []
        self.windows_numjobs = 1
        self.windows_iodepth = 1
        self.windows_direct_io = "1"
        self.windows_fsync = None  # Optional: FIO --fsync=N (None = omit)
        self.windows_rate_iops = None
        self.windows_output_dir = None
        self.windows_output_format = None
        self.timeout_default = DEFAULT_TIMEOUT
        self.timeout_quick = QUICK_TIMEOUT
        self.timeout_process_check = PROCESS_CHECK_TIMEOUT
        self.timeout_connectivity = CONNECTIVITY_TIMEOUT
        self.timeout_runtime_buffer = RUNTIME_BUFFER
        self.timeout_nohup_setup = NOHUP_SETUP_TIMEOUT
        self.timeout_scp = SCP_TIMEOUT
        self.timeout_dataset_buffer = DATASET_WRITE_BUFFER
        self.timeout_dataset_stall = DATASET_STALL_SECONDS
        self.timeout_dataset_hard = None  # computed from runtime if unset
        self.dataset_write_retries = DATASET_WRITE_RETRIES
        self.timeout_check_interval = CHECK_INTERVAL
        self.timeout_migration = MIGRATION_TIMEOUT
        self.monitor_vm = False
        self.monitor_vm_interval = 10
        self.migration_report = False
        self.max_workers_cli = None

    @property
    def max_workers(self) -> int:
        """Effective max workers: CLI override or DEFAULT_MAX_WORKERS."""
        if self.max_workers_cli is not None:
            return self.max_workers_cli
        return DEFAULT_MAX_WORKERS

    def get_linux_hosts(self) -> List[str]:
        """
        Get Linux hosts only.

        Returns:
            List of hostnames that are not Windows hosts.
        """
        return [h for h in self.vm_hosts if h not in self.windows_hosts]

    def get_windows_hosts(self) -> List[str]:
        """
        Get Windows hosts only.

        Returns:
            List of hostnames that are Windows hosts.
        """
        return [h for h in self.vm_hosts if h in self.windows_hosts]

    def get_results_dir_name(self, timestamp: Optional[str] = None) -> str:
        """
        Generate results directory name.

        Creates a directory name with timestamp, description, and host count.
        Format: ./fio-results-{timestamp}-{description}-machines_{count}

        Args:
            timestamp: Optional timestamp string (defaults to current time).

        Returns:
            Directory name as string.
        """
        ts = timestamp or datetime.now().strftime('%Y%m%d-%H%M%S')
        desc = re.sub(r'[^a-z0-9]', '_', self.description.lower()) if self.description else ""
        desc = re.sub(r'_+', '_', desc).strip('_')
        if desc:
            return f"./fio-results-{ts}-{desc}-machines_{len(self.vm_hosts)}"
        return f"./fio-results-{ts}-machines_{len(self.vm_hosts)}"




class ConfigLoader:
    """
    Loads and validates configuration from YAML file.

    Handles parsing of YAML configuration with support for:
    - Linux and Windows VM hosts
    - Storage configuration (devices, mount points, filesystems)
    - FIO test parameters (block sizes, I/O patterns, runtime)
    - Migration settings
    - Optional timeout overrides
    """

    def __init__(self, config: FioTestConfig):
        self.config = config
    
    def load_config(self) -> None:
        """
        Load configuration from YAML file and populate FioTestConfig.

        Reads the YAML configuration file and validates required fields.
        Sets configuration values on the FioTestConfig object.

        Raises:
            FioConfigError: If required configuration fields are missing or invalid.
        """
        if not os.path.exists(self.config.config_file):
            raise FioConfigError(f"Configuration file '{self.config.config_file}' not found")
        
        with open(self.config.config_file, 'r') as f:
            yaml_data = yaml.safe_load(f)
        
        # Load namespace
        if self.config.use_virtctl is not False:
            self.config.namespace = yaml_data.get('vm', {}).get('namespace', 'default')
            if self.config.namespace == "null":
                self.config.namespace = "default"
        else:
            self.config.namespace = "N/A"
        
        # Load VM hosts (Linux hosts)
        self.config.vm_hosts = self._get_vm_hosts(yaml_data)
        
        # Load storage configuration (required for Linux hosts unless --testdir)
        storage = yaml_data.get('storage', {})
        linux_hosts_present = len(self.config.vm_hosts) > 0
        
        if linux_hosts_present:
            if self.config.use_testdir:
                if storage:
                    logger.info(
                        "TESTDIR MODE: ignoring YAML storage.devices / mount_point / filesystem "
                        "(OS-disk paths will be used)"
                    )
                for host in self.config.vm_hosts:
                    self.config.storage_devices[host] = "unused"
            else:
                if not storage:
                    raise FioConfigError("CRITICAL: 'storage' section is required when Linux hosts are configured")
                
                if 'mount_point' not in storage or not storage.get('mount_point') or storage.get('mount_point') == "null":
                    raise FioConfigError("CRITICAL: 'storage.mount_point' is required when Linux hosts are configured")
                self.config.mount_point = storage['mount_point']
                
                if 'filesystem' not in storage or not storage.get('filesystem') or storage.get('filesystem') == "null":
                    raise FioConfigError("CRITICAL: 'storage.filesystem' is required when Linux hosts are configured")
                self.config.filesystem = storage['filesystem']
                
                # Load persistent mount option (optional, defaults to False)
                persistent = storage.get('persistent', False)
                if persistent == "true" or persistent is True:
                    self.config.persistent_mount = True
                else:
                    self.config.persistent_mount = False
                
                # Load device mappings
                devices = storage.get('devices', {})
                for host in self.config.vm_hosts:
                    device = devices.get(host)
                    if not device:
                        # Try pattern matching
                        device = self._get_device_from_pattern(host, devices)
                    if device:
                        self.config.storage_devices[host] = device
                    else:
                        raise FioConfigError(f"CRITICAL: No storage device specified for Linux host '{host}'")
        
        # Load FIO configuration (required for Linux hosts, optional if only Windows)
        fio = yaml_data.get('fio', {})
        if linux_hosts_present:
            self.config.test_size = fio.get('test_size')
            self.config.test_runtime = parse_optional_runtime(fio.get('runtime'))
            raw_bs = fio.get('block_sizes', '')
            self.config.block_sizes = raw_bs if isinstance(raw_bs, list) else raw_bs.split()
            raw_ip = fio.get('io_patterns', '')
            self.config.io_patterns = raw_ip if isinstance(raw_ip, list) else raw_ip.split()
            self.config.numjobs = int(fio.get('numjobs', 1))
            self.config.iodepth = int(fio.get('iodepth', 1))
            self.config.ioengine = str(fio.get('ioengine', DEFAULT_LINUX_IOENGINE)).strip() or DEFAULT_LINUX_IOENGINE
            self.config.direct_io = str(fio.get('direct_io', 1))
            self.config.fsync = normalize_optional_fsync(fio.get('fsync'))
            self.config.rate_iops = fio.get('rate_iops')
            if self.config.rate_iops == "null" or not self.config.rate_iops:
                self.config.rate_iops = None
            else:
                # Ensure rate_iops is an integer if it's set
                if isinstance(self.config.rate_iops, str):
                    self.config.rate_iops = int(self.config.rate_iops)
            self.config.fio_installed = parse_bool(fio.get('fio_installed'), False)
        
        # Load output configuration (required for Linux hosts, optional if only Windows)
        output = yaml_data.get('output', {})
        if linux_hosts_present:
            if not output:
                raise FioConfigError("CRITICAL: 'output' section is required when Linux hosts are configured")
            
            if 'directory' not in output or not output.get('directory') or output.get('directory') == "null":
                raise FioConfigError("CRITICAL: 'output.directory' is required when Linux hosts are configured")
            self.config.output_dir = output['directory']
            
            if 'format' not in output or not output.get('format') or output.get('format') == "null":
                raise FioConfigError("CRITICAL: 'output.format' is required when Linux hosts are configured")
            self.config.output_format = output['format']
        
        self.config.description = yaml_data.get('description', '')
        if self.config.description == "null" or not self.config.description:
            self.config.description = ""
        
        # Load retry configuration (required)
        retry = yaml_data.get('retry', {})
        if not retry:
            raise FioConfigError("CRITICAL: 'retry' section is required in configuration file")
        
        if 'interval' not in retry or retry.get('interval') is None:
            raise FioConfigError("CRITICAL: 'retry.interval' is required in configuration file")
        self.config.retry_interval = int(retry['interval'])
        
        if 'max_retries' not in retry or retry.get('max_retries') is None:
            raise FioConfigError("CRITICAL: 'retry.max_retries' is required in configuration file")
        self.config.max_retries = int(retry['max_retries'])
        
        if retry.get('skip_connectivity_test'):
            self.config.skip_connectivity_test = retry['skip_connectivity_test']
        
        # Load monitoring configuration (required)
        monitoring = yaml_data.get('monitoring', {})
        if not monitoring:
            raise FioConfigError("CRITICAL: 'monitoring' section is required in configuration file")
        
        if 'task_monitor_interval' not in monitoring or monitoring.get('task_monitor_interval') is None:
            raise FioConfigError("CRITICAL: 'monitoring.task_monitor_interval' is required in configuration file")
        self.config.task_monitor_interval = int(monitoring['task_monitor_interval'])
        
        # Load migration configuration
        migrate = yaml_data.get('migrate')
        if migrate is None or migrate == "null":
            # No migration configuration or explicitly null
            self.config.migrate_workloads = []
            self.config.migrate_interval = 0
        else:
            # migrate is a dictionary
            migrate_workloads = migrate.get('workloads', '')
            if migrate_workloads and migrate_workloads != "null":
                self.config.migrate_workloads = migrate_workloads.split()
            else:
                self.config.migrate_workloads = []
            
            migrate_interval = migrate.get('interval', 0)
            if migrate_interval == "null" or not migrate_interval:
                self.config.migrate_interval = 0
            else:
                self.config.migrate_interval = int(migrate_interval)
        
        # Load Windows-specific configuration (optional)
        windows_config = yaml_data.get('windows', {})
        if windows_config:
            # Load Windows host list
            windows_hosts = windows_config.get('hosts', [])
            if isinstance(windows_hosts, str):
                windows_hosts = windows_hosts.split()
            elif not isinstance(windows_hosts, list):
                windows_hosts = []
            
            # Also check for Windows host patterns
            windows_host_pattern = windows_config.get('host_pattern')
            if windows_host_pattern:
                if '{' in windows_host_pattern and '..' in windows_host_pattern:
                    match = re.search(r'([\w-]+)\{(\d+)\.\.(\d+)\}', windows_host_pattern)
                    if match:
                        prefix = match.group(1)
                        start = int(match.group(2))
                        end = int(match.group(3))
                        pattern_hosts = [f"{prefix}{i}" for i in range(start, end + 1)]
                        windows_hosts.extend(pattern_hosts)
                        logger.info(f"Expanded Windows host pattern to {len(pattern_hosts)} hosts")
                else:
                    windows_hosts.extend(windows_host_pattern.split())
                    logger.info(f"Using Windows host pattern as literal hostname(s): {windows_host_pattern}")
            
            self.config.windows_hosts = set(windows_hosts)
            
            if self.config.windows_hosts:
                logger.info(f"Windows hosts detected: {sorted(self.config.windows_hosts)}")
                
                # Load Windows storage configuration (ignored entirely with --testdir)
                storage_win = windows_config.get('storage_win', {})
                if self.config.use_testdir:
                    if storage_win:
                        logger.info(
                            "TESTDIR MODE: ignoring YAML windows.storage_win "
                            "(devices / mount_point not used; OS-disk paths will be used)"
                        )
                    self.config.windows_storage_devices = {}
                elif storage_win:
                    devices_win = storage_win.get('devices', {})
                    for host in self.config.windows_hosts:
                        device = devices_win.get(host)
                        if not device:
                            # Try pattern matching
                            device = self._get_device_from_pattern(host, devices_win)
                        if device:
                            self.config.windows_storage_devices[host] = device
                        else:
                            raise FioConfigError(f"CRITICAL: No storage device specified for Windows host '{host}'")
                    
                    self.config.windows_mount_point = storage_win.get('mount_point')
                    if not self.config.windows_mount_point or self.config.windows_mount_point == "null":
                        raise FioConfigError("CRITICAL: 'windows.storage_win.mount_point' is required for Windows hosts")
                
                # Load Windows FIO configuration
                fio_win = windows_config.get('fio_win', {})
                if fio_win:
                    # Load run_dir (location of FIO executable) - this is the directory containing fio.exe
                    # Also support root_dir for compatibility (though run_dir takes precedence)
                    run_dir = fio_win.get('run_dir') or fio_win.get('fio_dir')
                    root_dir = fio_win.get('root_dir')
                    
                    if self.config.use_testdir:
                        if run_dir or root_dir:
                            logger.info(
                                "TESTDIR MODE: ignoring YAML windows.fio_win.run_dir "
                                f"(using {WINDOWS_FIO_DIR_TESTDIR})"
                            )
                    elif run_dir:
                        # run_dir is explicitly set - normalize and use it directly
                        self.config.windows_fio_dir = normalize_windows_path(run_dir)
                    elif root_dir:
                        # Only root_dir is set - append 'fio' to it (e.g., "d:/" -> "d:/fio")
                        # Ensure root_dir ends with / for proper path construction
                        root_dir_normalized = normalize_windows_path(root_dir)
                        if not root_dir_normalized.endswith('/'):
                            root_dir_normalized += '/'
                        self.config.windows_fio_dir = normalize_windows_path(root_dir_normalized + 'fio')
                    else:
                        # Default fallback
                        self.config.windows_fio_dir = normalize_windows_path('d:/fio')
                    
                    if not self.config.use_testdir:
                        # Ensure fio_dir ends with / for proper path construction (like Windows script FIO_DIR="d:/fio/")
                        if not self.config.windows_fio_dir.endswith('/'):
                            self.config.windows_fio_dir += '/'
                    
                    self.config.windows_test_size = fio_win.get('test_size')
                    self.config.windows_test_runtime = parse_optional_runtime(fio_win.get('runtime'))
                    raw_wbs = fio_win.get('block_sizes', '')
                    self.config.windows_block_sizes = raw_wbs if isinstance(raw_wbs, list) else raw_wbs.split()
                    raw_wip = fio_win.get('io_patterns', '')
                    self.config.windows_io_patterns = raw_wip if isinstance(raw_wip, list) else raw_wip.split()
                    self.config.windows_numjobs = int(fio_win.get('numjobs', 1))
                    self.config.windows_iodepth = int(fio_win.get('iodepth', 1))
                    self.config.windows_direct_io = str(fio_win.get('direct_io', 1))
                    self.config.windows_fsync = normalize_optional_fsync(fio_win.get('fsync'))
                    self.config.windows_rate_iops = fio_win.get('rate_iops')
                    if self.config.windows_rate_iops == "null" or not self.config.windows_rate_iops:
                        self.config.windows_rate_iops = None
                    else:
                        if isinstance(self.config.windows_rate_iops, str):
                            self.config.windows_rate_iops = int(self.config.windows_rate_iops)
                
                # Load Windows output configuration
                output_win = windows_config.get('output_win', {})
                if output_win:
                    if self.config.use_testdir:
                        if output_win.get('directory'):
                            logger.info(
                                "TESTDIR MODE: ignoring YAML windows.output_win.directory "
                                f"(using {WINDOWS_OUTPUT_TESTDIR})"
                            )
                        self.config.windows_output_format = output_win.get('format', 'json+')
                    else:
                        self.config.windows_output_dir = output_win.get('directory')
                        if not self.config.windows_output_dir or self.config.windows_output_dir == "null":
                            raise FioConfigError("CRITICAL: 'windows.output_win.directory' is required for Windows hosts")
                        self.config.windows_output_format = output_win.get('format', 'json+')
        
        # Load optional timeouts
        timeouts = yaml_data.get('timeouts', {})
        if timeouts:
            self.config.timeout_default = int(timeouts.get('default', DEFAULT_TIMEOUT))
            self.config.timeout_quick = int(timeouts.get('quick', QUICK_TIMEOUT))
            self.config.timeout_process_check = int(timeouts.get('process_check', PROCESS_CHECK_TIMEOUT))
            self.config.timeout_connectivity = int(timeouts.get('connectivity', CONNECTIVITY_TIMEOUT))
            self.config.timeout_runtime_buffer = int(timeouts.get('runtime_buffer', RUNTIME_BUFFER))
            self.config.timeout_nohup_setup = int(timeouts.get('nohup_setup', NOHUP_SETUP_TIMEOUT))
            self.config.timeout_scp = int(timeouts.get('scp', SCP_TIMEOUT))
            self.config.timeout_dataset_buffer = int(timeouts.get('dataset_buffer', DATASET_WRITE_BUFFER))
            self.config.timeout_dataset_stall = int(timeouts.get('dataset_stall', DATASET_STALL_SECONDS))
            if timeouts.get('dataset_hard') is not None:
                self.config.timeout_dataset_hard = int(timeouts.get('dataset_hard'))
            self.config.dataset_write_retries = int(timeouts.get('dataset_write_retries', DATASET_WRITE_RETRIES))
            self.config.timeout_check_interval = int(timeouts.get('check_interval', CHECK_INTERVAL))
            self.config.timeout_migration = int(timeouts.get('migration', MIGRATION_TIMEOUT))
            logger.info(f"Timeouts - default: {self.config.timeout_default}s, quick: {self.config.timeout_quick}s, "
                        f"scp: {self.config.timeout_scp}s, connectivity: {self.config.timeout_connectivity}s, "
                        f"migration: {self.config.timeout_migration}s, "
                        f"dataset_stall: {self.config.timeout_dataset_stall}s, "
                        f"dataset_write_retries: {self.config.dataset_write_retries}")

        # Merge Windows hosts into vm_hosts list (so all hosts are in one list)
        if self.config.windows_hosts:
            self.config.vm_hosts.extend(list(self.config.windows_hosts))
            logger.info(f"Total hosts (Linux + Windows): {len(self.config.vm_hosts)}")
        
        # Validate that at least some hosts are configured
        if not self.config.vm_hosts:
            raise FioConfigError("CRITICAL: No hosts configured. Please specify hosts in 'vm' section (Linux) or 'windows' section (Windows)")
    
    def _get_vm_hosts(self, yaml_data: Dict) -> List[str]:
        """
        Get VM hosts from YAML configuration using multiple methods.

        Attempts to load hosts in the following priority order:
        1. Host pattern (e.g., "vm{1..200}") - expands numeric ranges
        2. Host labels - queries cluster for VMs matching labels
        3. Host file - reads hosts from external file
        4. Simple host list - space-separated hostnames

        Args:
            yaml_data: Parsed YAML configuration dictionary.

        Returns:
            List of hostnames as strings.
        """
        vm_config = yaml_data.get('vm', {})
        
        # Method 1: Host pattern
        host_pattern = vm_config.get('host_pattern')
        if host_pattern:
            logger.info(f"Using host pattern: {host_pattern}")
            # Expand pattern like vm{1..200} or vme-{1..10}
            if '{' in host_pattern and '..' in host_pattern:
                # Match pattern with optional dashes/underscores: prefix{start..end}
                # Examples: vm{1..5}, vme-{1..10}, host_{1..100}
                match = re.search(r'([\w-]+)\{(\d+)\.\.(\d+)\}', host_pattern)
                if match:
                    prefix = match.group(1)
                    start = int(match.group(2))
                    end = int(match.group(3))
                    expanded = [f"{prefix}{i}" for i in range(start, end + 1)]
                    logger.info(f"Expanded pattern to {len(expanded)} hosts: {expanded[:5]}{'...' if len(expanded) > 5 else ''}")
                    return expanded
                else:
                    logger.warning(f"Could not parse host pattern '{host_pattern}' - using as-is")
            return [host_pattern]
        
        # Method 2: Host labels
        host_labels = vm_config.get('host_labels')
        if host_labels:
            if self.config.use_virtctl is False:
                raise FioConfigError("Label-based host selection is not supported in SSH-only mode")
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
        
        # Method 3: Host file
        host_file = vm_config.get('host_file')
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
        hosts = vm_config.get('hosts')
        if hosts:
            logger.info(f"Using simple host list: {hosts}")
            return hosts.split() if isinstance(hosts, str) else hosts
        
        # No Linux hosts found - return empty list (Windows hosts will be loaded separately)
        # This allows Windows-only configurations
        return []
    
    def _get_device_from_pattern(self, host: str, devices: Dict) -> Optional[str]:
        """
        Get storage device for a host using pattern matching.

        Expands numeric patterns like "vd{1..3}" to match hostnames.

        Args:
            host: Hostname to match.
            devices: Dictionary mapping patterns to device names.

        Returns:
            Device name if pattern matches, None otherwise.
        """
        for pattern, device in devices.items():
            if '{' in pattern and '..' in pattern:
                match = re.search(r'([\w-]+)\{(\d+)\.\.(\d+)\}', pattern)
                if match:
                    prefix = match.group(1)
                    start = int(match.group(2))
                    end = int(match.group(3))
                    for i in range(start, end + 1):
                        if f"{prefix}{i}" == host:
                            return device
        return None



