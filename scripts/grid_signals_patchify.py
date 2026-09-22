#!/usr/bin/env python3
"""Compute native-grid extreme events from BCSD finals and a prepared baseline."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grid_extreme_signals.grid_compute import main

if __name__ == "__main__":
    main("signals")
