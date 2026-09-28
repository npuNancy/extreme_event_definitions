#!/usr/bin/env python3
"""Extract existing grid events using a frozen prepared manifest."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grid_extreme_signals import station_contract as ct
from grid_extreme_signals.station_pipeline import extract_combination


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prepared", required=True)
    for name in ("model", "scenario", "tech", "patch", "output-root", "code-sha"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--years", help="Exact existing source shard YYYY-YYYY; omit for all shards")
    a = p.parse_args(argv)
    key = "/".join(ct.safe_name(v) for v in (a.model, a.scenario, a.patch, a.tech))
    result = extract_combination(ct.read_json(a.prepared), key, a.output_root, a.code_sha, years=a.years)
    print(result["status"])


if __name__ == "__main__":
    main()
