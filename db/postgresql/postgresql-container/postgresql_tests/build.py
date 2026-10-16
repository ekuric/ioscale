"""TPCC database build monitoring."""
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from postgresql_tests.config import PostgreSQLTestConfig
from postgresql_tests.executor import CommandExecutor
from postgresql_tests.service import manage_postgresql_service
from postgresql_tests.util import get_vm_number

logger = logging.getLogger("postgresql_tests")

def _build_process_check_cmd(build_script: str) -> str:
    """Return ps pipeline that matches only the given HammerDB build script."""
    return f"ps aux | grep -F '{build_script}' | grep -v grep | wc -l"


def _build_output_verify_cmd(hammerdb_dir: str, output_file: str) -> str:
    """Remote command to inspect HammerDB build log for completion/errors."""
    return (
        f"cd {hammerdb_dir} && "
        f"if [ -f {output_file} ] && [ -s {output_file} ]; then "
        f"  grep -q 'ALL VIRTUAL USERS COMPLETE' {output_file} && echo 'BUILD_DONE' || echo 'BUILD_NOT_DONE'; "
        f"  tail -50 {output_file} | grep -iE '(error|failed|fatal)' | tail -5 || echo 'NO_ERRORS'; "
        f"else "
        f"  echo 'FILE_MISSING_OR_EMPTY'; "
        f"fi"
    )


def _parse_build_verify_output(verify_output: str) -> Tuple[bool, bool, bool]:
    """Return (build_done, has_errors, file_missing)."""
    if not verify_output:
        return False, False, False
    file_missing = 'FILE_MISSING_OR_EMPTY' in verify_output
    build_done = 'BUILD_DONE' in verify_output
    has_errors = 'NO_ERRORS' not in verify_output and not file_missing
    return build_done, has_errors, file_missing


def _build_log_indicates_complete(verify_output: str) -> bool:
    """True only when the full HammerDB build finished (monitor Vuser 1 done)."""
    return 'BUILD_DONE' in verify_output


def _get_build_output_size(
    executor: 'CommandExecutor',
    config: PostgreSQLTestConfig,
    host: str,
    output_file: str,
    *,
    quiet: bool = True,
    max_retries: Optional[int] = None,
) -> Optional[int]:
    """Return build log size in bytes, or None if the check failed."""
    size_cmd = f"stat -c '%s' {config.hammerdb_dir}/{output_file} 2>/dev/null || echo -1"
    success, output = executor.execute_command(
        host,
        size_cmd,
        f"Checking build output size on {host}",
        timeout=config.build_check_timeout,
        max_retries=max_retries if max_retries is not None else config.build_check_max_retries,
        quiet=quiet,
    )
    if not success or not output.strip().lstrip('-').isdigit():
        return None
    size = int(output.strip())
    return size if size >= 0 else None


def _poll_build_host_status(
    executor: 'CommandExecutor',
    config: PostgreSQLTestConfig,
    host: str,
    vm_number: int,
    output_file: str,
    last_output_sizes: Dict[str, int],
    *,
    quiet: bool = True,
    max_retries: Optional[int] = None,
) -> str:
    """
    Poll one host during build monitoring.

    Returns:
        'complete'       - build finished successfully
        'running'        - build still in progress
        'failed'         - build log shows errors and no progress
        'check_failed'   - virtctl/SSH check failed (treat as still running)
    """
    poll_retries = max_retries if max_retries is not None else config.build_check_max_retries
    build_script = f"build{vm_number}_pg.tcl"

    verify_success, verify_output = executor.execute_command(
        host,
        _build_output_verify_cmd(config.hammerdb_dir, output_file),
        f"Verifying build completion on {host}",
        timeout=config.build_check_timeout,
        max_retries=poll_retries,
        quiet=quiet,
    )
    if verify_success and _build_log_indicates_complete(verify_output):
        return 'complete'
    elif not verify_success and not quiet:
        logger.debug(f"Build log verify failed on {host}")

    check_cmd = _build_process_check_cmd(build_script)
    success, output = executor.execute_command(
        host,
        check_cmd,
        f"Checking build status on {host}",
        timeout=config.build_check_timeout,
        max_retries=poll_retries,
        quiet=quiet,
    )
    if not success:
        return 'check_failed'

    process_count = int(output.strip()) if output.strip().isdigit() else 0
    if process_count > 0:
        current_size = _get_build_output_size(
            executor, config, host, output_file, quiet=quiet, max_retries=poll_retries
        )
        previous_size = last_output_sizes.get(host)
        if current_size is not None:
            if previous_size is None or current_size > previous_size:
                last_output_sizes[host] = current_size
                return 'running'
            last_output_sizes[host] = current_size
        return 'running'

    if not verify_success:
        return 'check_failed'

    build_done, has_errors, file_missing = _parse_build_verify_output(verify_output)
    if build_done:
        return 'complete'
    if file_missing:
        return 'running'

    current_size = _get_build_output_size(
        executor, config, host, output_file, quiet=quiet, max_retries=poll_retries
    )
    previous_size = last_output_sizes.get(host)
    if current_size is not None:
        if previous_size is None or current_size > previous_size:
            last_output_sizes[host] = current_size
            return 'running'
        last_output_sizes[host] = current_size

    if has_errors:
        return 'failed'

    # No completion marker yet, but no errors either — keep waiting.
    return 'running'


def _finalize_build_hosts(
    executor: 'CommandExecutor',
    config: PostgreSQLTestConfig,
    host_build_info: Dict[str, Tuple[int, str]],
    hosts: List[str],
) -> List[str]:
    """Run a final strict verification pass; return hosts that failed."""
    failed_builds = []
    last_output_sizes: Dict[str, int] = {}

    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(hosts))) as pool:
        futures = {
            pool.submit(
                _poll_build_host_status,
                executor,
                config,
                host,
                host_build_info[host][0],
                host_build_info[host][1],
                last_output_sizes,
                quiet=False,
                max_retries=config.max_retries,
            ): host
            for host in hosts
        }
        for future in as_completed(futures):
            host = futures[future]
            vm_number, output_file = host_build_info[host]
            try:
                status = future.result()
            except Exception as e:
                logger.error(f"Final build verification error on {host}: {e}")
                failed_builds.append(host)
                continue

            if status == 'complete':
                logger.info(f"Database build completed for all virtual users on {host}")
            elif status == 'running':
                logger.error(
                    f"Database build on {host} still running after {config.build_time_limit}s "
                    f"- check {output_file}"
                )
                failed_builds.append(host)
            elif status == 'check_failed':
                logger.error(
                    f"Could not verify database build on {host} after {config.build_time_limit}s "
                    f"- check connectivity and {output_file}"
                )
                failed_builds.append(host)
            else:
                logger.error(
                    f"Database build on {host} may have failed - check {output_file}"
                )
                failed_builds.append(host)

    return failed_builds


def build_database(config: PostgreSQLTestConfig, executor: CommandExecutor) -> None:
    """Build TPCC database"""
    logger.info("Building TPCC database with parallel execution...")
    
    # Step 1: Restart PostgreSQL services
    logger.info("Step 1/5: Restarting PostgreSQL services on all hosts...")
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            future = pool.submit(manage_postgresql_service, config, executor, host, "restart", "Restarting PostgreSQL service")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
    # Step 2: Wait for services to be ready
    logger.info("Step 2/5: Waiting for PostgreSQL services to be ready...")
    time.sleep(15)
    
    # Step 3: Clean existing databases
    logger.info("Step 3/5: Cleaning existing databases on all hosts...")
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            cmd = (
                "echo 'DROP DATABASE IF EXISTS tpcc;' > /tmp/cleanup.sql && "
                "echo 'DROP ROLE IF EXISTS tpcc;' >> /tmp/cleanup.sql && "
                "/usr/bin/psql -U postgres -d postgres -h 127.0.0.1 -f /tmp/cleanup.sql"
            )
            future = pool.submit(executor.execute_command, host, cmd, "Cleaning existing database")
            futures.append(future)
        for future in as_completed(futures):
            future.result()
    
    # Step 4: Copy and configure build scripts
    logger.info("Step 4/5: Preparing build scripts on all hosts...")
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            vm_number = get_vm_number(host)
            cmd = (
                f"cd {config.hammerdb_dir} && "
                f"cp '{config.hammerdb_path}/templates/postgresql/postgresqlsetup/build_pg.tcl' build{vm_number}_pg.tcl && "
                f"sed -i 's/^diset connection pg_host.*/diset connection pg_host 127.0.0.1/g' build{vm_number}_pg.tcl && "
                f"sed -i 's/^diset tpcc pg_count_ware.*/diset tpcc pg_count_ware {config.warehouse_count}/g' build{vm_number}_pg.tcl"
            )
            future = pool.submit(executor.execute_command, host, cmd, f"Preparing build script (build{vm_number}_pg.tcl)")
            futures.append((future, vm_number))
        for future, vm_number in futures:
            success, output = future.result()
            if not success:
                logger.error(f"Failed to prepare build script: {output}")
    
    # Step 5: Build databases
    logger.info("Step 5/5: Building TPCC databases on all hosts (this may take a while)...")
    
    # Start build processes - use nohup with immediate verification
    host_build_info = {}  # Map host -> (vm_number, output_file)
    with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
        futures = []
        for host in config.db_hosts:
            vm_number = get_vm_number(host)
            output_file = f"build_pg{vm_number}.out"
            # Start the build process and immediately verify it's running
            # This approach ensures we can verify the process even if SSH times out
            cmd = f"cd {config.hammerdb_dir} && (nohup ./hammerdbcli auto build{vm_number}_pg.tcl > {output_file} 2>&1 </dev/null &) && echo 'started'"
            future = pool.submit(executor.execute_command, host, cmd, f"Starting database build (output: {output_file})", timeout=90, max_retries=1)
            futures.append((future, vm_number, host, output_file))
            host_build_info[host] = (vm_number, output_file)
        
        # Wait for all builds to start and verify they're running
        failed_starts = []
        for future, vm_number, host, output_file in futures:
            success, output = future.result()
            if not success:
                # Even if command timed out, verify the process might still be running
                logger.warning(f"Command to start database build on {host} may have timed out, verifying process...")
                verify_cmd = _build_process_check_cmd(f"build{vm_number}_pg.tcl")
                verify_success, verify_output = executor.execute_command(host, verify_cmd, f"Verifying build process on {host}", timeout=30)
                if verify_success:
                    process_count = int(verify_output.strip()) if verify_output.strip().isdigit() else 0
                    if process_count > 0:
                        logger.info(f"✓ Database build process is running on {host} (verified despite timeout)")
                    else:
                        logger.error(f"Database build process not found on {host} - build did not start")
                        failed_starts.append(host)
                else:
                    logger.error(f"Could not verify database build process on {host}: {verify_output}")
                    failed_starts.append(host)
            else:
                if output and 'started' in output.strip().lower():
                    logger.info(f"✓ Database build started on {host}")
                else:
                    verify_cmd = _build_process_check_cmd(f"build{vm_number}_pg.tcl")
                    verify_success, verify_output = executor.execute_command(host, verify_cmd, f"Verifying build on {host}", timeout=30)
                    verify_output_stripped = verify_output.strip() if verify_output else ""
                    process_count = int(verify_output_stripped) if verify_success and verify_output_stripped.isdigit() else 0
                    if process_count > 0:
                        logger.info(f"✓ Database build started on {host} (verified)")
                    else:
                        logger.error(f"Database build process not found on {host} - build may not have started")
                        failed_starts.append(host)
        
        if failed_starts:
            logger.error(f"Failed to start database builds on {len(failed_starts)} host(s): {', '.join(failed_starts)}")
            sys.exit(1)
    
    logger.info("All database build processes started. Monitoring build progress...")

    if config.build_time_limit <= config.build_poll_interval:
        logger.error("monitoring.build_time_limit must be greater than build_poll_interval")
        sys.exit(1)

    start_time = time.time()
    warned_long_build = False
    completed_hosts = set()
    last_output_sizes: Dict[str, int] = {}

    while True:
        if time.time() - start_time >= config.build_time_limit:
            remaining = [h for h in config.db_hosts if h not in completed_hosts]
            logger.warning(
                f"Database build reached time limit ({config.build_time_limit}s). "
                f"Running final verification on {len(remaining)} host(s): {', '.join(remaining)}"
            )
            failed_builds = _finalize_build_hosts(
                executor, config, host_build_info, remaining
            )
            if failed_builds:
                logger.error(
                    f"Database build failed or timed out on {len(failed_builds)} host(s): "
                    f"{', '.join(failed_builds)}"
                )
                logger.error("Cannot proceed with tests - database must be built successfully first")
                sys.exit(1)
            if remaining:
                logger.info("All remaining database builds verified successfully after time limit")
            break

        hosts_to_check = [h for h in config.db_hosts if h not in completed_hosts]
        if not hosts_to_check:
            logger.info("All database builds completed!")
            break

        still_building = []
        check_failed_hosts = []

        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(hosts_to_check))) as pool:
            futures = {
                pool.submit(
                    _poll_build_host_status,
                    executor,
                    config,
                    host,
                    host_build_info[host][0],
                    host_build_info[host][1],
                    last_output_sizes,
                ): host
                for host in hosts_to_check
            }
            for future in as_completed(futures):
                host = futures[future]
                try:
                    status = future.result()
                except Exception as e:
                    logger.warning(f"Error checking build status on {host}: {e}")
                    still_building.append(host)
                    continue

                if status == 'complete':
                    logger.info(f"Database build completed for all virtual users on {host}")
                    completed_hosts.add(host)
                elif status == 'check_failed':
                    still_building.append(host)
                    check_failed_hosts.append(host)
                elif status == 'failed':
                    output_file = host_build_info[host][1]
                    logger.warning(
                        f"Build on {host} may have errors in {output_file} - will re-check before failing"
                    )
                    still_building.append(host)
                else:
                    still_building.append(host)

        if check_failed_hosts:
            logger.warning(
                "Could not reach %d host(s) for build status check (virtctl/SSH timeout); "
                "assuming still building: %s",
                len(check_failed_hosts),
                ', '.join(check_failed_hosts),
            )

        elapsed = int(time.time() - start_time)
        if elapsed > config.build_time_limit // 4 and not warned_long_build:
            logger.warning(
                f"Database builds are taking longer than {config.build_time_limit // 4}s; "
                f"hard timeout at {config.build_time_limit}s"
            )
            warned_long_build = True
        logger.info(
            f"Waiting for database builds to complete... "
            f"({len(still_building)} hosts still building: {', '.join(still_building)}, {elapsed}s elapsed)"
        )
        time.sleep(config.build_poll_interval)

    logger.info("Database building completed successfully on all hosts!")
