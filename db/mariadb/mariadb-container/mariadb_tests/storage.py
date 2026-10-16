"""MariaDB install (disk mount / fstab / HammerDB install script)."""
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from mariadb_tests.config import MariaDBTestConfig
from mariadb_tests.executor import CommandExecutor

logger = logging.getLogger("mariadb_tests")


def _disk_device_path(disk_list: str) -> str:
    """HammerDB install script requires a real block path (test -b), e.g. /dev/vdb."""
    disk = (disk_list or "").strip()
    if not disk or disk in ("none", "null"):
        return disk
    if disk.startswith("/dev/"):
        return disk
    return f"/dev/{disk}"


def install_mariadb(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Install MariaDB on VMs"""
    logger.info("Installing MariaDB on VMs...")

    if config.mount_point != "none":
        install_disk_arg = f"-m {config.mount_point}"
    else:
        disk_path = _disk_device_path(config.disk_list)
        logger.info(f"MariaDB install will use disk device: {disk_path}")
        install_disk_arg = f"-d {disk_path}"
    
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            cmd = (
                f"if systemctl is-active --quiet mariadb && rpm -q mariadb-server &>/dev/null; then "
                f"echo 'MariaDB already installed and running'; "
                f"exit 0; "
                f"elif rpm -q mariadb-server &>/dev/null; then "
                f"echo 'MariaDB already installed but not running, will reinstall'; "
                f"cd '{config.hammerdb_path}/templates/mariadb'; "
                f"./Hammerdb-mariadb-install-script {install_disk_arg} || exit 1; "
                f"else "
                f"echo 'Installing MariaDB from scratch'; "
                f"cd '{config.hammerdb_path}/templates/mariadb'; "
                f"./Hammerdb-mariadb-install-script {install_disk_arg} || exit 1; "
                f"fi"
            )
            future = pool.submit(executor.execute_command, host, cmd, "Installing MariaDB")
            futures.append(future)
        
        already_installed = 0
        installed_from_scratch = 0
        failed = 0
        reinstall_count = 0
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to install MariaDB: {output}")
                failed += 1
            elif output and "already installed and running" in output.lower():
                already_installed += 1
            elif output and "from scratch" in output.lower():
                installed_from_scratch += 1
            elif output and "will reinstall" in output.lower():
                reinstall_count += 1
        
        if failed > 0:
            logger.error(f"{failed}/{len(config.db_hosts)} hosts failed to install MariaDB")
            sys.exit(1)
        
        if already_installed > 0:
            logger.info(f"MariaDB already installed and running on {already_installed} host(s)")
        if installed_from_scratch > 0:
            logger.info(f"MariaDB installed from scratch on {installed_from_scratch} host(s)")
        if reinstall_count > 0:
            logger.info(f"MariaDB reinstalled (was installed but not running) on {reinstall_count} host(s)")
    
    # Create /etc/fstab entries if persistent mount is enabled
    if config.persistent_mount:
        logger.info("Creating /etc/fstab entries for persistent mounts...")
        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
            futures = []
            for host in config.db_hosts:
                # Determine device and mount point
                if config.mount_point != "none":
                    mount_point = config.mount_point
                    # Need to find the device that's mounted at this point
                    # The install script mounts it, so we can query it
                    cmd = (
                        f"if mountpoint -q '{mount_point}' 2>/dev/null; then "
                        f"device=$(findmnt -n -o SOURCE --target '{mount_point}' 2>/dev/null || echo ''); "
                        f"if [ -n \"$device\" ]; then "
                        f"if ! grep -q \"$device {mount_point}\" /etc/fstab; then "
                        f"filesystem=$(findmnt -n -o FSTYPE --target '{mount_point}' 2>/dev/null || echo 'xfs'); "
                        f"echo \"$device {mount_point} $filesystem defaults 0 0\" >> /etc/fstab && "
                        f"echo \"Added fstab entry for $device -> {mount_point}\" || "
                        f"echo \"Failed to add fstab entry\"; "
                        f"else "
                        f"echo \"fstab entry already exists for $device -> {mount_point}\"; "
                        f"fi; "
                        f"else "
                        f"echo \"Could not determine device for {mount_point}\"; "
                        f"fi; "
                        f"else "
                        f"echo \"Mount point {mount_point} is not mounted\"; "
                        f"fi"
                    )
                else:
                    # Using disk device - mount point should be /perf1
                    device_path = _disk_device_path(config.disk_list)
                    mount_point = "/perf1"
                    filesystem = "xfs"  # Default filesystem from install script
                    cmd = (
                        f"if ! grep -q '{device_path} {mount_point}' /etc/fstab; then "
                        f"echo '{device_path} {mount_point} {filesystem} defaults 0 0' >> /etc/fstab && "
                        f"echo 'Added fstab entry for {device_path} -> {mount_point}' || "
                        f"echo 'Failed to add fstab entry'; "
                        f"else "
                        f"echo 'fstab entry already exists for {device_path} -> {mount_point}'; "
                        f"fi"
                    )
                future = pool.submit(executor.execute_command, host, cmd, f"Creating fstab entry for {host}")
                futures.append(future)
            for future in as_completed(futures):
                success, output = future.result()
                if success:
                    logger.info(f"fstab entry: {output.strip()}")
                else:
                    logger.warning(f"Failed to create fstab entry: {output}")
    else:
        logger.info("Skipping /etc/fstab entries (persistent mount not enabled)")

