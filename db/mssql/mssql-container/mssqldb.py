#!/usr/bin/env python3
"""Thin shim — prefer: python3 -m mssql_tests"""
from mssql_tests.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
