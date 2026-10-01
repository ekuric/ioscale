"""FIO performance test execution."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from fio_tests.config import FioTestConfig
from fio_tests.constants import CHECK_INTERVAL, RUNTIME_BUFFER, UNREACHABLE_GRACE_WAIT
from fio_tests.executor import CommandExecutor
from fio_tests.migration import migrate_vms_during_test
from fio_tests.util import (
    build_fio_fsync_option,
    build_linux_fio_thread_option,
    fio_runtime_flags,
    normalize_windows_path,
    parse_optional_runtime,
    windows_fio_directory_arg,
)

if TYPE_CHECKING:
    from fio_tests.migration import VMMigrationMonitor

logger = logging.getLogger("fio_tests")

def run_fio_tests(config: FioTestConfig, executor: CommandExecutor, migration_monitor: Optional['VMMigrationMonitor'] = None) -> None:
    """
    Run FIO performance tests across all configured hosts.

    Executes FIO tests for all combinations of block sizes and I/O patterns.
    Tests are run sequentially - each combination runs on all hosts in parallel
    before moving to the next combination.

    If migration is configured for a given I/O pattern, VMs are migrated
    at the midpoint of the test runtime.

    Args:
        config: FIO test configuration object.
        executor: Command executor for remote operations.
        migration_monitor: Optional VM migration monitor for tracking migrations.
    """
    logger.info("Running FIO performance tests...")
    
    # Separate Linux and Windows hosts
    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()
    
    # Get test parameters - use Linux config for Linux hosts, Windows config for Windows hosts
    # We'll run tests for both Linux and Windows block sizes/patterns
    linux_block_sizes = config.block_sizes if config.block_sizes else []
    linux_io_patterns = config.io_patterns if config.io_patterns else []
    windows_block_sizes = config.windows_block_sizes if config.windows_block_sizes else linux_block_sizes
    windows_io_patterns = config.windows_io_patterns if config.windows_io_patterns else linux_io_patterns
    
    logger.info(f"Linux hosts: {linux_hosts}")
    if linux_hosts:
        logger.info(f"Linux block sizes: {linux_block_sizes}")
        logger.info(f"Linux I/O patterns: {linux_io_patterns}")
    logger.info(f"Windows hosts: {windows_hosts}")
    if windows_hosts:
        logger.info(f"Windows block sizes: {windows_block_sizes}")
        logger.info(f"Windows I/O patterns: {windows_io_patterns}")
    
    # Preserve config order (do not sort) so progress numbering matches declared lists
    def _unique_preserve(seq):
        seen = set()
        ordered = []
        for item in seq:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    all_block_sizes = _unique_preserve(linux_block_sizes + windows_block_sizes)
    all_io_patterns = _unique_preserve(linux_io_patterns + windows_io_patterns)
    
    logger.info(f"All block sizes to test: {all_block_sizes}")
    logger.info(f"All I/O patterns to test: {all_io_patterns}")

    # Precompute combinations that will actually run on at least one host OS
    planned_tests = [
        (bs, pattern)
        for bs in all_block_sizes
        for pattern in all_io_patterns
        if ((bs in linux_block_sizes and pattern in linux_io_patterns and linux_hosts) or
            (bs in windows_block_sizes and pattern in windows_io_patterns and windows_hosts))
    ]
    total_tests = len(planned_tests)
    logger.info(f"Total FIO test combinations to run: {total_tests}")

    test_counter = 0

    for bs, pattern in planned_tests:
        test_counter += 1
        tests_remaining = total_tests - test_counter
        logger.info(
            f"Running test {test_counter}/{total_tests} "
            f"({tests_remaining} remaining): {pattern} with block size {bs}"
        )
        logger.debug(f"  Linux check: bs='{bs}' in {linux_block_sizes}? {bs in linux_block_sizes}, pattern='{pattern}' in {linux_io_patterns}? {pattern in linux_io_patterns}")
        logger.debug(f"  Windows check: bs='{bs}' in {windows_block_sizes}? {bs in windows_block_sizes}, pattern='{pattern}' in {windows_io_patterns}? {pattern in windows_io_patterns}")
        
        if migration_monitor:
            migration_monitor.current_operation = f"test {test_counter}/{total_tests}: {pattern} bs={bs}"
        
        # Start FIO tests on all hosts
        threads = []
        test_name = f"fio-test-{pattern}-bs-{bs}"
        # Per-host FIO command (used to relaunch after paused-VM recovery)
        host_fio_cmds: Dict[str, str] = {}
        host_fio_desc: Dict[str, str] = {}
        recovered_hosts: set = set()
        # First time we saw paused/unreachable for a host this test (grace before restart)
        access_issue_since: Dict[str, float] = {}
        deadline_extension = 0
        
        # Linux hosts
        linux_should_run = bs in linux_block_sizes and pattern in linux_io_patterns
        logger.debug(f"  Linux should run: {linux_should_run}")
        if linux_should_run:
            logger.info(
                f"Running Linux test {test_counter}/{total_tests}: "
                f"{pattern} with block size {bs} on hosts: {linux_hosts}"
            )
            for host in linux_hosts:
                fio_cmd = (
                    f"cd {config.output_dir} && fio "
                    f"--ioengine={config.ioengine} "
                    f"--name=testfile "
                    f"--directory={config.mount_point} "
                    f"--size={config.test_size} "
                    f"--rw={pattern} "
                    f"--bs={bs} "
                    f"{fio_runtime_flags(config.test_runtime)}"
                    f"--direct={config.direct_io} "
                    f"{build_fio_fsync_option(config.fsync)}"
                    f"--numjobs={config.numjobs} "
                    f"--iodepth={config.iodepth} "
                    f"{build_linux_fio_thread_option(config.ioengine)}"
                    f"--output-format={config.output_format} "
                    f"--group_reporting"
                )
                
                if config.rate_iops:
                    fio_cmd += f" --rate_iops={config.rate_iops}"
                
                fio_cmd += f" --output={test_name}.json"
                fio_desc = f"FIO test: {pattern}, block size: {bs}"
                host_fio_cmds[host] = fio_cmd
                host_fio_desc[host] = fio_desc
                
                logger.info(f"Starting FIO test on {host}: {test_name}")
                thread = executor.execute_background(host, fio_cmd, fio_desc)
                threads.append(thread)
        else:
            logger.debug(f"Skipping Linux test: {pattern} with block size {bs} (bs in {linux_block_sizes}? {bs in linux_block_sizes}, pattern in {linux_io_patterns}? {pattern in linux_io_patterns})")
        
        # Windows hosts
        if bs in windows_block_sizes and pattern in windows_io_patterns:
            for host in windows_hosts:
                fio_dir = normalize_windows_path(config.windows_fio_dir)
                output_dir_win = normalize_windows_path(config.windows_output_dir)
                
                # Ensure fio_dir has trailing slash for proper path construction
                if not fio_dir.endswith('/'):
                    fio_dir += '/'
                
                # Native Windows path for fio --directory= (no c\:\… bash-style escape)
                mount_point_fio = windows_fio_directory_arg(config.windows_mount_point)
                fio_cmd = (
                    f"powershell cd {fio_dir} ; {fio_dir}fio.exe "
                    f"--ioengine=windowsaio "
                    f"--name=fiodatafile "
                    f"--directory={mount_point_fio} "
                    f"--size={config.windows_test_size} "
                    f"--rw={pattern} "
                    f"--bs={bs} "
                    f"{fio_runtime_flags(config.windows_test_runtime)}"
                    f"--direct={config.windows_direct_io} "
                    f"{build_fio_fsync_option(config.windows_fsync)}"
                    f"--numjobs={config.windows_numjobs} "
                    f"--iodepth={config.windows_iodepth} "
                    f"--output-format={config.windows_output_format} "
                    f"--thread "
                    f"--overwrite=1 "
                    f"--group_reporting"
                )
                
                # Add rate_iops only if it's set (matches bash script logic)
                if config.windows_rate_iops:
                    fio_cmd += f" --rate_iops={config.windows_rate_iops}"
                
                fio_cmd += f" --output={output_dir_win}/{test_name}.json"
                fio_desc = f"FIO test: {pattern}, block size: {bs}"
                host_fio_cmds[host] = fio_cmd
                host_fio_desc[host] = fio_desc
                
                logger.info(f"Starting FIO test on {host}: {test_name}")
                thread = executor.execute_background(host, fio_cmd, fio_desc)
                threads.append(thread)
        
        # Check if migration is needed
        if pattern in config.migrate_workloads:
            linux_runtime = parse_optional_runtime(config.test_runtime) or 0
            windows_runtime = parse_optional_runtime(config.windows_test_runtime) or 0
            test_runtime_int = max(linux_runtime, windows_runtime)
            if test_runtime_int <= 0:
                logger.warning(
                    f"Migration configured for pattern '{pattern}' but runtime is omitted "
                    f"(size-based) — skipping timed midpoint migration"
                )
            else:
                half_runtime = test_runtime_int // 2
                logger.info(
                    f"Migration configured for pattern '{pattern}' - will migrate VMs at "
                    f"{half_runtime}s (midpoint of {test_runtime_int}s runtime)"
                )
                logger.info(f"Waiting {half_runtime}s before triggering VM migrations...")
                time.sleep(half_runtime)

                logger.info("Triggering VM migrations at midpoint of test runtime...")
                migrate_vms_during_test(config, pattern, executor)
        
        # Wait for all threads to start (they just start the FIO process)
        for thread in threads:
            thread.join(timeout=config.timeout_check_interval)  # Wait for thread to start the process
        
        # Now wait for FIO processes to actually complete
        logger.info(
            f"Waiting for all FIO tests to complete for test {test_counter}/{total_tests}: "
            f"{pattern} with block size {bs} "
            f"({tests_remaining} remaining)..."
        )
        linux_runtime = parse_optional_runtime(config.test_runtime) or 0
        windows_runtime = parse_optional_runtime(config.windows_test_runtime) or 0
        test_runtime_int = max(linux_runtime, windows_runtime)
        size_based_test = test_runtime_int <= 0
        if size_based_test:
            logger.info(
                "FIO wait mode: size-based (runtime omitted) — waiting until processes exit"
            )
        start_time = time.time()
        check_interval = config.timeout_check_interval
        active_hosts = list(host_fio_cmds.keys())
        completed_hosts = set()
        total_hosts = len(active_hosts)
        no_result_streak: Dict[str, int] = {}
        no_result_streak_needed = 3
        
        while True:
            all_done = True
            running_count = 0
            running_hosts = []
            check_failures = 0
            recovery_candidates = []  # (host, status)
            newly_completed = []
            
            # Check hosts that were started for this combo (parallel)
            with ThreadPoolExecutor(max_workers=min(max(len(active_hosts), 1), config.max_workers)) as pool:
                check_futures = {}
                for host in active_hosts:
                    if executor.is_windows_host(host):
                        future = pool.submit(executor.check_task_status, host, "fio")
                    else:
                        future = pool.submit(executor.check_task_status, host, f"fio.*{test_name}")
                    check_futures[future] = host
                
                for future in as_completed(check_futures):
                    host = check_futures[future]
                    try:
                        status = future.result()
                        if status == "running":
                            access_issue_since.pop(host, None)
                            no_result_streak.pop(host, None)
                            all_done = False
                            running_count += 1
                            running_hosts.append(host)
                        elif status in ("paused", "unreachable"):
                            # Only recover if this host still needs a result for the current test
                            if host in recovered_hosts:
                                all_done = False  # wait / give up after prior recovery attempt
                            elif executor.has_fio_result_file(host, test_name):
                                access_issue_since.pop(host, None)
                                no_result_streak.pop(host, None)
                                logger.info(
                                    f"{host}: Host {status} but result for {test_name} exists - treating as done"
                                )
                                if host not in completed_hosts:
                                    completed_hosts.add(host)
                                    newly_completed.append(host)
                            else:
                                all_done = False
                                now = time.time()
                                first_seen = access_issue_since.setdefault(host, now)
                                issue_age = now - first_seen
                                remaining_grace = max(0, UNREACHABLE_GRACE_WAIT - issue_age)
                                if remaining_grace > 0:
                                    logger.warning(
                                        f"{host}: Host {status} during FIO test '{test_name}' - "
                                        f"retrying (grace {int(issue_age)}s/{UNREACHABLE_GRACE_WAIT}s, "
                                        f"{int(remaining_grace)}s left before VM restart)"
                                    )
                                else:
                                    recovery_candidates.append((host, status))
                        elif status == "stopped":
                            # Finished, or never started / died without connectivity error.
                            # If result is missing, check once for paused VMI before accepting done.
                            if (host not in recovered_hosts
                                    and host in host_fio_cmds
                                    and not executor.has_fio_result_file(host, test_name)):
                                if executor.is_vmi_paused(host):
                                    all_done = False
                                    now = time.time()
                                    first_seen = access_issue_since.setdefault(host, now)
                                    issue_age = now - first_seen
                                    remaining_grace = max(0, UNREACHABLE_GRACE_WAIT - issue_age)
                                    if remaining_grace > 0:
                                        logger.warning(
                                            f"{host}: FIO not running, no result, VMI paused - "
                                            f"retrying (grace {int(issue_age)}s/{UNREACHABLE_GRACE_WAIT}s, "
                                            f"{int(remaining_grace)}s left before VM restart)"
                                        )
                                    else:
                                        recovery_candidates.append((host, "paused"))
                                else:
                                    # Do not instantly accept "finished" with no JSON — FIO may
                                    # not have appeared yet after detached launch, or crashed.
                                    streak = no_result_streak.get(host, 0) + 1
                                    no_result_streak[host] = streak
                                    if streak < no_result_streak_needed:
                                        all_done = False
                                        logger.warning(
                                            f"{host}: FIO not running and no result for {test_name} "
                                            f"({streak}/{no_result_streak_needed} before giving up) — will recheck"
                                        )
                                    else:
                                        access_issue_since.pop(host, None)
                                        logger.error(
                                            f"{host}: FIO not running and no result for {test_name} "
                                            f"after {no_result_streak_needed} checks - treating as finished "
                                            f"(missing result)"
                                        )
                                        if host not in completed_hosts:
                                            completed_hosts.add(host)
                                            newly_completed.append(host)
                            else:
                                access_issue_since.pop(host, None)
                                no_result_streak.pop(host, None)
                                if host not in completed_hosts:
                                    completed_hosts.add(host)
                                    newly_completed.append(host)
                    except Exception as e:
                        check_failures += 1
                        logger.debug(f"Failed to check task status on {host}: {e}")
            
            # After grace period: restart paused/unreachable VMs and relaunch FIO (once per host/test)
            if recovery_candidates:
                unique_hosts = []
                seen = set()
                for host, status in recovery_candidates:
                    if host in seen or host in recovered_hosts:
                        continue
                    seen.add(host)
                    unique_hosts.append((host, status))
                
                if unique_hosts:
                    logger.warning(
                        f"Recovering {len(unique_hosts)} paused/unreachable VM(s) for "
                        f"{test_name} after {UNREACHABLE_GRACE_WAIT}s grace: "
                        f"{[h for h, _ in unique_hosts]}"
                    )
                    
                    def _recover(host: str) -> Tuple[str, bool]:
                        return host, executor.recover_paused_vm_and_relaunch_fio(
                            host,
                            host_fio_cmds[host],
                            test_name,
                            host_fio_desc.get(host, f"FIO test: {pattern}, block size: {bs}"),
                        )
                    
                    for host, _status in unique_hosts:
                        recovered_hosts.add(host)
                        access_issue_since.pop(host, None)
                    
                    recovered_ok = 0
                    with ThreadPoolExecutor(max_workers=min(len(unique_hosts), config.max_workers)) as pool:
                        recover_futures = [pool.submit(_recover, host) for host, _ in unique_hosts]
                        for future in as_completed(recover_futures):
                            host, ok = future.result()
                            if ok:
                                recovered_ok += 1
                                logger.info(f"{host}: FIO relaunched after VM recovery")
                            else:
                                logger.error(f"{host}: VM recovery / FIO relaunch failed")
                    
                    if recovered_ok:
                        if size_based_test:
                            # Size-based: no known runtime; extend by buffer only
                            extra = config.timeout_runtime_buffer
                        else:
                            extra = test_runtime_int + config.timeout_runtime_buffer
                        deadline_extension += extra
                        logger.info(
                            f"Extended wait window by {extra}s after "
                            f"{recovered_ok} VM recovery(ies)"
                        )
                    all_done = False
                    running_count = max(running_count, recovered_ok)
            
            if newly_completed:
                logger.info(
                    f"FIO test completed on: {', '.join(sorted(newly_completed))}"
                )
            
            if all_done:
                logger.info(
                    f"All FIO test processes completed for test {test_counter}/{total_tests}: "
                    f"{pattern} with block size {bs}"
                )
                break
            
            elapsed = time.time() - start_time
            remaining_hosts = [
                h for h in active_hosts
                if h not in completed_hosts
            ]
            # Time-based: enforce runtime + buffer. Size-based: only soft-cap if dataset_hard set.
            over_deadline = False
            if size_based_test:
                if config.timeout_dataset_hard is not None:
                    over_deadline = elapsed > (
                        int(config.timeout_dataset_hard) + deadline_extension
                    )
            else:
                over_deadline = elapsed > (
                    test_runtime_int + config.timeout_runtime_buffer + deadline_extension
                )
            if over_deadline:
                if size_based_test:
                    logger.warning(
                        f"FIO size-based test exceeded dataset_hard "
                        f"({config.timeout_dataset_hard}s)"
                    )
                else:
                    logger.warning(f"FIO test exceeded expected time ({test_runtime_int}s)")
                logger.warning(
                    f"{len(remaining_hosts)} hosts remaining"
                    f"{(': ' + ', '.join(sorted(remaining_hosts))) if remaining_hosts else ''}"
                )
                # Check if result files exist - if they do, the test likely completed
                result_files_exist = 0
                hosts_to_check = active_hosts or list(config.vm_hosts)
                with ThreadPoolExecutor(max_workers=min(len(hosts_to_check), config.max_workers)) as pool:
                    file_futures = {}
                    for host in hosts_to_check:
                        future = pool.submit(executor.has_fio_result_file, host, test_name)
                        file_futures[future] = host
                    for future in as_completed(file_futures):
                        host = file_futures[future]
                        try:
                            if future.result():
                                result_files_exist += 1
                        except Exception as e:
                            logger.debug(f"Result file check failed on {host}: {e}")
                
                if result_files_exist == len(hosts_to_check):
                    logger.info(f"All result files exist - test completed successfully despite timeout warnings")
                    break
                else:
                    logger.warning(f"Only {result_files_exist}/{len(hosts_to_check)} result files exist")
                    break
            
            logger.info(
                f"Waiting for FIO test {test_counter}/{total_tests} "
                f"({pattern} bs={bs})... "
                f"({len(remaining_hosts)} hosts remaining"
                f"{(': ' + ', '.join(sorted(remaining_hosts))) if remaining_hosts else ''}, "
                f"{len(completed_hosts)}/{total_hosts} completed, "
                f"{int(elapsed)}s elapsed, "
                f"{tests_remaining} tests remaining after this)"
            )
            time.sleep(check_interval)
        
        logger.info(
            f"Completed test {test_counter}/{total_tests}: {pattern} with block size {bs} "
            f"({tests_remaining} remaining)"
        )
    
    logger.info(f"Completed all FIO performance tests ({total_tests} combinations)")



