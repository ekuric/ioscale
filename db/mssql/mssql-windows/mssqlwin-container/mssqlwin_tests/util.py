"""Small helpers for MSSQL Windows tests."""
import base64
import re

def get_vm_number(hostname: str) -> str:
    """Extract VM number from hostname"""
    match = re.search(r'vm-(\d+)', hostname)
    if match:
        return match.group(1)
    match = re.search(r'(\d+)', hostname)
    if match:
        return match.group(1)
    return "1"


def build_powershell_command(command: str) -> str:
    """Wrap a PowerShell command for SSH execution"""
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    return f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"
