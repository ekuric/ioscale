#!/usr/bin/env python3
"""Thin shim — prefer: python3 -m postgresql_tests"""
from postgresql_tests.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
