"""Path and FIO option helpers."""

import re

from typing import Optional

from fio_tests.constants import DEFAULT_LINUX_IOENGINE, LINUX_THREAD_IOENGINES

def normalize_windows_path(path: str) -> str:
    """
    Normalize Windows path for PowerShell commands.
    Converts paths like 'd\\:/fio/data' or 'd:/fio/data' to 'd:/fio/data'
    Handles both escaped and unescaped backslashes from YAML.
    """
    if not path:
        return path
    
    # Replace any backslashes with forward slashes
    # This handles both 'd\:/fio/data' (from YAML "d\\:/fio/data") and 'd:/fio/data'
    normalized = path.replace('\\', '/')
    
    # Fix the case where we get 'd/:/fio/data' (drive letter followed by /:/)
    # This happens when YAML has "d\\:/fio/data" which becomes "d\:/fio/data"
    # and then replace('\\', '/') gives "d/:/fio/data"
    # We need to convert it to "d:/fio/data"
    normalized = re.sub(r'([a-zA-Z])/:/', r'\1:/', normalized)
    
    return normalized


def windows_fio_directory_arg(path: str) -> str:
    """
    Path for fio.exe --directory= on Windows.

    FIO's option parser treats ':' as a separator, so a drive path must escape
    the colon as backslash-colon, e.g. c\\:\\testdir (not c:/testdir — that
    becomes directory "c").

    Pass this string via ProcessStartInfo.Arguments (not PowerShell & splat),
    otherwise PowerShell can turn \\t into a TAB.
    """
    normalized = normalize_windows_path(path)  # c:/testdir
    # Native separators for the path after the drive
    native = normalized.replace("/", "\\")  # c:\testdir
    if len(native) >= 2 and native[1] == ":":
        # c:\testdir → c\:\testdir (escape colon for fio option parser)
        return native[0] + "\\:" + native[2:]
    return native


def parse_bool(value, default: bool = False) -> bool:
    """Parse YAML/config booleans from bool, int, or string values."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off", "", "null"):
            return False
    return bool(value)


def linux_fio_uses_threads(ioengine: str) -> bool:
    """True when Linux FIO ioengine needs --thread for parallel numjobs."""
    return ioengine.lower() in LINUX_THREAD_IOENGINES


def normalize_optional_fsync(value) -> Optional[str]:
    """Parse optional FIO fsync interval; None means do not pass --fsync."""
    if value is None or value is False:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "off", "false", "no", "0"):
        return None
    try:
        n = int(s)
    except (TypeError, ValueError) as e:
        raise FioConfigError(f"CRITICAL: fsync must be a positive integer (got {value!r})") from e
    if n < 1:
        return None
    return str(n)


def build_fio_fsync_option(fsync: Optional[str]) -> str:
    """Return '--fsync=N ' for FIO cmdline, or '' when fsync is disabled."""
    if not fsync:
        return ""
    return f"--fsync={fsync} "


def build_linux_fio_thread_option(ioengine: str) -> str:
    """Return '--thread ' for async Linux engines, else empty string."""
    return "--thread " if linux_fio_uses_threads(ioengine) else ""


def parse_optional_runtime(value) -> Optional[int]:
    """
    Parse FIO performance-test runtime in seconds.

    Returns None when runtime is omitted/empty/null/non-positive — tests then
    run size-based (complete --size and exit; no --time_based). Dataset
    pre-write always ignores runtime and is size-based.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in ("null", "none", "false"):
            return None
        try:
            value = int(text)
        except ValueError:
            return None
    try:
        secs = int(value)
    except (TypeError, ValueError):
        return None
    return secs if secs > 0 else None


def fio_runtime_flags(runtime) -> str:
    """Return '--runtime=N --time_based=1 ' for FIO tests, or '' when omitted.

    Used only for performance tests. Dataset pre-write never includes these flags.
    """
    secs = parse_optional_runtime(runtime)
    if secs is None:
        return ""
    return f"--runtime={secs} --time_based=1 "


