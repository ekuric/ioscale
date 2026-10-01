"""Dataset (pre-write) helpers and write_test_data."""

import base64
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from fio_tests.config import FioTestConfig
from fio_tests.constants import (
    CHECK_INTERVAL,
    DATASET_STALL_SECONDS,
    DATASET_WRITE_BUFFER,
    DATASET_WRITE_RETRIES,
)
from fio_tests.executor import CommandExecutor
from fio_tests.util import (
    build_fio_fsync_option,
    build_linux_fio_thread_option,
    normalize_windows_path,
    windows_fio_directory_arg,
)

logger = logging.getLogger("fio_tests")

def _dataset_hard_timeout_seconds(config: FioTestConfig) -> int:
    """Max wait hint for size-based dataset write (no --runtime on dataset)."""
    if config.timeout_dataset_hard is not None:
        return int(config.timeout_dataset_hard)
    # Dataset is always size-based; do not derive wait from test runtime.
    return max(int(config.timeout_dataset_stall) * 3, 3600) + int(config.timeout_dataset_buffer)


def _parse_fio_size_to_bytes(size_str: Optional[str]) -> Optional[int]:
    """Parse FIO size strings like 8G, 512M, 1024k into bytes (binary units)."""
    if not size_str:
        return None
    text = str(size_str).strip().lower().replace(" ", "")
    match = re.match(r'^(\d+(?:\.\d+)?)([kmgtpe]i?b?)?$', text)
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "").rstrip("b")
    multipliers = {
        "": 1,
        "k": 1024,
        "ki": 1024,
        "m": 1024 ** 2,
        "mi": 1024 ** 2,
        "g": 1024 ** 3,
        "gi": 1024 ** 3,
        "t": 1024 ** 4,
        "ti": 1024 ** 4,
        "p": 1024 ** 5,
        "pi": 1024 ** 5,
        "e": 1024 ** 6,
        "ei": 1024 ** 6,
    }
    if unit not in multipliers:
        return None
    return int(value * multipliers[unit])


def _expected_dataset_data_bytes(config: FioTestConfig, host: str) -> Optional[int]:
    """Expected total dataset file bytes for a host (size * numjobs)."""
    if host in config.windows_hosts:
        per_file = _parse_fio_size_to_bytes(config.windows_test_size)
        numjobs = int(config.windows_numjobs or 1)
    else:
        per_file = _parse_fio_size_to_bytes(config.test_size)
        numjobs = int(config.numjobs or 1)
    if per_file is None:
        return None
    return per_file * max(numjobs, 1)


def _dataset_files_full_size(nbytes: int, expected_bytes: Optional[int]) -> bool:
    """True when observed dataset bytes are at (or nearly) full configured size."""
    if expected_bytes is None or expected_bytes <= 0 or nbytes <= 0:
        return False
    # Allow tiny slack for filesystem accounting; require ~99% of expected data size.
    return nbytes >= int(expected_bytes * 0.99)


def _linux_dataset_fio_cmd(config: FioTestConfig) -> str:
    # Dataset pre-write is always size-based: full sequential write of --size (fast fill).
    # Configured --runtime applies only to the later FIO performance tests.
    return (
        f"cd {config.output_dir} && fio "
        f"--ioengine={config.ioengine} "
        f"--name=testfile "
        f"--directory={config.mount_point} "
        f"--size={config.test_size} "
        f"--rw=write "
        f"--bs=1M "
        f"--direct={config.direct_io} "
        f"{build_fio_fsync_option(config.fsync)}"
        f"--numjobs={config.numjobs} "
        f"--iodepth=32 "
        f"{build_linux_fio_thread_option(config.ioengine)}"
        f"--output-format={config.output_format} "
        f"--overwrite=1 "
        f"--output=write_dataset.json"
    )


def _windows_dataset_fio_cmd(config: FioTestConfig) -> str:
    # Windows: one full sequential write of --size (not randwrite/time-based) so NTFS
    # allocates the test file before perf runs — see linux-win-differences.md.
    fio_dir = normalize_windows_path(config.windows_fio_dir)
    output_dir_win = normalize_windows_path(config.windows_output_dir)
    if not fio_dir.endswith('/'):
        fio_dir += '/'
    mount_point_fio = windows_fio_directory_arg(config.windows_mount_point)
    return (
        f"powershell cd {fio_dir} ; {fio_dir}fio.exe "
        f"--ioengine=windowsaio "
        f"--name=fiodatafile "
        f"--directory={mount_point_fio} "
        f"--size={config.windows_test_size} "
        f"--rw=write "
        f"--bs=1M "
        f"--direct={config.windows_direct_io} "
        f"{build_fio_fsync_option(config.windows_fsync)}"
        f"--numjobs={config.windows_numjobs} "
        f"--iodepth=32 "
        f"--output-format={config.windows_output_format} "
        f"--thread "
        f"--overwrite=1 "
        f"--output={output_dir_win}/write_dataset.json"
    )


def _launch_linux_dataset_nohup(executor: CommandExecutor, host: str, fio_cmd: str) -> Dict:
    """
    Start Linux dataset FIO via setsid+nohup and return job metadata (pid/script/log).

    setsid detaches from the virtctl/SSH session process group so a local command
    timeout/SIGTERM on the SSH helper does not kill the remote FIO job.

    The launch SSH is fire-and-forget (print PID immediately, no remote sleep/ps).
    Under high parallel launch load, virtctl often exceeds the setup timeout even
    after FIO has started — so we never retry the launch (avoids duplicate FIO)
    and always confirm via a separate short pgrep.
    """
    safe_host = re.sub(r'[^a-zA-Z0-9._-]', '_', host)
    script_file = f"/tmp/fio_run_{int(time.time())}_{os.getpid()}_{safe_host}.sh"
    log_file = f"/tmp/fio_background_{int(time.time())}_{os.getpid()}_{safe_host}.log"
    encoded_cmd = base64.b64encode(fio_cmd.encode()).decode()
    # Remove any prior aborted/stale JSON so status checks cannot false-DONE.
    output_json = f"{executor.config.output_dir.rstrip('/')}/write_dataset.json"
    # Return as soon as the background job is spawned — do not sleep/ps here;
    # that keeps the SSH session open and causes false 60s timeouts at scale.
    script_cmd = (
        f"echo '{encoded_cmd}' | base64 -d > {script_file} && "
        f"chmod +x {script_file} && "
        f"rm -f '{output_json}' && "
        f"setsid nohup bash {script_file} > {log_file} 2>&1 < /dev/null & "
        f"echo $!"
    )
    success, output = executor.execute_command(
        host, script_cmd, "Writing test dataset (nohup)",
        timeout=executor.config.timeout_nohup_setup,
        max_retries=1,  # never re-launch on SSH timeout (would start a 2nd FIO)
        retry_interval=1,
        quiet=True,
    )

    pid = None
    if success:
        lines = (output or "").strip().splitlines()
        match = re.search(r'\d+', lines[-1]) if lines else None
        if match and match.group() != "0":
            pid = match.group()

    # Always verify independently — launch SSH may time out after FIO started
    if not pid:
        time.sleep(2)
    find_pid_cmd = (
        f"pgrep -f -- '--output=write_dataset.json' 2>/dev/null | head -1 || "
        f"pgrep -f -- '{script_file}' 2>/dev/null | head -1 || "
        f"pgrep -f -- 'fio.*--name=testfile' 2>/dev/null | head -1 || echo 0"
    )
    ok, out = executor.execute_command(
        host, find_pid_cmd, "Find dataset FIO PID", quiet=True, timeout=20, max_retries=2, retry_interval=2
    )
    if ok:
        lines = (out or "").strip().splitlines()
        match = re.search(r'\d+', lines[-1]) if lines else None
        if match and match.group() != "0":
            pid = match.group()

    if pid:
        if success:
            logger.info(f"Dataset FIO started on {host} with PID {pid}")
        else:
            logger.info(
                f"Dataset FIO confirmed running on {host} with PID {pid} "
                f"(launch SSH timed out/failed — treating as success, not retrying)"
            )
    else:
        logger.warning(
            f"Dataset FIO start on {host}: PID unknown after launch "
            f"(success={success}, output={str(output)[:120]!r}); will poll by process pattern"
        )
    return {
        "pid": pid,
        "script_file": script_file,
        "log_file": log_file,
        "fio_cmd": fio_cmd,
    }


def _kill_dataset_fio_on_host(executor: CommandExecutor, host: str, job: Optional[Dict] = None) -> None:
    """Terminate stuck dataset-write FIO (and wrapper) on a host."""
    if executor.is_windows_host(host):
        kill_cmd = (
            "powershell -Command \""
            "Get-Process -Name fio -ErrorAction SilentlyContinue | Stop-Process -Force; "
            "Write-Host killed\""
        )
        executor.execute_command(host, kill_cmd, "Kill dataset FIO", quiet=True, timeout=30)
        return

    pid = (job or {}).get("pid")
    script_file = (job or {}).get("script_file")
    parts = []
    if pid:
        parts.append(f"kill -TERM {pid} 2>/dev/null; sleep 2; kill -KILL {pid} 2>/dev/null; true")
    parts.append("pkill -TERM -f -- '--output=write_dataset.json' 2>/dev/null || true")
    parts.append("sleep 2")
    parts.append("pkill -KILL -f -- '--output=write_dataset.json' 2>/dev/null || true")
    if script_file:
        parts.append(f"pkill -TERM -f -- '{script_file}' 2>/dev/null || true")
        parts.append(f"pkill -KILL -f -- '{script_file}' 2>/dev/null || true")
    parts.append("sleep 1")
    kill_cmd = "; ".join(parts)
    executor.execute_command(host, kill_cmd, "Kill dataset FIO", quiet=True, timeout=60)
    logger.info(f"Sent kill for dataset FIO on {host}" + (f" (pid={pid})" if pid else ""))


def _dataset_progress_bytes(executor: CommandExecutor, host: str, config: FioTestConfig) -> int:
    """Return approximate written dataset bytes (testfile* + json size)."""
    if executor.is_windows_host(host):
        mount_point_win = normalize_windows_path(config.windows_mount_point)
        output_dir_win = normalize_windows_path(config.windows_output_dir)
        cmd = (
            f"powershell -Command \""
            f"$sum = 0; "
            f"Get-ChildItem -Path '{mount_point_win}' -Filter 'fiodatafile*' -ErrorAction SilentlyContinue | "
            f"ForEach-Object {{ $sum += $_.Length }}; "
            f"if (Test-Path '{output_dir_win}/write_dataset.json') {{ "
            f"  $sum += (Get-Item '{output_dir_win}/write_dataset.json').Length "
            f"}}; "
            f"Write-Host $sum\""
        )
    else:
        cmd = (
            f"(du -sb {config.mount_point}/testfile* 2>/dev/null | awk '{{s+=$1}} END{{print s+0}}'; "
            f"stat -c %s {config.output_dir}/write_dataset.json 2>/dev/null || echo 0) | "
            f"awk '{{s+=$1}} END{{print s+0}}'"
        )
    ok, out = executor.execute_command(host, cmd, "Dataset progress bytes", quiet=True, timeout=30)
    if not ok or not out:
        return 0
    try:
        return int(re.search(r'\d+', out.strip().splitlines()[-1]).group())
    except (AttributeError, ValueError):
        return 0


def write_test_data(config: FioTestConfig, executor: CommandExecutor) -> None:
    """
    Write initial test dataset to all hosts.

    Always size-based (no --runtime / --time_based): FIO writes the full --size
    then exits. Configured runtime applies only to later performance tests.
    Stall recovery applies when FIO is still running with incomplete data and
    no byte growth for stall_limit seconds.
    """
    logger.info("Writing initial test dataset...")

    linux_hosts = config.get_linux_hosts()
    windows_hosts = config.get_windows_hosts()
    stall_limit = int(config.timeout_dataset_stall)
    max_attempts = 1 + int(config.dataset_write_retries)
    check_interval = config.timeout_check_interval
    # Dataset write never uses --runtime; stall recovery can apply immediately
    # once growth stops (gate=0).
    expected_runtime = 0

    logger.info(
        "Dataset write mode: always size-based — "
        "FIO exits when --size is fully written (no --runtime / --time_based); "
        "configured runtime applies only to FIO performance tests"
    )
    logger.info(
        f"Dataset write policy: no hard timeout; "
        f"stall_limit={stall_limit}s if data incomplete and no byte growth; "
        f"full-size data + running FIO = wait for JSON; "
        f"max_attempts={max_attempts} "
        f"(1 start + {config.dataset_write_retries} restart)"
    )

    linux_cmd = _linux_dataset_fio_cmd(config) if linux_hosts else None
    windows_cmd = _windows_dataset_fio_cmd(config) if windows_hosts else None

    jobs: Dict[str, Dict] = {}
    jobs_lock = threading.Lock()

    def _init_progress_fields(meta: Dict) -> Dict:
        now = time.time()
        meta.update({
            "attempt": meta.get("attempt", 1),
            "attempt_started": now,
            "last_bytes": 0,
            "last_progress_at": now,
            "retried": meta.get("retried", False),
        })
        return meta

    def _start_host(host: str, attempt: int) -> None:
        if executor.is_windows_host(host):
            logger.info(f"Starting Windows dataset write on {host} (attempt {attempt}/{max_attempts})")
            thread = executor.execute_background(host, windows_cmd, "Writing test dataset")
            meta = _init_progress_fields({
                "attempt": attempt,
                "thread": thread,
                "fio_cmd": windows_cmd,
                "pid": None,
                "script_file": None,
                "log_file": None,
                "retried": attempt > 1,
            })
        else:
            logger.info(f"Starting Linux dataset write on {host} (attempt {attempt}/{max_attempts})")
            meta = _launch_linux_dataset_nohup(executor, host, linux_cmd)
            meta["attempt"] = attempt
            meta["retried"] = attempt > 1
            meta = _init_progress_fields(meta)
        with jobs_lock:
            jobs[host] = meta

    if windows_hosts:
        mount_point_win = normalize_windows_path(config.windows_mount_point)
        logger.info(f"Ensuring mount point directories exist on {len(windows_hosts)} Windows hosts...")
        with ThreadPoolExecutor(max_workers=min(len(windows_hosts), config.max_workers)) as pool:
            dir_futures = []
            for host in windows_hosts:
                ensure_dir_cmd = (
                    f"powershell -Command \"New-Item -ItemType Directory -Force -Path '{mount_point_win}' | Out-Null; "
                    f"if (Test-Path '{mount_point_win}') {{ Write-Host 'EXISTS' }} else {{ Write-Host 'NOT_FOUND' }}\""
                )
                dir_futures.append(
                    (pool.submit(
                        executor.execute_command, host, ensure_dir_cmd,
                        "Ensuring mount point directory exists", timeout=10
                    ), host)
                )
            for future, host in dir_futures:
                dir_success, dir_output = future.result()
                if not (dir_success and 'EXISTS' in (dir_output or '')):
                    logger.warning(f"Mount point directory may not exist on {host}: {mount_point_win}")

    # Launch dataset write on ALL hosts at once (one thread per host, no max_workers batching).
    logger.info(
        f"Starting dataset write on all {len(config.vm_hosts)} hosts in parallel "
        f"(unbounded — not capped by max_workers={config.max_workers})"
    )
    start_threads = []
    for host in config.vm_hosts:
        t = threading.Thread(target=_start_host, args=(host, 1), daemon=True)
        t.start()
        start_threads.append(t)
    for t in start_threads:
        t.join()
    logger.info(f"Dataset write launch completed for {len(jobs)}/{len(config.vm_hosts)} hosts")

    completed_hosts = set()
    failed_hosts = set()
    failed_streak: Dict[str, int] = {}
    failed_streak_needed = 3
    total_hosts = len(config.vm_hosts)
    start_time = time.time()

    def _linux_dataset_running_check(job: Optional[Dict]) -> str:
        pid = (job or {}).get("pid")
        script_file = (job or {}).get("script_file")
        checks = [
            "pgrep -f -- '--output=write_dataset.json' >/dev/null 2>&1",
            "pgrep -f -- 'fio.*--name=testfile' >/dev/null 2>&1",
        ]
        if pid:
            checks.insert(0, f"kill -0 {pid} 2>/dev/null")
        if script_file:
            checks.append(f"pgrep -f -- '{script_file}' >/dev/null 2>&1")
        return " || ".join(checks)

    def _dataset_status_cmd(host: str) -> str:
        """
        DONE only when dataset files are full-size and write_dataset.json is a
        successful completion — not an aborted SIGTERM dump with zero I/O.
        """
        job = jobs.get(host)
        expected = _expected_dataset_data_bytes(config, host) or 0
        # Require real data on disk; never treat aborted/empty JSON alone as DONE.
        min_bytes = int(expected * 0.99) if expected > 0 else 1

        if executor.is_windows_host(host):
            output_dir_win = normalize_windows_path(config.windows_output_dir)
            mount_point_win = normalize_windows_path(config.windows_mount_point)
            output_file = f"{output_dir_win}/write_dataset.json"
            return (
                f"powershell -NoProfile -Command \""
                f"$out = '{output_file}'; $dir = '{mount_point_win}'; $minBytes = {min_bytes}; "
                f"$dataBytes = 0; "
                f"Get-ChildItem -Path $dir -Filter 'fiodatafile*' -ErrorAction SilentlyContinue | "
                f"  ForEach-Object {{ $dataBytes += $_.Length }}; "
                f"$fioRunning = [bool](Get-Process fio -ErrorAction SilentlyContinue); "
                # Prefer Get-Item.Length over Get-Content -Raw: full reads fail/timeout when
                # fio.exe still holds the file or the guest is under heavy disk load, which
                # left hosts stuck in RUNNING forever despite a valid write_dataset.json.
                f"$jsonOk = $false; "
                f"if (Test-Path -LiteralPath $out) {{ "
                f"  $len = [int64](Get-Item -LiteralPath $out).Length; "
                f"  if ($len -ge 32) {{ "
                f"    $jsonOk = $true; "
                f"    try {{ "
                f"      $hit = Select-String -LiteralPath $out -Pattern 'terminating on signal' "
                f"        -SimpleMatch -Quiet -ErrorAction Stop; "
                f"      if ($hit) {{ $jsonOk = $false }} "
                f"    }} catch {{ }} "
                f"  }} "
                f"}}; "
                f"if ($dataBytes -ge $minBytes -and $jsonOk) {{ Write-Host 'DONE' }} "
                f"elseif ($fioRunning) {{ Write-Host 'RUNNING' }} "
                f"else {{ Write-Host 'FAILED' }}\""
            )

        output_file = f"{config.output_dir.rstrip('/')}/write_dataset.json"
        data_glob = f"{config.mount_point.rstrip('/')}/testfile*"
        running = _linux_dataset_running_check(job)
        # Shell status:
        # DONE = full data files + non-empty JSON without 'terminating on signal'
        # RUNNING = fio alive, or data full while waiting for a good JSON
        # FAILED = fio gone with incomplete data and/or aborted JSON
        return (
            f"output_file='{output_file}'; "
            f"min_bytes={min_bytes}; "
            f"data_bytes=$(du -sb {data_glob} 2>/dev/null | awk '{{s+=$1}} END{{print s+0}}'); "
            f"json_aborted=0; json_present=0; "
            f"if test -s \"$output_file\"; then "
            f"  json_present=1; "
            f"  if grep -q 'terminating on signal' \"$output_file\" 2>/dev/null; then json_aborted=1; fi; "
            f"fi; "
            f"if [ \"$data_bytes\" -ge \"$min_bytes\" ] && [ \"$json_present\" -eq 1 ] && [ \"$json_aborted\" -eq 0 ]; then "
            f"  echo 'DONE'; "
            f"elif {running}; then "
            f"  echo 'RUNNING'; "
            f"elif [ \"$data_bytes\" -ge \"$min_bytes\" ] && [ \"$json_aborted\" -eq 0 ]; then "
            f"  echo 'RUNNING'; "
            f"else "
            f"  echo 'FAILED'; "
            f"fi"
        )

    def _log_dataset_failure_details(host: str) -> None:
        if executor.is_windows_host(host):
            output_dir_win = normalize_windows_path(config.windows_output_dir)
            output_file = f"{output_dir_win}/write_dataset.json"
            check_cmd = (
                f"powershell -Command \""
                f"if (Test-Path '{output_file}') {{ $f = Get-Item '{output_file}'; "
                f"Write-Host ('json_size=' + $f.Length + ' (0 until FIO finishes)') }} "
                f"else {{ Write-Host 'json=NOT_FOUND' }}; "
                f"if (Get-Process fio -ErrorAction SilentlyContinue) {{ Write-Host 'fio=RUNNING' }} "
                f"else {{ Write-Host 'fio=NOT_RUNNING' }}\""
            )
            ok, out = executor.execute_command(host, check_cmd, "Dataset failure diagnostics", quiet=True, timeout=15)
            if ok and out:
                logger.error(f"{host}: {out.strip()}")
            return

        output_file = f"{config.output_dir}/write_dataset.json"
        data_glob = f"{config.mount_point}/testfile*"
        log_file = (jobs.get(host) or {}).get("log_file")
        log_tail = (
            f"tail -n 40 {log_file}" if log_file else
            "ls -t /tmp/fio_background_*.log 2>/dev/null | head -1 | xargs -r tail -n 40"
        )
        diag_cmd = (
            f"echo -n 'json='; "
            f"if test -f '{output_file}'; then "
            f"  size=$(stat -c '%s' '{output_file}' 2>/dev/null || echo 0); "
            f"  echo \"${{size}} bytes (0 until FIO finishes)\"; "
            f"else echo 'NOT_FOUND'; fi; "
            f"echo -n 'data_files='; ls -1 {data_glob} 2>/dev/null | wc -l; "
            f"echo -n 'fio_procs='; pgrep -ax fio 2>/dev/null || echo 'none'; "
            f"echo '--- fio background log (tail) ---'; "
            f"{log_tail}"
        )
        ok, out = executor.execute_command(host, diag_cmd, "Dataset failure diagnostics", quiet=True, timeout=30)
        if ok and out:
            for line in out.strip().splitlines()[:50]:
                logger.error(f"{host}: {line}")

    def _clear_dataset_json(host: str) -> None:
        """Remove write_dataset.json (including aborted SIGTERM dumps)."""
        if executor.is_windows_host(host):
            output_dir_win = normalize_windows_path(config.windows_output_dir)
            cmd = (
                f"powershell -Command \""
                f"$p='{output_dir_win}/write_dataset.json'; "
                f"if (Test-Path $p) {{ Remove-Item $p -Force }}\""
            )
        else:
            cmd = f"rm -f '{config.output_dir.rstrip('/')}/write_dataset.json'"
        executor.execute_command(host, cmd, "Clear write_dataset.json", quiet=True, timeout=15)

    def _recover_or_fail(host: str, reason: str) -> None:
        """Kill stuck job; one-shot restart if attempts remain, else mark FAILED."""
        job = jobs.get(host, {})
        attempt = int(job.get("attempt", 1))
        logger.warning(f"{host}: dataset write recovery triggered ({reason}), attempt {attempt}/{max_attempts}")

        # Race guard: FIO may have finished between status poll and recovery.
        recheck_cmd = _dataset_status_cmd(host)
        ok, out = executor.execute_command(
            host, recheck_cmd, "Recheck dataset status before recovery", quiet=True, timeout=15
        )
        status = (out or "").strip().splitlines()[-1].strip() if ok and out else ""
        if status == "DONE":
            logger.info(f"{host}: dataset write verified complete — skipping kill/restart")
            completed_hosts.add(host)
            failed_streak.pop(host, None)
            return

        _log_dataset_failure_details(host)
        _kill_dataset_fio_on_host(executor, host, job)
        _clear_dataset_json(host)

        if attempt < max_attempts:
            next_attempt = attempt + 1
            logger.info(f"{host}: one-shot restart of dataset write (attempt {next_attempt}/{max_attempts})")
            _start_host(host, next_attempt)
            failed_streak.pop(host, None)
        else:
            logger.error(f"{host}: dataset write failed after {attempt} attempt(s) ({reason})")
            failed_hosts.add(host)
            failed_streak.pop(host, None)

    while True:
        newly_completed = []
        newly_failed = []
        pending_hosts = [h for h in config.vm_hosts if h not in completed_hosts and h not in failed_hosts]

        if not pending_hosts:
            break

        now = time.time()

        with ThreadPoolExecutor(max_workers=min(len(pending_hosts), config.max_workers)) as pool:
            check_futures = {
                pool.submit(
                    executor.execute_command,
                    host,
                    _dataset_status_cmd(host),
                    "Checking dataset status",
                    quiet=True,
                    timeout=15,
                ): host
                for host in pending_hosts
            }

            status_by_host = {}
            for future in as_completed(check_futures):
                host = check_futures[future]
                try:
                    success, output = future.result()
                except Exception as e:
                    logger.warning(f"Dataset status check error on {host}: {e}")
                    status_by_host[host] = ("", False)
                    continue
                status = (output or "").strip().splitlines()[-1].strip() if success and output else ""
                status_by_host[host] = (status, success)

        running_hosts = [h for h, (st, ok) in status_by_host.items() if st == "RUNNING"]
        if running_hosts:
            with ThreadPoolExecutor(max_workers=min(len(running_hosts), config.max_workers)) as pool:
                prog_futures = {
                    pool.submit(_dataset_progress_bytes, executor, host, config): host
                    for host in running_hosts
                }
                for future in as_completed(prog_futures):
                    host = prog_futures[future]
                    try:
                        nbytes = future.result()
                    except Exception:
                        nbytes = 0
                    job = jobs.get(host)
                    if not job:
                        continue
                    if nbytes > job.get("last_bytes", 0):
                        job["last_bytes"] = nbytes
                        job["last_progress_at"] = now

        recover_hosts = []
        for host in pending_hosts:
            status, success = status_by_host.get(host, ("", False))
            job = jobs.get(host, {})
            attempt_age = now - job.get("attempt_started", start_time)
            stall_age = now - job.get("last_progress_at", start_time)
            expected_bytes = _expected_dataset_data_bytes(config, host)
            data_full = _dataset_files_full_size(int(job.get("last_bytes", 0)), expected_bytes)

            if status == "DONE":
                completed_hosts.add(host)
                newly_completed.append(host)
                failed_streak.pop(host, None)
                continue

            if status == "RUNNING":
                failed_streak.pop(host, None)
                # Dataset write is always size-based: FIO should exit once --size
                # is written. Wait for JSON when data is full; stall-recover only
                # when incomplete and no progress.
                stall_gate = expected_runtime  # always 0 for dataset
                if data_full:
                    if int(attempt_age) % 60 < check_interval:
                        logger.info(
                            f"{host}: dataset files full-size "
                            f"({job.get('last_bytes', 0)} bytes); size-based FIO still running — "
                            f"waiting for write_dataset.json (not killing)"
                        )
                    continue
                if attempt_age > stall_gate and stall_age > stall_limit:
                    recover_hosts.append((
                        host,
                        f"no progress for {int(stall_age)}s > {stall_limit}s "
                        f"(size-based, incomplete) "
                        f"(data incomplete: {job.get('last_bytes', 0)}/{expected_bytes or '?'} bytes)"
                    ))
                continue

            if status == "FAILED":
                # Process gone: if data is already full, give JSON a few polls to appear
                # before declaring failure (FIO may have just exited).
                if data_full:
                    streak = failed_streak.get(host, 0) + 1
                    failed_streak[host] = streak
                    if streak < failed_streak_needed:
                        logger.info(
                            f"{host}: FIO exited with full-size dataset "
                            f"({streak}/{failed_streak_needed}) — waiting for write_dataset.json"
                        )
                        continue
                streak = failed_streak.get(host, 0) + 1
                failed_streak[host] = streak
                if streak >= failed_streak_needed:
                    recover_hosts.append((host, "process gone without write_dataset.json"))
                else:
                    logger.warning(
                        f"{host}: no dataset FIO process detected "
                        f"({streak}/{failed_streak_needed} before recovery) — will recheck"
                    )
                continue

            logger.warning(
                f"Could not determine dataset status on {host} "
                f"(success={success}, status={status!r}); will retry"
            )

        for host, reason in recover_hosts:
            if host in completed_hosts or host in failed_hosts:
                continue
            before_failed = host in failed_hosts
            _recover_or_fail(host, reason)
            if host in failed_hosts and not before_failed:
                newly_failed.append(host)

        if newly_completed:
            logger.info(f"Dataset write completed on: {', '.join(sorted(newly_completed))}")
        if newly_failed:
            logger.error(
                f"Dataset write FAILED on: {', '.join(sorted(newly_failed))} "
                f"(exhausted retries or unrecoverable)"
            )

        elapsed = int(time.time() - start_time)
        remaining_hosts = [h for h in config.vm_hosts if h not in completed_hosts and h not in failed_hosts]
        logger.info(
            f"Waiting for FIO dataset writing... "
            f"({len(remaining_hosts)} hosts remaining"
            f"{(': ' + ', '.join(sorted(remaining_hosts))) if remaining_hosts else ''}, "
            f"{len(completed_hosts)}/{total_hosts} completed, "
            f"{len(failed_hosts)} failed, {elapsed}s elapsed)"
        )

        if not remaining_hosts:
            break

        time.sleep(check_interval)

    elapsed = int(time.time() - start_time)
    logger.info(
        f"Dataset writing finished: {len(completed_hosts)}/{total_hosts} succeeded, "
        f"{len(failed_hosts)} failed, {elapsed}s elapsed"
    )

    if failed_hosts:
        logger.error(
            f"FIO dataset write failed on {len(failed_hosts)} host(s): "
            f"{', '.join(sorted(failed_hosts))}"
        )
        logger.error("Cannot continue tests without a valid dataset on all hosts")
        sys.exit(1)

    logger.info("Test dataset writing completed")



