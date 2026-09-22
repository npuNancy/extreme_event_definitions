#!/usr/bin/env python3
"""Prepare native-grid resource climatology and P5 thresholds."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grid_extreme_signals.grid_compute import main

if __name__ == "__main__":
    main("baseline")
