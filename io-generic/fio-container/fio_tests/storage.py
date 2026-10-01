"""Storage prepare (format/mount/provision) and cleanup."""

import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from fio_tests.config import FioTestConfig
from fio_tests.constants import LINUX_TESTDIR
from fio_tests.executor import CommandExecutor
from fio_tests.util import normalize_windows_path

logger = logging.getLogger("fio_tests")

def prepare_storage(config: FioTestConfig, executor: CommandExecutor) -> None:
    """
    Prepare storage on all VMs.

    Performs the following steps in sequence:
    1. Validate test devices exist on all hosts
    2. Unmount existing mounts on Linux hosts
    3. Partition and format disks on Windows hosts
    4. Create test directories on all hosts
    5. Format devices with filesystem on Linux hosts
    6. Mount devices on Linux hosts
    7. Optionally create /etc/fstab entries for persistent mounts

    Args:
        config: FIO test configuration object.
        executor: Command executor for remote operations.

    Raises:
        SystemExit: If any storage preparation step fails.
    """
    logger.info("Preparing storage on VMs with parallel execution...")

    # Separate Linux and Windows hosts
    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()

    if config.use_testdir:
        logger.info("TESTDIR MODE: Creating test directories only (no format/mount/provision)...")
        with ThreadPoolExecutor(max_workers=min(len(config.vm_hosts), config.max_workers)) as pool:
            futures = []
            for host in linux_hosts:
                cmd = f"mkdir -p {config.output_dir} {config.mount_point}"
                future = pool.submit(executor.execute_prep_command, host, cmd, "Creating test directories")
                futures.append(future)
            for host in windows_hosts:
                mount_point_win = normalize_windows_path(config.windows_mount_point)
                output_dir_win = normalize_windows_path(config.windows_output_dir)
                cmd = (
                    f"powershell -Command \"New-Item -ItemType Directory -Force "
                    f"-Path '{mount_point_win}', '{output_dir_win}'\""
                )
                future = pool.submit(executor.execute_prep_command, host, cmd, "Creating test directories")
                futures.append(future)
            for future in as_completed(futures):
                success, output = future.result()
                if not success:
                    logger.error(f"Failed to create directories: {output}")
                    sys.exit(1)
        logger.info("Storage preparation completed on all hosts (testdir mode)!")
        return

    # Step 1: Validate devices (Linux only - Windows uses PowerShell script)
    logger.info("Step 1/7: Validating test devices on all hosts...")
    with ThreadPoolExecutor(max_workers=min(len(config.vm_hosts), config.max_workers)) as pool:
        futures = []
        for host in linux_hosts:
            device = config.storage_devices[host]
            cmd = f"test -b /dev/{device} && echo 'Found block device /dev/{device}' && lsblk /dev/{device} || (echo 'ERROR: Block device /dev/{device} not found' && exit 1)"
            future = pool.submit(executor.execute_prep_command, host, cmd, "Validating test device")
            futures.append(future)
        for host in windows_hosts:
            # Windows: Use PowerShell to validate disk (provision script will handle this)
            device = config.windows_storage_devices.get(host, "1")
            # Wrap in powershell -Command to ensure it runs in PowerShell, not cmd.exe
            # Use single quotes to avoid shell interpretation of pipes
            cmd = f"powershell -Command \"Get-Disk -Number {device} | Select-Object -Property Number,Size,PartitionStyle\""
            future = pool.submit(executor.execute_prep_command, host, cmd, "Validating Windows disk")
            futures.append(future)
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Device validation failed: {output}")
                sys.exit(1)
    
    # Step 2: Unmount existing mounts (Linux only - Windows doesn't need this)
    logger.info("Step 2/7: Unmounting existing mounts on Linux hosts...")
    if linux_hosts:
        with ThreadPoolExecutor(max_workers=min(len(linux_hosts), config.max_workers)) as pool:
            futures = []
            for host in linux_hosts:
                cmd = f"mountpoint -q {config.mount_point} && (echo 'Unmounting {config.mount_point}' && umount {config.mount_point} || true) || echo 'Mount point {config.mount_point} is not mounted'"
                future = pool.submit(executor.execute_prep_command, host, cmd, "Unmounting existing mount")
                futures.append(future)
            for future in as_completed(futures):
                future.result()  # Don't fail on unmount errors
    
    # Step 3: Windows storage preparation (MUST be done before creating directories)
    # This partitions and formats the disk, creating the drive (e.g., d:)
    if windows_hosts:
        logger.info("Step 3/7 (Windows): Preparing storage on Windows hosts using provision-data-disk.ps1...")
        logger.info(
            f"NOTE: This will partition and format the disk on "
            f"{', '.join(windows_hosts)}, creating the drive (e.g., d:)"
        )
        with ThreadPoolExecutor(max_workers=min(len(windows_hosts), config.max_workers)) as pool:
            futures = {}
            for host in windows_hosts:
                device = config.windows_storage_devices.get(host, "1")
                logger.info(f"{host}: Partitioning and formatting Disk {device}...")
                # Match bash script format: powershell c:\tools\setup\provision-data-disk.ps1 -DiskID {device}
                cmd = f"powershell c:\\tools\\setup\\provision-data-disk.ps1 -DiskID {device}"
                future = pool.submit(
                    executor.execute_prep_command,
                    host,
                    cmd,
                    f"Preparing Windows storage on {host}",
                )
                futures[future] = host
            for future in as_completed(futures):
                host = futures[future]
                success, output = future.result()
                if not success:
                    logger.error(f"{host}: Windows storage preparation failed: {output}")
                    sys.exit(1)
                logger.info(f"{host}: Disk partition/format completed")
    
    # Step 4: Create directories (Linux and Windows separately)
    # For Windows: This must be done AFTER disk provisioning (Step 3) so the drive exists
    logger.info("Step 4/7: Creating test directories on all hosts...")
    with ThreadPoolExecutor(max_workers=min(len(config.vm_hosts), config.max_workers)) as pool:
        futures = []
        for host in linux_hosts:
            cmd = f"mkdir -p {config.output_dir} {config.mount_point}"
            future = pool.submit(executor.execute_prep_command, host, cmd, "Creating test directories")
            futures.append(future)
        for host in windows_hosts:
            # Windows: Use PowerShell to create directories
            # This is done AFTER disk provisioning so the drive (d:) exists
            mount_point_win = normalize_windows_path(config.windows_mount_point)
            output_dir_win = normalize_windows_path(config.windows_output_dir)
            # Use -Command to ensure it runs in PowerShell, not cmd.exe
            cmd = f"powershell -Command \"New-Item -ItemType Directory -Force -Path '{mount_point_win}', '{output_dir_win}'\""
            future = pool.submit(executor.execute_prep_command, host, cmd, "Creating test directories")
            futures.append(future)
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to create directories: {output}")
    
    # Step 5: Format devices (Linux only - Windows handled by provision script)
    if linux_hosts:
        logger.info("Step 5/7: Formatting devices on Linux hosts (WARNING: destructive operation)...")

        def _is_mounted_filesystem_error(output: str) -> bool:
            text = (output or "").lower()
            return (
                "mounted filesystem" in text
                or "apparently in use" in text
                or "is mounted" in text
            )

        def _mkfs_once(host: str, description: str, *, max_retries: int = 1) -> Tuple[bool, str]:
            device = config.storage_devices[host]
            device_path = f"/dev/{device}"
            fmt_cmd = (
                f"echo 'WARNING: Formatting {device_path} with {config.filesystem}' && "
                f"mkfs.{config.filesystem} -f {device_path}"
            )
            # quiet=True: mounted-filesystem failures are expected sometimes and
            # handled by reboot+retry below — avoid ERROR spam before recovery.
            return executor.execute_command(
                host, fmt_cmd, description,
                max_retries=max_retries, retry_interval=10, timeout=60,
                quiet=True,
                restart_vm_on_unreachable=True,
            )

        def _format_pass(hosts: List[str], description: str, *, max_retries: int = 1
                         ) -> Dict[str, Tuple[bool, str]]:
            """Run mkfs on all given hosts in parallel (unbounded)."""
            results: Dict[str, Tuple[bool, str]] = {}
            lock = threading.Lock()

            def _run(host: str) -> None:
                device = config.storage_devices[host]
                logger.info(f"{host}: {description} of /dev/{device} with {config.filesystem}")
                success, output = _mkfs_once(host, description, max_retries=max_retries)
                with lock:
                    results[host] = (success, output)

            threads = []
            for host in hosts:
                t = threading.Thread(target=_run, args=(host,), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            return results

        # Pass 1: format ALL hosts at once
        logger.info(
            f"Format pass 1: {len(linux_hosts)} Linux hosts in parallel "
            f"(unbounded — not capped by max_workers={config.max_workers})"
        )
        pass1 = _format_pass(linux_hosts, "Formatting test device", max_retries=1)

        ok_hosts = []
        reboot_hosts = []
        hard_fail_hosts = []
        for host in linux_hosts:
            success, output = pass1.get(host, (False, "no result"))
            if success:
                ok_hosts.append(host)
                logger.info(f"{host}: Format completed on /dev/{config.storage_devices[host]}")
            elif _is_mounted_filesystem_error(output):
                reboot_hosts.append(host)
                logger.warning(
                    f"{host}: /dev/{config.storage_devices[host]} contains a mounted filesystem — "
                    f"will restart VM and retry format"
                )
            else:
                hard_fail_hosts.append(host)
                logger.error(f"{host}: Formatting failed on /dev/{config.storage_devices[host]}: {output}")

        if hard_fail_hosts:
            logger.error(
                f"Format failed (non-mount issues) on {len(hard_fail_hosts)} host(s): "
                f"{', '.join(hard_fail_hosts)}"
            )
            sys.exit(1)

        # Pass 2: reboot stuck hosts, wait, then format only those hosts again
        if reboot_hosts:
            logger.warning(
                f"Format pass 2: restarting {len(reboot_hosts)} VM(s) to clear stuck mounts, "
                f"then retrying format (script waits here — will not proceed to mount yet): "
                f"{', '.join(reboot_hosts)}"
            )

            def _reboot_host(host: str) -> Tuple[str, bool]:
                # remount=False — we are about to format, not use the old FS
                return host, executor.restart_vm(
                    host,
                    remount=False,
                    reason="Stuck mount during format — clearing via reboot",
                )

            reboot_results: Dict[str, bool] = {}
            reboot_lock = threading.Lock()

            def _reboot_and_store(host: str) -> None:
                h, ok = _reboot_host(host)
                with reboot_lock:
                    reboot_results[h] = ok

            reboot_threads = []
            for host in reboot_hosts:
                t = threading.Thread(target=_reboot_and_store, args=(host,), daemon=True)
                t.start()
                reboot_threads.append(t)
            for t in reboot_threads:
                t.join()

            reboot_failed = [h for h in reboot_hosts if not reboot_results.get(h)]
            if reboot_failed:
                logger.error(
                    f"VM restart failed on {len(reboot_failed)} host(s): {', '.join(reboot_failed)}"
                )
                sys.exit(1)

            logger.info(
                f"All {len(reboot_hosts)} VM(s) restarted — retrying format on those hosts only..."
            )
            pass2 = _format_pass(
                reboot_hosts, "Formatting test device (after VM restart)", max_retries=5
            )

            still_failed = []
            for host in reboot_hosts:
                success, output = pass2.get(host, (False, "no result"))
                if success:
                    ok_hosts.append(host)
                    logger.info(
                        f"{host}: Format completed after VM restart on /dev/{config.storage_devices[host]}"
                    )
                else:
                    still_failed.append(host)
                    logger.error(
                        f"{host}: Formatting still failed after VM restart on "
                        f"/dev/{config.storage_devices[host]}: {output}"
                    )

            if still_failed:
                logger.error(
                    f"Format failed after reboot on {len(still_failed)} host(s): "
                    f"{', '.join(still_failed)}"
                )
                sys.exit(1)

        logger.info(f"Format completed on all {len(ok_hosts)} Linux host(s)")
    
    # Step 6: Mount devices (Linux only - Windows handled by provision script in Step 3)
    if linux_hosts:
        logger.info("Step 6/7: Mounting devices on Linux hosts...")
        with ThreadPoolExecutor(max_workers=min(len(linux_hosts), config.max_workers)) as pool:
            futures = []
            for host in linux_hosts:
                device = config.storage_devices[host]
                cmd = f"mount /dev/{device} {config.mount_point}"
                future = pool.submit(executor.execute_prep_command, host, cmd, "Mounting test device")
                futures.append(future)
            for future in as_completed(futures):
                success, output = future.result()
                if not success:
                    logger.error(f"Mounting failed: {output}")
                    sys.exit(1)
    
    # Step 7: Create /etc/fstab entries if persistent mount is enabled (Linux only)
    if config.persistent_mount and linux_hosts:
        logger.info("Step 7/7: Creating /etc/fstab entries for persistent mounts on Linux hosts...")
        with ThreadPoolExecutor(max_workers=min(len(linux_hosts), config.max_workers)) as pool:
            futures = []
            for host in linux_hosts:
                device = config.storage_devices[host]
                device_path = f"/dev/{device}"
                mount_point = config.mount_point
                filesystem = config.filesystem
                
                # Create command to add fstab entry if it doesn't exist
                # Check if entry already exists, and add it if not
                cmd = (
                    f"if ! grep -q '{device_path} {mount_point}' /etc/fstab; then "
                    f"echo '{device_path} {mount_point} {filesystem} defaults 0 0' >> /etc/fstab && "
                    f"echo 'Added fstab entry for {device_path} -> {mount_point}' || "
                    f"echo 'Failed to add fstab entry'; "
                    f"else "
                    f"echo 'fstab entry already exists for {device_path} -> {mount_point}'; "
                    f"fi"
                )
                future = pool.submit(executor.execute_prep_command, host, cmd, f"Creating fstab entry for {host}")
                futures.append(future)
            for future in as_completed(futures):
                success, output = future.result()
                if success:
                    logger.info(f"fstab entry: {output.strip()}")
                else:
                    logger.warning(f"Failed to create fstab entry: {output}")
    elif config.persistent_mount:
        logger.info("Skipping /etc/fstab entries (Windows hosts don't use /etc/fstab)")
    else:
        logger.info("Skipping /etc/fstab entries (persistent mount not enabled)")
    
    logger.info("Storage preparation completed on all hosts!")




def cleanup_storage(config: FioTestConfig, executor: CommandExecutor) -> None:
    """
    Clean up storage on VMs after test completion.

    Performs cleanup operations:
    1. Unmounts mount points on Linux hosts
    2. Removes test result files from all hosts

    Args:
        config: FIO test configuration object.
        executor: Command executor for remote operations.
    """
    logger.info("Cleaning up storage on VMs...")
    
    # Separate Linux and Windows hosts
    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()

    if config.use_testdir:
        logger.info("TESTDIR MODE: Cleaning test artifacts (no umount of data volumes)...")
        with ThreadPoolExecutor(max_workers=min(len(config.vm_hosts), config.max_workers)) as pool:
            futures = []
            for host in linux_hosts:
                cmd = (
                    f"rm -rf {config.output_dir}/*.json {config.mount_point}/* "
                    f"2>/dev/null || true && echo 'Test results cleanup completed'"
                )
                futures.append(
                    pool.submit(executor.execute_command, host, cmd, "Cleaning up test results")
                )
            for host in windows_hosts:
                output_dir_win = normalize_windows_path(config.windows_output_dir)
                mount_point_win = normalize_windows_path(config.windows_mount_point)
                cmd = (
                    f"powershell -Command \"Remove-Item -Path '{output_dir_win}/*' "
                    f"-Recurse -Force -ErrorAction SilentlyContinue; "
                    f"Remove-Item -Path '{mount_point_win}/*' -Recurse -Force "
                    f"-ErrorAction SilentlyContinue; Write-Host 'Test results cleanup completed'\""
                )
                futures.append(
                    pool.submit(executor.execute_command, host, cmd, "Cleaning up test results")
                )
            for future in as_completed(futures):
                future.result()
        logger.info("Storage cleanup completed")
        return

    # Unmount mount points (Linux only - Windows doesn't need unmounting)
    if linux_hosts:
        logger.info("Step 1/3: Cleaning up storage mount points on Linux hosts...")
        with ThreadPoolExecutor(max_workers=min(len(linux_hosts), config.max_workers)) as pool:
            futures = []
            for host in linux_hosts:
                cmd = f"mountpoint -q {config.mount_point} && (umount {config.mount_point} && echo 'Successfully unmounted {config.mount_point}') || echo 'Mount point {config.mount_point} is not mounted'"
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up storage mount points")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    
    # Clean up test results (both Linux and Windows)
    logger.info("Step 2/3: Cleaning up test results on all hosts...")
    with ThreadPoolExecutor(max_workers=min(len(config.vm_hosts), config.max_workers)) as pool:
        futures = []
        for host in linux_hosts:
            cmd = f"rm -rf {config.output_dir}/*.json 2>/dev/null || true && echo 'Test results cleanup completed'"
            future = pool.submit(executor.execute_command, host, cmd, "Cleaning up test results")
            futures.append(future)
        for host in windows_hosts:
            # Windows: Use PowerShell to remove files
            output_dir_win = normalize_windows_path(config.windows_output_dir)
            mount_point_win = normalize_windows_path(config.windows_mount_point)
            # Use -Command to ensure it runs in PowerShell, not cmd.exe
            cmd = f"powershell -Command \"Remove-Item -Path '{output_dir_win}/*' -Recurse -Force -ErrorAction SilentlyContinue; Remove-Item -Path '{mount_point_win}/*' -Recurse -Force -ErrorAction SilentlyContinue; Write-Host 'Test results cleanup completed'\""
            future = pool.submit(executor.execute_command, host, cmd, "Cleaning up test results")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
    logger.info("Storage cleanup completed")


