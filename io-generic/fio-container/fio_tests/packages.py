"""Package / FIO installation on remote hosts."""

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

from fio_tests.config import FioTestConfig
from fio_tests.executor import CommandExecutor
from fio_tests.util import normalize_windows_path

logger = logging.getLogger("fio_tests")

def _ensure_windows_fio_copied_to_data_disk(
    config: FioTestConfig, executor: CommandExecutor, windows_hosts: List[str]
) -> None:
    """Provision data disk if needed and copy FIO from c:\\tools\\fio to the data drive."""
    logger.info(f"Installing FIO on Windows hosts: {windows_hosts}")
    root_dir = "d:/"
    if config.windows_fio_dir:
        fio_dir_normalized = normalize_windows_path(config.windows_fio_dir)
        fio_dir_normalized = fio_dir_normalized.rstrip('/')
        if '/' in fio_dir_normalized:
            root_dir = fio_dir_normalized.rsplit('/', 1)[0] + '/'
        else:
            root_dir = fio_dir_normalized + '/'

    root_dir_ps = root_dir.replace('/', '\\').rstrip('\\')
    root_dir_ps_with_slash = root_dir_ps + '\\'

    drive_letter = root_dir_ps[0].upper() if root_dir_ps else "D"
    logger.info(f"Ensuring Windows disk is provisioned for drive {drive_letter}: before FIO installation...")

    with ThreadPoolExecutor(max_workers=min(len(windows_hosts), config.max_workers)) as pool:
        provision_futures = []
        for host in windows_hosts:
            device = config.windows_storage_devices.get(host, "1")
            cmd = (
                f"powershell -Command \""
                f"if (Test-Path '{drive_letter}:\\') {{ Write-Host 'DRIVE_EXISTS' }} "
                f"else {{ "
                f"Write-Host 'PROVISIONING'; "
                f"& c:\\tools\\setup\\provision-data-disk.ps1 -DiskID {device}; "
                f"Write-Host 'PROVISIONED' "
                f"}}\""
            )
            future = pool.submit(
                executor.execute_prep_command,
                host,
                cmd,
                f"Checking/provisioning drive {drive_letter}: on {host}",
                timeout=config.timeout_default,
            )
            provision_futures.append((future, host))

        provisioned = 0
        existed = 0
        for future, host in provision_futures:
            success, output = future.result()
            if not success:
                logger.error(f"Failed to check/provision disk on {host}: {output}")
                sys.exit(1)
            elif output and 'DRIVE_EXISTS' in output:
                existed += 1
            else:
                provisioned += 1
        if existed > 0:
            logger.info(f"Drive {drive_letter}: already existed on {existed} host(s)")
        if provisioned > 0:
            logger.info(f"Drive {drive_letter}: provisioned on {provisioned} host(s)")

    logger.info(f"Copying FIO from c:\\tools\\fio to {root_dir_ps_with_slash} on Windows hosts...")
    with ThreadPoolExecutor(max_workers=min(len(windows_hosts), config.max_workers)) as pool:
        futures = []
        for host in windows_hosts:
            cmd = (
                f"powershell -Command \"if (Test-Path 'c:\\tools\\fio') {{ "
                f"copy-item -Path c:\\tools\\fio -Destination {root_dir_ps_with_slash} "
                f"-recurse -force; Write-Host 'FIO_COPIED' }} else {{ Write-Host 'SOURCE_NOT_FOUND' }}\""
            )
            futures.append(
                pool.submit(executor.execute_prep_command, host, cmd, f"Installing FIO on {host}")
            )

        failed = 0
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                failed += 1
            elif output and 'SOURCE_NOT_FOUND' in output:
                logger.warning("Source c:\\tools\\fio not found on a host")
                failed += 1

        if failed > 0:
            logger.error(f"{failed}/{len(windows_hosts)} Windows hosts failed to install FIO")
            sys.exit(1)

    logger.info("FIO installation completed on all Windows hosts")


def ensure_packages_installed(config: FioTestConfig, executor: CommandExecutor) -> None:
    """
    Ensure FIO and required packages are installed on all hosts.

    For Linux hosts: installs fio, xfsprogs, and util-linux via dnf unless
    config.fio_installed is True (golden image — Linux check/install skipped).
    For Windows hosts: copies FIO executable from c:\tools\fio to the
    configured FIO directory on each host.

    Args:
        config: FIO test configuration object.
        executor: Command executor for remote operations.

    Raises:
        SystemExit: If installation fails on any host.
    """
    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()

    # Install FIO on Windows hosts (copy from c:\tools\fio to root_dir)
    if windows_hosts:
        if config.use_testdir:
            logger.info(
                f"TESTDIR MODE: Verifying FIO at c:\\tools\\fio on Windows hosts: {windows_hosts}"
            )
            with ThreadPoolExecutor(max_workers=min(len(windows_hosts), config.max_workers)) as pool:
                futures = []
                for host in windows_hosts:
                    cmd = (
                        "powershell -Command \""
                        "if (Test-Path 'c:\\tools\\fio\\fio.exe') { Write-Host 'FIO_OK' } "
                        "else { Write-Host 'FIO_NOT_FOUND'; exit 1 }\""
                    )
                    futures.append(
                        pool.submit(
                            executor.execute_prep_command, host, cmd, f"Verifying FIO on {host}"
                        )
                    )
                failed = 0
                for future in as_completed(futures):
                    success, output = future.result()
                    if not success or (output and 'FIO_NOT_FOUND' in output):
                        failed += 1
                        logger.error(f"FIO not found at c:\\tools\\fio\\fio.exe: {output}")
                if failed:
                    logger.error(
                        f"{failed}/{len(windows_hosts)} Windows hosts missing FIO at c:\\tools\\fio"
                    )
                    sys.exit(1)
            logger.info("FIO verified on all Windows hosts (testdir mode, no copy to data disk)")
        else:
            _ensure_windows_fio_copied_to_data_disk(config, executor, windows_hosts)

    if not linux_hosts:
        return

    if config.fio_installed:
        logger.info("Skipping FIO package check/install on Linux hosts (fio_installed=true)")
        return

    logger.info("Checking if FIO and required packages are installed on all Linux hosts...")

    # Install FIO on each Linux host when not using a golden image
    with ThreadPoolExecutor(max_workers=min(len(linux_hosts), config.max_workers)) as pool:
        futures = []
        for host in linux_hosts:
            cmd = (
                "bash -c '"
                "if command -v fio &> /dev/null; then "
                "echo \"FIO is already installed on this host\"; "
                "fio --version; "
                "else "
                "echo \"Installing FIO and dependencies...\"; "
                "dnf install -y fio xfsprogs util-linux; "
                "echo \"FIO installation completed\"; "
                "fio --version; "
                "fi"
                "'"
            )
            future = pool.submit(
                executor.execute_prep_command,
                host,
                cmd,
                "Checking and installing FIO dependencies",
            )
            futures.append(future)
        
        # Wait for all installations to complete
        failed = 0
        installed_count = 0
        already_installed_count = 0
        for future in as_completed(futures):
            success, output = future.result()
            if not success:
                logger.error(f"Failed to install FIO dependencies: {output}")
                failed += 1
            else:
                # Log output to show what happened
                if output:
                    if "already installed" in output.lower():
                        already_installed_count += 1
                        logger.debug(f"Package check output: {output.strip()}")
                    else:
                        installed_count += 1
                        logger.info(f"Package installation output: {output.strip()[:200]}")
        
        if failed > 0:
            logger.error(f"{failed}/{len(linux_hosts)} Linux hosts failed to install FIO dependencies")
            sys.exit(1)
        
        if installed_count > 0:
            logger.info(f"Installed FIO and dependencies on {installed_count} Linux host(s)")
        if already_installed_count > 0:
            logger.info(f"FIO and dependencies already installed on {already_installed_count} Linux host(s)")
    
    logger.info("FIO and dependencies are ready on all Linux hosts")


def prepare_machine(config: FioTestConfig, executor: CommandExecutor) -> None:
    """
    Prepare machines by installing FIO dependencies only.

    This is a standalone mode that only installs FIO and dependencies
    without running any tests. Useful for preparing hosts in advance.

    Args:
        config: FIO test configuration object.
        executor: Command executor for remote operations.
    """
    if config.fio_installed and not config.get_windows_hosts():
        logger.info("Skipping machine preparation (fio_installed=true, golden image)")
        return

    logger.info("Preparing machines - installing FIO dependencies only...")
    ensure_packages_installed(config, executor)
    logger.info("Machine preparation completed - FIO dependencies are ready on all hosts")



