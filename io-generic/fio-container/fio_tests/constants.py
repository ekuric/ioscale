"""Shared timeouts, defaults, and OS-disk (--testdir) path constants."""

DEFAULT_TIMEOUT = 300           # General SSH command timeout in seconds
QUICK_TIMEOUT = 60              # Short commands (mkdir, package check, etc.)
PROCESS_CHECK_TIMEOUT = 30      # Checking if a remote process is still running
CONNECTIVITY_TIMEOUT = 10       # Initial SSH connectivity test per host
RUNTIME_BUFFER = 300            # Extra seconds added to FIO runtime for test timeout
NOHUP_SETUP_TIMEOUT = 60       # Setting up nohup background FIO on remote host
SCP_TIMEOUT = 300               # File copy (scp/virtctl scp) timeout
DATASET_WRITE_BUFFER = 60      # Extra seconds for FIO dataset pre-write to finish
DATASET_STALL_SECONDS = 600     # No dataset byte growth for this long => treat as hung
DATASET_WRITE_RETRIES = 1       # One-shot restart of dataset write after kill (per host)
CHECK_INTERVAL = 10             # Polling interval when waiting for background tasks
MIGRATION_TIMEOUT = 600         # VM live migration timeout per host
DEFAULT_MAX_WORKERS = 50        # Default thread pool max workers
VM_RESTART_WAIT = 300           # Seconds to wait for VM restart after virtctl restart (5 min; many VMs need longer)
UNREACHABLE_GRACE_WAIT = 180    # Wait/retry this long before virtctl restart (prep + FIO tests)
WINDOWS_PREP_RESTART_AFTER_ATTEMPT = 5  # Windows prep: restart VM after this many failed attempts
DEFAULT_LINUX_IOENGINE = "libaio"
LINUX_THREAD_IOENGINES = frozenset({"libaio", "io_uring", "posixaio"})

# Fixed paths for --testdir (OS-disk FIO mode; no separate data disk format/provision)
LINUX_TESTDIR = "/root/testdir"
WINDOWS_TESTDIR = "c:/testdir"
WINDOWS_FIO_DIR_TESTDIR = "c:/tools/fio"
WINDOWS_OUTPUT_TESTDIR = "c:/fio-results"
