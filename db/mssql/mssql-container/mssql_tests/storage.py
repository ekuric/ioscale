"""Install / prepare MSSQL Server on Linux guests."""
from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from mssql_tests.config import MSSQLTestConfig
from mssql_tests.executor import CommandExecutor
from mssql_tests.util import get_vm_number

logger = logging.getLogger("mssql_tests")


def _disk_device_path(disk_list: str) -> str:
    """HammerDB install script needs a real block path (test -b), e.g. /dev/vdb."""
    disk = (disk_list or "").strip()
    if not disk or disk in ("none", "null", "auto"):
        return disk
    if disk.startswith("/dev/"):
        return disk
    return f"/dev/{disk}"


def install_mssql(config: MSSQLTestConfig, executor: CommandExecutor) -> None:
    """Install MSSQL Server on VMs"""
    logger.info("Installing MSSQL Server on VMs...")

    hammerdb_path_escaped = config.hammerdb_path.replace("'", "'\"'\"'")
    if config.mount_point == "none":
        disk_path = _disk_device_path(config.disk_list)
        disk_escaped = disk_path.replace("'", "'\"'\"'")
        logger.info(f"MSSQL install will use disk device: {disk_path}")
        install_arg = f"-d \"{disk_escaped}\""
    else:
        mount_point_escaped = config.mount_point.replace("'", "'\"'\"'")
        logger.info(f"MSSQL install will use mount point: {config.mount_point}")
        install_arg = f"-m \"{mount_point_escaped}\""

    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            logger.info(f"Installing MSSQL Server on {host}...")
            # Skip full reinstall when mssql-server is already up (CREATE_VMS re-runs).
            # Otherwise pass /dev/... so the install script's test -b succeeds.
            cmd = (
                f"bash -c '"
                f"if systemctl is-active --quiet mssql-server && rpm -q mssql-server &>/dev/null; then "
                f"echo \"MSSQL Server already installed and running\"; "
                f"exit 0; "
                f"fi; "
                f"cd \"{hammerdb_path_escaped}/templates/mssql\" && "
                f"./Hammerdb-mssql-install-script {install_arg} 2>&1"
                f"'"
            )
            future = pool.submit(
                executor.execute_command,
                host,
                cmd,
                f"Installing MSSQL Server on {host}",
                timeout=1800,
            )
            futures.append(future)

        failed = 0
        already = 0
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                failed += 1
                logger.error("MSSQL Server installation failed. Error details:")
                logger.error(f"{output}")
            elif output and "already installed and running" in output.lower():
                already += 1
                logger.info(output.strip())

        if failed > 0:
            logger.error(f"{failed}/{len(config.db_hosts)} hosts failed to install MSSQL Server")
            logger.error("Please check the error messages above for details.")
            logger.error("Common issues:")
            logger.error("  1. Verify the Hammerdb-mssql-install-script exists and is executable")
            logger.error("  2. Check that required packages are installed")
            logger.error("  3. Verify disk device or mount point is accessible (/dev/...)")
            logger.error("  4. Check MSSQL Server installation prerequisites")
            logger.error("  5. Try running with --verbose flag for more detailed output")
            sys.exit(1)
        if already > 0:
            logger.info(f"MSSQL Server already installed and running on {already} host(s)")
    
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



