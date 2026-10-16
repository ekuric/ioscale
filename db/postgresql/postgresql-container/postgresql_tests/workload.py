"""HammerDB TPCC performance test execution."""
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Optional

from postgresql_tests.config import PostgreSQLTestConfig
from postgresql_tests.executor import CommandExecutor
from postgresql_tests.migration import VMMigrationMonitor, migrate_vms_during_test
from postgresql_tests.util import _extract_postgresql_tpm, get_vm_number

logger = logging.getLogger("postgresql_tests")

def run_tests(config: PostgreSQLTestConfig, executor: CommandExecutor, migration_monitor: Optional['VMMigrationMonitor'] = None) -> None:
    """Run performance tests"""
    logger.info("Running performance tests...")
    
    num_hosts = len(config.db_hosts)
    run_date = datetime.now().strftime("%Y.%m.%d")
    
    for user_count in config.user_count:
        logger.info(f"Starting test run with {user_count} users on all hosts...")
        
        if migration_monitor:
            migration_monitor.current_operation = f"tpcc {user_count} users"
        
        # Step 1: Setup test scripts
        logger.info(f"Preparing test scripts for {user_count} users...")
        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
            futures = []
            for host in config.db_hosts:
                vm_number = get_vm_number(host)
                cmd = (
                    f"cd {config.hammerdb_dir} && "
                    f"cp '{config.hammerdb_path}/templates/postgresql/postgresqlsetup/runtest_pg.tcl' runtest{vm_number}_pg.tcl && "
                    f"sed -i 's/^diset tpcc pg_count_ware.*/diset tpcc pg_count_ware {config.warehouse_count}/g' runtest{vm_number}_pg.tcl && "
                    f"sed -i 's/^vuset.*/vuset vu {user_count}/g' runtest{vm_number}_pg.tcl && "
                    f"sed -i 's/^diset tpcc pg_duration.*/diset tpcc pg_duration {config.test_duration}/g' runtest{vm_number}_pg.tcl" +
                    (f" && sed -i 's/^diset tpcc pg_rampup.*/diset tpcc pg_rampup {config.rampup_time}/g' runtest{vm_number}_pg.tcl" if config.rampup_time else "")
                )
                future = pool.submit(executor.execute_command, host, cmd, f"Preparing test script (runtest{vm_number}_pg.tcl) for {user_count} users")
                futures.append(future)
            for future in as_completed(futures):
                future.result()
        
        # Step 2: Run performance tests
        logger.info(f"Executing performance tests with {user_count} users...")
        # Calculate test duration and migration timing upfront
        test_duration_seconds = int(config.test_duration) * 60 if config.test_duration else 900
        rampup_time_seconds = int(config.rampup_time) * 60 if config.rampup_time else 120
        
        # Start tests - use short timeout just to verify command starts (nohup should return quickly)
        test_start_time = time.time()
        logger.info(f"Starting performance tests at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
            futures = []
            for host in config.db_hosts:
                vm_number = get_vm_number(host)
                output_file = f"test_postgresql_pg_{run_date}_{num_hosts}pod_pod{vm_number}_{user_count}.out"
                # Use nohup with & to truly background the process and return immediately
                # The command should return quickly, but we use a longer timeout to account for slow SSH connections
                cmd = f"cd {config.hammerdb_dir} && (nohup ./hammerdbcli auto runtest{vm_number}_pg.tcl > '{output_file}' 2>&1 </dev/null &) && echo 'started'"
                future = pool.submit(executor.execute_command, host, cmd, f"Starting performance test (output: {output_file})", timeout=90, max_retries=1)
                futures.append((future, vm_number, host, output_file))
            
            # Wait for all tests to start (should be quick with nohup)
            # Collect results and verify tests actually started
            hosts_to_verify = []
            for future, vm_number, host, output_file in futures:
                try:
                    success, output = future.result()  # Wait for the future (already has timeout)
                    if not success:
                        # Command may have timed out, but test might still be running
                        logger.warning(f"Command to start test on {host} reported failure, verifying test status...")
                        hosts_to_verify.append((vm_number, host, output_file))
                    else:
                        # Command succeeded, but verify test actually started
                        hosts_to_verify.append((vm_number, host, output_file))
                except Exception as e:
                    # Future exception occurred (shouldn't happen, but handle it)
                    logger.warning(f"Exception getting result for {host}: {e}, verifying test status...")
                    hosts_to_verify.append((vm_number, host, output_file))
            
            # Verify tests actually started by checking processes and output files
            logger.info("Verifying that performance tests actually started on all hosts...")
            time.sleep(2)  # Give processes a moment to start
            failed_starts = []
            with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(hosts_to_verify))) as verify_pool:
                verify_futures = []
                for vm_number, host, output_file in hosts_to_verify:
                    # Check both process and output file
                    verify_cmd = (
                        f"cd {config.hammerdb_dir} && "
                        f"process_count=$(ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_pg' | grep -v grep | wc -l) && "
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
            migrate_vms_during_test(config, executor, user_count)
            
            # Verify HammerDB processes are still running after migration
            logger.info("Verifying HammerDB processes are still running after migration...")
            with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
                futures = {}
                for host in config.db_hosts:
                    vm_number = get_vm_number(host)
                    cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_pg' | grep -v grep | wc -l"
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
        
        # Wait for tests to complete
        logger.info(f"Waiting for performance tests with {user_count} users to complete...")
        with ThreadPoolExecutor(max_workers=config.max_pool_workers(len(config.db_hosts))) as pool:
            futures = []
            for host in config.db_hosts:
                vm_number = get_vm_number(host)
                output_file = f"test_postgresql_pg_{run_date}_{num_hosts}pod_pod{vm_number}_{user_count}.out"
                # Check if test is still running by looking for the process
                check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_pg' | grep -v grep | wc -l"
                future = pool.submit(executor.execute_command, host, check_cmd, f"Checking test status on {host}", timeout=30)
                futures.append((future, vm_number, host, output_file))
            
            # Wait a bit for tests to start
            time.sleep(5)
            
            # Monitor test completion
            test_duration_seconds = int(config.test_duration) * 60 if config.test_duration else 900
            start_time = time.time()
            check_interval = 30  # Check every 30 seconds
            max_wait_time = test_duration_seconds + 300  # Add 5 minute buffer
            
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
                        # Re-check this host
                        check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_pg' | grep -v grep | wc -l"
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
                
                # Recreate futures for next check
                futures = []
                for host in config.db_hosts:
                    vm_number = get_vm_number(host)
                    check_cmd = f"ps aux | grep -E 'hammerdbcli.*runtest{vm_number}_pg' | grep -v grep | wc -l"
                    future = pool.submit(executor.execute_command, host, check_cmd, f"Checking test status on {host}", timeout=30)
                    futures.append((future, vm_number, host, f"test_postgresql_pg_{run_date}_{num_hosts}pod_pod{vm_number}_{user_count}.out"))
        
        # Step 3: Collect results
        logger.info(f"Collecting test results for {user_count} users:")
        for host in config.db_hosts:
            vm_number = get_vm_number(host)
            output_file = f"test_postgresql_pg_{run_date}_{num_hosts}pod_pod{vm_number}_{user_count}.out"
            if not config.dry_run:
                cmd = f"cd {config.hammerdb_dir}; grep 'TEST RESULT' '{output_file}' | tail -1"
                success, result = executor.execute_command(host, cmd, "Collecting TPM results", timeout=30)
                if success:
                    tpm = _extract_postgresql_tpm(result)
                    if tpm is not None:
                        logger.info(f"Host {host}: {tpm} TPM")
                    else:
                        logger.warning(f"Host {host}: Could not parse TPM from result line: {result.strip()!r}")
                else:
                    logger.warning(f"Host {host}: Error collecting results")
            else:
                logger.info(f"DRY-RUN: Would collect results from {host}")
        
        logger.info(f"Completed test run with {user_count} users on all hosts")
