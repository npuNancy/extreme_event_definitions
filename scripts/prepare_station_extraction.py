#!/usr/bin/env python3
"""Build frozen catalogs and grid-to-station maps on a compute node."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grid_extreme_signals import station_contract as ct
from grid_extreme_signals.station_pipeline import prepare


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign", required=True)
    p.add_argument("--shared-root", required=True)
    p.add_argument("--code-sha", required=True)
    a = p.parse_args(argv)
    result = prepare(ct.read_json(a.campaign), a.shared_root, a.code_sha)
    print(result["identity"])


if __name__ == "__main__":
    main()
