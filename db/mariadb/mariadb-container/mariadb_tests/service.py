"""MariaDB service start/stop / cleanup."""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from mariadb_tests.config import MariaDBTestConfig
from mariadb_tests.executor import CommandExecutor

logger = logging.getLogger("mariadb_tests")

def manage_mariadb_service(config: MariaDBTestConfig, executor: CommandExecutor, 
                          host: str, action: str, description: str = "MariaDB service management") -> bool:
    """Safely manage MariaDB service"""
    if action == "restart":
        cmd = (
            "if systemctl list-unit-files | grep -q '^mariadb.*\\.service'; then "
            "if systemctl is-active --quiet mariadb; then "
            "echo 'MariaDB is running, restarting...' && "
            "systemctl restart mariadb; "
            "else "
            "echo 'MariaDB is installed but not running, starting...' && "
            "systemctl start mariadb; "
            "fi; "
            "else "
            "echo 'WARNING: MariaDB service not found, skipping restart'; "
            "exit 0; "
            "fi"
        )
    elif action == "stop":
        cmd = (
            "if systemctl list-unit-files | grep -q '^mariadb.*\\.service'; then "
            "if systemctl is-active --quiet mariadb; then "
            "echo 'MariaDB is running, stopping...' && "
            "systemctl stop mariadb; "
            "else "
            "echo 'MariaDB is not running'; "
            "fi; "
            "else "
            "echo 'WARNING: MariaDB service not found, nothing to stop'; "
            "fi"
        )
    else:
        logger.error(f"Invalid action '{action}' for MariaDB service management")
        return False
    
    success, output = executor.execute_command(host, cmd, description)
    if not success:
        logger.error(f"Failed to {description} on {host}: {output}")
    return success

def stop_mariadb(config: MariaDBTestConfig, executor: CommandExecutor) -> None:
    """Stop MariaDB instances"""
    logger.info("Stopping MariaDB instances on all hosts...")
    
    # Step 1: Stop MariaDB services
    logger.info("Step 1/3: Stopping MariaDB services on all hosts...")
    failed_hosts = []
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            future = pool.submit(manage_mariadb_service, config, executor, host, "stop", "Stopping MariaDB service")
            futures.append((future, host))
        for future, host in futures:
            if not future.result():
                failed_hosts.append(host)
    if failed_hosts:
        logger.warning(f"Failed to stop MariaDB on {len(failed_hosts)} host(s): {', '.join(failed_hosts)}")
    
    # Step 2: Cleanup storage
    if config.mount_point != "none" and config.mount_point != "null":
        logger.info("Step 2/3: Cleaning up storage mount points on all hosts...")
        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
            futures = []
            for host in config.db_hosts:
                cmd = (
                    f"if mountpoint -q '{config.mount_point}' 2>/dev/null; then "
                    f"echo 'Unmounting {config.mount_point}' && "
                    f"umount '{config.mount_point}' && "
                    f"echo 'Successfully unmounted {config.mount_point}'; "
                    f"else "
                    f"echo 'Mount point {config.mount_point} is not mounted or does not exist'; "
                    f"fi && "
                    f"cd '{config.hammerdb_dir}' && rm -f mariadb-results.tar.gz 2>/dev/null || true"
                )
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up storage and temporary files")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    elif config.disk_list != "none" and config.disk_list != "null":
        logger.info("Step 2/3: Cleaning up disk device mount points on all hosts...")
        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
            futures = []
            for host in config.db_hosts:
                cmd = (
                    "if mountpoint -q '/perf1' 2>/dev/null; then "
                    "echo 'Unmounting /perf1 (disk device mount point)' && "
                    "umount '/perf1' && "
                    "echo 'Successfully unmounted /perf1'; "
                    "else "
                    "echo 'Mount point /perf1 is not mounted or does not exist'; "
                    "fi && "
                    f"cd '{config.hammerdb_dir}' && rm -f mariadb-results.tar.gz 2>/dev/null || true"
                )
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up disk device mount point and temporary files")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    else:
        logger.info("Step 2/3: No storage configuration detected - only cleaning up temporary files")
        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
            futures = []
            for host in config.db_hosts:
                cmd = f"cd '{config.hammerdb_dir}' && rm -f mariadb-results.tar.gz 2>/dev/null || true"
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up temporary files")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    
    logger.info("Cleanup completed")
