"""PostgreSQL installation via HammerDB install script."""
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from postgresql_tests.config import PostgreSQLTestConfig
from postgresql_tests.executor import CommandExecutor

logger = logging.getLogger("postgresql_tests")


def _disk_device_path(disk_list: str) -> str:
    """Install script expects a real block path (test -b), e.g. /dev/vdb."""
    disk = (disk_list or "").strip()
    if not disk or disk in ("none", "null"):
        return disk
    if disk.startswith("/dev/"):
        return disk
    return f"/dev/{disk}"


def install_postgresql(config: PostgreSQLTestConfig, executor: CommandExecutor) -> None:
    """Install PostgreSQL on VMs"""
    logger.info("Installing PostgreSQL on VMs...")
    
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            if config.mount_point == "none":
                disk_path = _disk_device_path(config.disk_list)
                logger.info(f"PostgreSQL install will use disk device: {disk_path}")
                cmd = f"cd '{config.hammerdb_path}/templates/postgresql'; ./Hammerdb-postgres-install-script -d '{disk_path}'"
            else:
                cmd = f"cd '{config.hammerdb_path}/templates/postgresql'; ./Hammerdb-postgres-install-script -m '{config.mount_point}'"
            future = pool.submit(executor.execute_command, host, cmd, "Installing PostgreSQL")
            futures.append(future)
        
        failed = 0
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to install PostgreSQL: {output}")
                failed += 1
        
        if failed > 0:
            logger.error(f"{failed}/{len(config.db_hosts)} hosts failed to install PostgreSQL")
            sys.exit(1)
    
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
