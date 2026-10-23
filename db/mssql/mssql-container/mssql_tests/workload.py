"""HammerDB TPCC workload execution on MSSQL Linux guests."""
from __future__ import annotations

import logging
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from mssql_tests.config import MSSQLTestConfig
from mssql_tests.executor import CommandExecutor
from mssql_tests.migration import VMMigrationMonitor
from mssql_tests.util import _extract_mssql_tpm, get_vm_number

logger = logging.getLogger("mssql_tests")


def _test_output_file(
    run_date: str, num_hosts: int, vm_number: str, user_count: str, run_id: int
) -> str:
    """HammerDB stdout path; includes _runN so multi-sweep results do not collide."""
    return (
        f"test_mssql_{run_date}_{num_hosts}pod_pod{vm_number}_"
        f"{user_count}_run{run_id}.out"
    )

def migrate_vms_during_test(config: MSSQLTestConfig, executor: CommandExecutor, user_count: str) -> bool:
    """Migrate VMs during MSSQL Server test"""
    if not config.migrate_user_counts or user_count not in config.migrate_user_counts:
        return True
    
    if config.use_virtctl is False:
        logger.warning(f"Migration requested for user_count '{user_count}' but SSH-only mode is enabled")
        return True
    
    if not config.namespace or config.namespace == "N/A":
        logger.warning(f"Migration requested for user_count '{user_count}' but namespace is not set")
        return True
    
    # Get VMs to migrate
    vms_to_migrate = [h for h in config.db_hosts if executor.is_vm_host(h)]
    
    if not vms_to_migrate:
        logger.info(f"No VMs found to migrate for user_count '{user_count}'")
        return True
    
    if config.migrate_interval > 0:
        logger.info(f"Starting VM migrations for user_count '{user_count}' ({len(vms_to_migrate)} VMs, sequential with {config.migrate_interval}s interval)...")
        failed_vms = []
        
        # First attempt: migrate all VMs
        for vm in vms_to_migrate:
            logger.info(f"Migrating VM: {vm}")
            try:
                result = subprocess.run(
                    ["virtctl", "-n", config.namespace, "migrate", vm],
                    capture_output=True,
                    timeout=600
                )
                if result.returncode == 0:
                    logger.info(f"✓ Successfully migrated VM: {vm}")
                else:
                    logger.error(f"✗ Failed to migrate VM: {vm}")
                    if result.stderr:
                        logger.error(f"  Error: {result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr}")
                    failed_vms.append(vm)
                
                if vm != vms_to_migrate[-1]:
                    time.sleep(config.migrate_interval)
            except Exception as e:
                logger.error(f"✗ Failed to migrate VM: {vm} - {e}")
                failed_vms.append(vm)
        
        # Retry failed migrations
        if failed_vms:
            logger.info(f"Retrying {len(failed_vms)} failed VM migrations: {', '.join(failed_vms)}")
            retry_failed = []
            for vm in failed_vms:
                logger.info(f"Retrying migration for VM: {vm}")
                try:
                    result = subprocess.run(
                        ["virtctl", "-n", config.namespace, "migrate", vm],
                        capture_output=True,
                        timeout=600
                    )
                    if result.returncode == 0:
                        logger.info(f"✓ Successfully migrated VM: {vm} (retry)")
                    else:
                        logger.error(f"✗ Failed to migrate VM: {vm} (retry)")
                        if result.stderr:
                            logger.error(f"  Error: {result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr}")
                        retry_failed.append(vm)
                    
                    if vm != failed_vms[-1]:
                        time.sleep(config.migrate_interval)
                except Exception as e:
                    logger.error(f"✗ Failed to migrate VM: {vm} (retry) - {e}")
                    retry_failed.append(vm)
            
            if retry_failed:
                logger.error(f"{len(retry_failed)}/{len(vms_to_migrate)} VM migrations failed after retry: {', '.join(retry_failed)}")
                return False
            else:
                logger.info(f"All failed migrations succeeded on retry")
                logger.info(f"All VM migrations completed successfully for user_count '{user_count}' (after retry)")
                return True
        
        logger.info(f"All VM migrations completed successfully for user_count '{user_count}'")
        return True
    else:
        logger.info(f"Starting VM migrations for user_count '{user_count}' ({len(vms_to_migrate)} VMs, parallel)...")
        
        def migrate_vm(vm_name):
            """Migrate a single VM and return (success, vm_name)"""
            logger.info(f"Migrating VM: {vm_name}")
            try:
                result = subprocess.run(
                    ["virtctl", "-n", config.namespace, "migrate", vm_name],
                    capture_output=True,
                    timeout=600
                )
                if result.returncode == 0:
                    logger.info(f"✓ Successfully migrated VM: {vm_name}")
                    return True, vm_name
                else:
                    logger.error(f"✗ Failed to migrate VM: {vm_name}")
                    if result.stderr:
                        logger.error(f"  Error: {result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr}")
                    return False, vm_name
            except Exception as e:
                logger.error(f"✗ Failed to migrate VM: {vm_name} - {e}")
                return False, vm_name
        
        # First attempt: migrate all VMs in parallel
        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(vms_to_migrate))) as pool:
            futures = [pool.submit(migrate_vm, vm) for vm in vms_to_migrate]
            failed_vms = []
            for future in as_completed(futures):
                success, vm_name = future.result()
                if not success:
                    failed_vms.append(vm_name)
        
        # Retry failed migrations
        if failed_vms:
            logger.info(f"Retrying {len(failed_vms)} failed VM migrations in parallel: {', '.join(failed_vms)}")
            with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(failed_vms))) as pool:
                futures = [pool.submit(migrate_vm, vm) for vm in failed_vms]
                retry_failed = []
                for future in as_completed(futures):
                    success, vm_name = future.result()
                    if not success:
                        retry_failed.append(vm_name)
                    else:
                        logger.info(f"✓ Successfully migrated VM: {vm_name} (retry)")
                
                if retry_failed:
                    logger.error(f"{len(retry_failed)}/{len(vms_to_migrate)} VM migrations failed after retry: {', '.join(retry_failed)}")
                    return False
                else:
                    logger.info(f"All failed migrations succeeded on retry")
                    logger.info(f"All VM migrations completed successfully for user_count '{user_count}' (after retry)")
                    return True
        
        logger.info(f"All VM migrations completed successfully for user_count '{user_count}'")
        return True


def run_tests(config: MSSQLTestConfig, executor: CommandExecutor, migration_monitor: Optional['VMMigrationMonitor'] = None) -> None:
    """Run performance tests (optional multi-sweep over user_count, like mssqlwin)."""
    logger.info("Running performance tests...")
    password = shlex.quote(config.mssql_pass)

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
                migration_monitor.current_operation = (
                    f"tpcc {user_count} users run{run_id}"
                )

            # Step 1: Setup test scripts
            logger.info(f"Preparing test scripts for {user_count} users...")
            with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
                futures = []
                for host in config.db_hosts:
                    vm_number = get_vm_number(host)
                    cmd = (
                        f"cd {config.hammerdb_dir} && "
                        f"cp '{config.hammerdb_path}/templates/mssql/mssqlsetup/runtest_mssql.tcl' runtest{vm_number}_mssql.tcl 2>/dev/null || "
                        f"cp runtest_mssql.tcl runtest{vm_number}_mssql.tcl 2>/dev/null && "
                        f"sed -i 's/^diset tpcc mssqls_count_ware.*/diset tpcc mssqls_count_ware {config.warehouse_count}/g' runtest{vm_number}_mssql.tcl && "
                        f"sed -i 's/^diset tpcc mssqls_duration.*/diset tpcc mssqls_duration {config.test_duration}/g' runtest{vm_number}_mssql.tcl && "
                        f"sed -i 's/^diset tpcc mssqls_num_vu.*/diset tpcc mssqls_num_vu {user_count}/g' runtest{vm_number}_mssql.tcl" +
                        (f" && sed -i 's/^diset tpcc mssqls_rampup.*/diset tpcc mssqls_rampup {config.rampup_time}/g' runtest{vm_number}_mssql.tcl" if config.rampup_time else "")
                    )
                    future = pool.submit(executor.execute_command, host, cmd, f"Preparing test script (runtest{vm_number}_mssql.tcl) for {user_count} users")
                    futures.append(future)
                for future in as_completed(futures):
                    future.result()

            # Step 2: Run performance tests
            logger.info(f"Executing performance tests with {user_count} users (run {run_id})...")
            test_duration_seconds = int(config.test_duration) * 60 if config.test_duration else 900
            rampup_time_seconds = int(config.rampup_time) * 60 if config.rampup_time else 120

            logger.info(f"Starting performance tests at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
                futures = []
                for host in config.db_hosts:
                    vm_number = get_vm_number(host)
                    output_file = _test_output_file(
                        run_date, num_hosts, vm_number, user_count, run_id
                    )
                    cmd = (
                        f"cd {config.hammerdb_dir} && "
                        f"export MSSQL_PASS={password} && "
                        f"(nohup ./hammerdbcli auto runtest{vm_number}_mssql.tcl > '{output_file}' 2>&1 </dev/null &) && echo 'started'"
                    )
                    future = pool.submit(executor.execute_command, host, cmd, f"Starting performance test (output: {output_file})", timeout=90, max_retries=1)
                    futures.append((future, vm_number, host, output_file))

                hosts_to_verify = []
                for future, vm_number, host, output_file in futures:
                    try:
                        success, output = future.result()
                        hosts_to_verify.append((vm_number, host, output_file))
                        if not success:
                            logger.warning(f"Command to start test on {host} reported failure, verifying test status...")
                    except Exception as e:
                        logger.warning(f"Exception getting result for {host}: {e}, verifying test status...")
                        hosts_to_verify.append((vm_number, host, output_file))

                logger.info("Verifying that performance tests actually started on all hosts...")
                time.sleep(2)
                failed_starts = []
                with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(hosts_to_verify))) as verify_pool:
                    verify_futures = []
                    for vm_number, host, output_file in hosts_to_verify:
                        verify_cmd = (
                            f"cd {config.hammerdb_dir} && "
                            f"process_count=$(ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_mssql' | grep -v grep | wc -l) && "
                            f"file_exists=$(test -f '{output_file}' && echo 'yes' || echo 'no') && "
                            f"file_size=$(test -f '{output_file}' && stat -c%s '{output_file}' 2>/dev/null || echo '0') && "
                            f"echo \"PROCESS:$process_count FILE:$file_exists SIZE:$file_size\""
                        )
                        verify_future = verify_pool.submit(
                            executor.execute_command, host, verify_cmd,
                            f"Verifying test started on {host}", timeout=30
                        )
                        verify_futures.append((verify_future, vm_number, host, output_file))

                    for verify_future, vm_number, host, output_file in verify_futures:
                        verify_success, verify_output = verify_future.result()
                        if verify_success and verify_output:
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

            if user_count in config.migrate_user_counts:
                migration_time = rampup_time_seconds + (test_duration_seconds // 2)
                logger.info(f"Migration configured for user_count '{user_count}' - will migrate VMs at {migration_time}s after test start")
                logger.info(f"  (rampup: {rampup_time_seconds}s + test_duration/2: {test_duration_seconds//2}s = {migration_time}s)")
                migration_timestamp = datetime.fromtimestamp(datetime.now().timestamp() + migration_time)
                logger.info(f"Test started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, migration will occur at {migration_timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info(f"Waiting {migration_time}s before triggering VM migrations...")
                time.sleep(migration_time)

                logger.info(f"Triggering VM migrations at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (midpoint of actual test runtime after rampup)...")
                migrate_vms_during_test(config, executor, user_count)

                logger.info("Verifying HammerDB processes are still running after migration...")
                with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
                    futures = {}
                    for host in config.db_hosts:
                        vm_number = get_vm_number(host)
                        cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_mssql' | grep -v grep | wc -l"
                        futures[pool.submit(executor.execute_command, host, cmd, "Checking HammerDB process status after migration", timeout=30)] = host
                    for future in as_completed(futures):
                        host = futures[future]
                        success, output = future.result()
                        if success:
                            process_count = int(output.strip()) if output.strip().isdigit() else 0
                            if process_count > 0:
                                logger.info(f"✓ HammerDB process confirmed running on {host} after migration")
                            else:
                                logger.warning(f"⚠ HammerDB process not found on {host} after migration - test may have completed or failed")

            logger.info(f"Waiting for performance tests with {user_count} users (run {run_id}) to complete...")
            with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
                futures = []
                for host in config.db_hosts:
                    vm_number = get_vm_number(host)
                    output_file = _test_output_file(
                        run_date, num_hosts, vm_number, user_count, run_id
                    )
                    check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_mssql' | grep -v grep | wc -l"
                    future = pool.submit(executor.execute_command, host, check_cmd, f"Checking test status on {host}", timeout=30)
                    futures.append((future, vm_number, host, output_file))

                time.sleep(5)

                test_duration_seconds = int(config.test_duration) * 60 if config.test_duration else 900
                rampup_seconds = int(config.rampup_time) * 60 if config.rampup_time else 120
                start_time = time.time()
                check_interval = 30
                max_wait_time = rampup_seconds + test_duration_seconds + 300

                while time.time() - start_time < max_wait_time:
                    all_done = True
                    running_count = 0

                    for future, vm_number, host, output_file in futures:
                        try:
                            success, output = future.result(timeout=1)
                            if success:
                                process_count = int(output.strip()) if output.strip().isdigit() else 0
                                if process_count > 0:
                                    all_done = False
                                    running_count += 1
                        except Exception:
                            check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_mssql' | grep -v grep | wc -l"
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

                    futures = []
                    for host in config.db_hosts:
                        vm_number = get_vm_number(host)
                        check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_mssql' | grep -v grep | wc -l"
                        future = pool.submit(executor.execute_command, host, check_cmd, f"Checking test status on {host}", timeout=30)
                        futures.append((
                            future, vm_number, host,
                            _test_output_file(run_date, num_hosts, vm_number, user_count, run_id),
                        ))

            expected_minutes = int(config.test_duration) if config.test_duration else 15
            for host in config.db_hosts:
                vm_number = get_vm_number(host)
                output_file = _test_output_file(
                    run_date, num_hosts, vm_number, user_count, run_id
                )
                if not config.dry_run:
                    verify_cmd = (
                        f"cd {config.hammerdb_dir} && "
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

            logger.info(f"Collecting test results for {user_count} users (run {run_id}):")
            for host in config.db_hosts:
                vm_number = get_vm_number(host)
                output_file = _test_output_file(
                    run_date, num_hosts, vm_number, user_count, run_id
                )
                if not config.dry_run:
                    cmd = f"cd {config.hammerdb_dir}; grep 'TEST RESULT' '{output_file}' | tail -1"
                    success, result = executor.execute_command(host, cmd, "Collecting TPM results", timeout=30)
                    if success:
                        tpm = _extract_mssql_tpm(result)
                        if tpm is not None:
                            logger.info(f"Host {host}: {tpm} TPM")
                        else:
                            logger.warning(f"Host {host}: Could not parse TPM from result line: {result.strip()!r}")
                    else:
                        logger.warning(f"Host {host}: Error collecting results")
                else:
                    logger.info(f"DRY-RUN: Would collect results from {host}")

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

