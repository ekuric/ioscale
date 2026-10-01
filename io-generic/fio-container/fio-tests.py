#!/usr/bin/env python3
"""FIO Remote Testing Script — CLI shim.

Implementation lives in the fio_tests package. Kept as fio-tests.py so
entrypoint.sh, Dockerfiles, and docs keep working unchanged.
"""

from fio_tests.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
