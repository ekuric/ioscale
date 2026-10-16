"""PostgreSQL service management and shutdown."""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from postgresql_tests.config import PostgreSQLTestConfig
from postgresql_tests.executor import CommandExecutor

logger = logging.getLogger("postgresql_tests")

def manage_postgresql_service(config: PostgreSQLTestConfig, executor: CommandExecutor, 
                             host: str, action: str, description: str = "PostgreSQL service management") -> None:
    """Safely manage PostgreSQL service"""
    if action == "restart":
        cmd = (
            "if systemctl list-unit-files | grep -q '^postgresql.*\\.service'; then "
            "if systemctl is-active --quiet postgresql; then "
            "echo 'PostgreSQL is running, restarting...' && "
            "systemctl restart postgresql; "
            "else "
            "echo 'PostgreSQL is installed but not running, starting...' && "
            "systemctl start postgresql; "
            "fi; "
            "else "
            "echo 'WARNING: PostgreSQL service not found, skipping restart'; "
            "exit 0; "
            "fi"
        )
    elif action == "stop":
        cmd = (
            "if systemctl list-unit-files | grep -q '^postgresql.*\\.service'; then "
            "if systemctl is-active --quiet postgresql; then "
            "echo 'PostgreSQL is running, stopping...' && "
            "systemctl stop postgresql; "
            "else "
            "echo 'PostgreSQL is not running'; "
            "fi; "
            "else "
            "echo 'WARNING: PostgreSQL service not found, nothing to stop'; "
            "fi"
        )
    else:
        logger.error(f"Invalid action '{action}' for PostgreSQL service management")
        return
    
    executor.execute_command(host, cmd, description)

def stop_postgresql(config: PostgreSQLTestConfig, executor: CommandExecutor) -> None:
    """Stop PostgreSQL instances"""
    logger.info("Stopping PostgreSQL instances on all hosts...")
    
    # Step 1: Stop PostgreSQL services
    logger.info("Step 1/3: Stopping PostgreSQL services on all hosts...")
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            future = pool.submit(manage_postgresql_service, config, executor, host, "stop", "Stopping PostgreSQL service")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
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
                    f"cd {config.hammerdb_dir} && rm -f postgresql-results.tar.gz 2>/dev/null || true"
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
                    f"cd {config.hammerdb_dir} && rm -f postgresql-results.tar.gz 2>/dev/null || true"
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
                cmd = f"cd {config.hammerdb_dir} && rm -f postgresql-results.tar.gz 2>/dev/null || true"
                future = pool.submit(executor.execute_command, host, cmd, "Cleaning up temporary files")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
    
    logger.info("Cleanup completed")
