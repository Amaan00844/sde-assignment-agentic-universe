#!/usr/bin/env python3
"""
Convenience entry point for the pipeline.
Usage:
    python run.py ingest data/drop_1
    python run.py ingest data/drop_2
    python run.py report --out reports/
"""
import sys
from solution.cli import main

if __name__ == "__main__":
    main()
