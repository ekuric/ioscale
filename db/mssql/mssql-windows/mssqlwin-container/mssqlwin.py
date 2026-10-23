#!/usr/bin/env python3
"""Thin shim — prefer: python3 -m mssqlwin_tests"""
from mssqlwin_tests.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
