"""HammerDB TPCC performance test runs."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from datetime import datetime
from typing import List, Optional

from mariadb_tests.config import MariaDBTestConfig
from mariadb_tests.executor import CommandExecutor
from mariadb_tests.migration import VMMigrationMonitor, migrate_vms_during_test
from mariadb_tests.util import _extract_mariadb_tpm

logger = logging.getLogger("mariadb_tests")


def _test_output_file(run_date: str, num_hosts: int, counter: int, user_count, run_id: int) -> str:
    """Result filename including sweep run id (matches mssql-container)."""
    return f"test_mariadb_{run_date}_{num_hosts}pod_pod{counter}_{user_count}_run{run_id}.out"


def run_tests(config: MariaDBTestConfig, executor: CommandExecutor, migration_monitor: Optional['VMMigrationMonitor'] = None) -> None:
    """Run performance tests (optional multi-sweep over user_count, like mssql-container)."""
    logger.info("Running performance tests...")
    
    num_hosts = len(config.db_hosts)
    run_date = datetime.now().strftime("%Y.%m.%d")
    user_counts = list(config.user_count)
    sweep_iterations = max(1, int(config.test_iterations or 1))

    for run_id in range(1, sweep_iterations + 1):
        if sweep_iterations > 1:
            logger.info(
                f"=== Sweep iteration {run_id}/{sweep_iterations} "
                f"(results -> *_run{run_id}.out) ==="
            )

        for vu_idx, user_count in enumerate(user_counts):
            logger.info(
                f"Starting test run with {user_count} users "
                f"(sweep run {run_id}/{sweep_iterations}) on all hosts..."
            )

            if migration_monitor:
                migration_monitor.current_operation = f"tpcc {user_count} users run{run_id}"

            # Step 1: Setup test scripts
            logger.info(f"Preparing test scripts for {user_count} users...")
            with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
                futures = []
                counter = 1
                for host in config.db_hosts:
                    cmd = (
                        f"cd '{config.hammerdb_dir}' && "
                        f"cp '{config.hammerdb_path}/templates/mariadb/mariadbsetup/runtest_mariadb.tcl' runtest{counter}_mariadb.tcl 2>/dev/null || "
                        f"cp runtest_mariadb.tcl runtest{counter}_mariadb.tcl && "
                        f"sed -i 's/^diset tpcc mysql_count_ware.*/diset tpcc mysql_count_ware {config.warehouse_count}/g' runtest{counter}_mariadb.tcl && "
                        f"sed -i 's/^vuset.*/vuset vu {user_count}/g' runtest{counter}_mariadb.tcl && "
                        f"sed -i 's/^diset tpcc mysql_duration.*/diset tpcc mysql_duration {config.test_duration}/g' runtest{counter}_mariadb.tcl" +
                        (f" && sed -i 's/^diset tpcc mysql_rampup.*/diset tpcc mysql_rampup {config.rampup_time}/g' runtest{counter}_mariadb.tcl" if config.rampup_time else "")
                    )
                    future = pool.submit(executor.execute_command, host, cmd, f"Preparing test script (runtest{counter}_mariadb.tcl) for {user_count} users")
                    futures.append((future, counter))
                    counter += 1
                for future, counter in futures:
                    future.result()

            # Step 2: Run performance tests
            logger.info(
                f"Executing performance tests with {user_count} users "
                f"(sweep run {run_id}/{sweep_iterations})..."
            )
            # Calculate test duration and migration timing upfront
            test_duration_seconds = int(config.test_duration) * 60 if config.test_duration else 900
            rampup_time_seconds = int(config.rampup_time) * 60 if config.rampup_time else 120

            # Start tests - use short timeout just to verify command starts (nohup should return quickly)
            test_start_time = time.time()
            logger.info(f"Starting performance tests at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
                futures = []
                counter = 1
                for host in config.db_hosts:
                    output_file = _test_output_file(run_date, num_hosts, counter, user_count, run_id)
                    # Use nohup with & to truly background the process and return immediately
                    # The command should return quickly, but we use a longer timeout to account for slow SSH connections
                    cmd = f"cd '{config.hammerdb_dir}' && (nohup ./hammerdbcli auto runtest{counter}_mariadb.tcl > '{output_file}' 2>&1 </dev/null &) && echo 'started'"
                    future = pool.submit(executor.execute_command, host, cmd, f"Starting performance test (output: {output_file})", timeout=90, max_retries=1)
                    futures.append((future, counter, host, output_file))
                    counter += 1

                # Wait for all tests to start (should be quick with nohup)
                # Collect results and verify tests actually started
                hosts_to_verify = []
                for future, counter, host, output_file in futures:
                    try:
                        success, output = future.result()  # Wait for the future (already has timeout)
                        if not success:
                            # Command may have timed out, but test might still be running
                            logger.warning(f"Command to start test on {host} reported failure, verifying test status...")
                            hosts_to_verify.append((counter, host, output_file))
                        else:
                            # Command succeeded, but verify test actually started
                            hosts_to_verify.append((counter, host, output_file))
                    except Exception as e:
                        # Future exception occurred (shouldn't happen, but handle it)
                        logger.warning(f"Exception getting result for {host}: {e}, verifying test status...")
                        hosts_to_verify.append((counter, host, output_file))

                # Verify tests actually started by checking processes and output files
                logger.info("Verifying that performance tests actually started on all hosts...")
                time.sleep(2)  # Give processes a moment to start
                failed_starts = []
                with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(hosts_to_verify))) as verify_pool:
                    verify_futures = []
                    for counter, host, output_file in hosts_to_verify:
                        # Check both process and output file
                        verify_cmd = (
                            f"cd '{config.hammerdb_dir}' && "
                            f"process_count=$(ps aux | grep -E 'hammerdbcli.*runtest{counter}_mariadb' | grep -v grep | wc -l) && "
                            f"file_exists=$(test -f '{output_file}' && echo 'yes' || echo 'no') && "
                            f"file_size=$(test -f '{output_file}' && stat -c%s '{output_file}' 2>/dev/null || echo '0') && "
                            f"echo \"PROCESS:$process_count FILE:$file_exists SIZE:$file_size\""
                        )
                        verify_future = verify_pool.submit(
                            executor.execute_command, host, verify_cmd,
                            f"Verifying test started on {host}", timeout=30
                        )
                        verify_futures.append((verify_future, counter, host, output_file))

                    for verify_future, counter, host, output_file in verify_futures:
                        verify_success, verify_output = verify_future.result()
                        if verify_success and verify_output:
                            # Parse verification output
                            process_count = 0
                            file_exists = False
                            file_size = 0
                            for part in verify_output.strip().split():
                                if part.startswith("PROCESS:"):
                                    try:
                                        process_count = int(part.split(":")[1])
                                    except (ValueError, IndexError):
                                        pass
                                elif part.startswith("FILE:"):
                                    file_exists = part.split(":")[1] == "yes"
                                elif part.startswith("SIZE:"):
                                    try:
                                        file_size = int(part.split(":")[1])
                                    except (ValueError, IndexError):
                                        pass

                            if process_count > 0:
                                logger.info(f"✓ Test confirmed running on {host} (process found, output file: {output_file})")
                            elif file_exists and file_size > 0:
                                logger.info(f"✓ Test appears to have started on {host} (output file exists with size {file_size} bytes)")
                            else:
                                logger.error(f"✗ Test verification failed on {host}: process not found, file missing or empty")
                                failed_starts.append(host)
                        else:
                            logger.warning(f"Could not verify test status on {host}, assuming it started: {verify_output}")

                if failed_starts:
                    logger.error(f"Failed to start or verify tests on {len(failed_starts)} host(s): {', '.join(failed_starts)}")
                    logger.error("Please check the hosts manually to confirm test status")
                    # Don't exit - let the monitoring phase catch if tests aren't running

            # Check if migration is needed for this user_count - do this immediately after starting tests
            if user_count in config.migrate_user_counts:
                # HammerDB has a 2-minute rampup time before actual test starts
                # Migration should occur at: rampup_time + (test_duration / 2)
                migration_time = rampup_time_seconds + (test_duration_seconds // 2)
                logger.info(f"Migration configured for user_count '{user_count}' - will migrate VMs at {migration_time}s after test start")
                logger.info(f"  (rampup: {rampup_time_seconds}s + test_duration/2: {test_duration_seconds//2}s = {migration_time}s)")
                migration_timestamp = datetime.fromtimestamp(datetime.now().timestamp() + migration_time)
                logger.info(f"Test started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, migration will occur at {migration_timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info(f"Waiting {migration_time}s before triggering VM migrations...")
                time.sleep(migration_time)

                logger.info(f"Triggering VM migrations at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (midpoint of actual test runtime after rampup)...")
                migration_success, migration_failed_vms = migrate_vms_during_test(config, executor, user_count)
                if not migration_success:
                    logger.error(f"VM migrations FAILED for user_count '{user_count}': {', '.join(migration_failed_vms)}")
                    if migration_monitor:
                        op = f"tpcc {user_count} users run{run_id}"
                        for vm_name in migration_failed_vms:
                            migration_monitor.log_event(f"VM migration FAILED: {vm_name}", vm=vm_name, operation=op, event_type="migration_failure")

                # Verify HammerDB processes are still running after migration
                logger.info("Verifying HammerDB processes are still running after migration...")
                for host in config.db_hosts:
                    cmd = "ps aux | grep -E 'hammerdbcli.*runtest' | grep -v grep | wc -l"
                    success, output = executor.execute_command(host, cmd, "Checking HammerDB process status", timeout=30)
                    if success:
                        process_count = int(output.strip()) if output.strip().isdigit() else 0
                        if process_count > 0:
                            logger.info(f"✓ HammerDB process confirmed running on {host} after migration")
                        else:
                            logger.warning(f"⚠ HammerDB process not found on {host} after migration - test may have completed or failed")

            # Wait for tests to complete
            logger.info(
                f"Waiting for performance tests with {user_count} users "
                f"(sweep run {run_id}/{sweep_iterations}) to complete..."
            )
            with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
                start_time = time.time()
                check_interval = 30  # Check every 30 seconds
                max_wait_time = rampup_time_seconds + test_duration_seconds + 600  # rampup + test + 10 min buffer

                while time.time() - start_time < max_wait_time:
                    # Submit fresh futures at the start of each iteration (avoids stale pre-loop checks)
                    futures = []
                    counter = 1
                    for host in config.db_hosts:
                        check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{counter}_mariadb' | grep -v grep | wc -l"
                        future = pool.submit(executor.execute_command, host, check_cmd, f"Checking test status on {host}", timeout=30)
                        futures.append((
                            future, counter, host,
                            _test_output_file(run_date, num_hosts, counter, user_count, run_id),
                        ))
                        counter += 1

                    all_done = True
                    running_count = 0

                    for future, counter, host, output_file in futures:
                        try:
                            success, output = future.result(timeout=1)
                            if success:
                                process_count = int(output.strip()) if output.strip().isdigit() else 0
                                if process_count > 0:
                                    all_done = False
                                    running_count += 1
                        except Exception:
                            # Re-check this host
                            check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{counter}_mariadb' | grep -v grep | wc -l"
                            success, output = executor.execute_command(host, check_cmd, f"Rechecking test status on {host}", timeout=30)
                            if success:
                                process_count = int(output.strip()) if output.strip().isdigit() else 0
                                if process_count > 0:
                                    all_done = False
                                    running_count += 1

                    if all_done:
                        logger.info("All performance tests completed")
                        break

                    elapsed = int(time.time() - start_time)
                    logger.info(f"Waiting for tests to complete... ({running_count} hosts still running, {elapsed}s elapsed)")
                    time.sleep(check_interval)

            # Verify tests ran for the full duration
            expected_minutes = int(config.test_duration) if config.test_duration else 15
            counter = 1
            for host in config.db_hosts:
                output_file = _test_output_file(run_date, num_hosts, counter, user_count, run_id)
                if not config.dry_run:
                    verify_cmd = (
                        f"cd '{config.hammerdb_dir}' && "
                        f"if grep -q 'TEST RESULT' '{output_file}' 2>/dev/null; then "
                        f"echo 'COMPLETE'; "
                        f"elif grep -q 'Timing test period' '{output_file}' 2>/dev/null; then "
                        f"last_min=$(grep -oP '\\d+(?= \\.\\.\\.,)' '{output_file}' | tail -1); "
                        f"echo \"INCOMPLETE:$last_min\"; "
                        f"else echo 'NO_OUTPUT'; fi"
                    )
                    success, output = executor.execute_command(host, verify_cmd, f"Verifying test completion on {host}", timeout=30)
                    if success:
                        result = output.strip()
                        if result == "COMPLETE":
                            logger.info(f"{host}: Test completed successfully (full {expected_minutes} min duration)")
                        elif result.startswith("INCOMPLETE:"):
                            last_min = result.split(":")[1] if ":" in result else "?"
                            logger.warning(f"{host}: Test ended prematurely at minute {last_min}/{expected_minutes} - results may be partial")
                        elif result == "NO_OUTPUT":
                            logger.warning(f"{host}: No test output found - test may not have started")
                counter += 1

            # Step 3: Collect results
            logger.info(
                f"Collecting test results for {user_count} users "
                f"(sweep run {run_id}/{sweep_iterations}):"
            )
            counter = 1
            for host in config.db_hosts:
                output_file = _test_output_file(run_date, num_hosts, counter, user_count, run_id)
                if not config.dry_run:
                    cmd = f"cd '{config.hammerdb_dir}'; grep 'TEST RESULT' '{output_file}' | tail -1"
                    success, result = executor.execute_command(host, cmd, "Collecting TPM results", timeout=30)
                    if success:
                        tpm = _extract_mariadb_tpm(result)
                        if tpm is not None:
                            logger.info(f"Host {host}: {tpm} TPM")
                        else:
                            logger.warning(f"Host {host}: Could not parse TPM from result line: {result.strip()!r}")
                    else:
                        logger.warning(f"Host {host}: Error collecting results")
                else:
                    logger.info(f"DRY-RUN: Would collect results from {host}")
                counter += 1

            logger.info(
                f"Completed test run with {user_count} users "
                f"(sweep run {run_id}/{sweep_iterations}) on all hosts"
            )

            vu_sleep = int(config.vu_sleep_sec or 0)
            if vu_sleep > 0 and vu_idx < len(user_counts) - 1:
                next_vu = user_counts[vu_idx + 1]
                logger.info(
                    f"VU {user_count} complete (iteration {run_id}/{sweep_iterations}); "
                    f"sleeping {vu_sleep}s before VU {next_vu}..."
                )
                if not config.dry_run:
                    time.sleep(vu_sleep)

        sleep_sec = int(config.iteration_sleep_sec or 0)
        if sleep_sec > 0 and run_id < sweep_iterations:
            logger.info(
                f"Iteration {run_id}/{sweep_iterations} complete; "
                f"sleeping {sleep_sec}s before next iteration..."
            )
            if not config.dry_run:
                time.sleep(sleep_sec)

    logger.info(
        f"Completed {sweep_iterations} sweep iteration(s) over user counts: "
        f"{' '.join(user_counts)}"
    )
