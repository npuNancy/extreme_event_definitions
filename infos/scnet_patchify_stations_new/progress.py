#!/usr/bin/env python3
"""Initialize the runtime ledger or render the compact Climate × Station table."""
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from grid_extreme_signals import station_contract as ct
from infos.scnet_patchify_stations_new.run_job import load_pack


def initial_ledger(pack):
    return {"campaign_identity": pack["campaign_identity"], "preparation": {"status": "not_started"},
            "tasks": {r["task_id"]: {"classification": "not_submitted", "unit_id": r["unit_id"],
                      "logical_owner": r["logical_owner"], "username": r["logical_owner"],
                      "job_id": None, "assignment_version": 0, "attempt": 0,
                      "pack_identity": pack["identity"], "resource_profile": pack["resource_profile"], "history": []}
                      for r in pack["jobs"]}}


def render(pack=None, ledger=None):
    counts = {(m, c, s): 0 for m in ct.MODELS for c in ct.SCENARIOS for s in ct.SCENARIOS}
    stages = {"prepare": 0, "publish": 0}
    ready = False
    if pack is not None and ledger is not None:
        if ledger["campaign_identity"] != pack["campaign_identity"]:
            raise ValueError("ledger campaign mismatch")
        if set(ledger["tasks"]) != {r["task_id"] for r in pack["jobs"]}:
            raise ValueError("incomplete ledger inventory")
        ready = ledger.get("preparation", {}).get("status") == "verified"
        for row in pack["jobs"]:
            state = ledger["tasks"][row["task_id"]]
            if state["classification"] == "succeeded":
                if not state.get("verified_at") or not state.get("job_id"):
                    raise ValueError("success requires recorded scheduler/receipt verification")
                if row["stage"] == "extract":
                    counts[(row["model"], row["climate_scenario"], row["station_scenario"])] += 1
                else:
                    stages[row["stage"]] += 1
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    lines = [f"Last checked: `{stamp} Asia/Shanghai`", "",
             "| 阶段/模型 | Climate | Station ssp126 | Station ssp245 | Station ssp585 |",
             "|---|---|---|---|---|",
             f"| 运行前准备 | — | {'已验证 ✅' if ready else '未完成'} | — | — |",
             f"| prepare | — | {stages['prepare']}/1{' ✅' if stages['prepare'] else ''} | — | — |"]
    for m in ct.MODELS:
        for c in ct.SCENARIOS:
            cells = [f"{counts[m,c,s]}/94" + (" ✅" if counts[m,c,s] == 94 else "") for s in ct.SCENARIOS]
            lines.append("| " + " | ".join([m, c, *cells]) + " |")
    lines.append(f"| publish | — | {stages['publish']}/1{' ✅' if stages['publish'] else ''} | — | — |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pack")
    p.add_argument("--ledger")
    p.add_argument("--initialize", action="store_true", help="Create an absent ledger; never overwrite")
    p.add_argument("--output", required=True)
    args = p.parse_args(argv)
    if bool(args.pack) != bool(args.ledger) or (args.initialize and not args.pack):
        p.error("pack and ledger must be provided together; initialization needs both")
    pack = load_pack(args.pack) if args.pack else None
    if args.initialize:
        with ct.lock(str(args.ledger) + ".init.lock"):
            if Path(args.ledger).exists():
                raise FileExistsError("ledger exists; resume it")
            ct.atomic_json(args.ledger, initial_ledger(pack))
    ledger = ct.read_json(args.ledger) if args.ledger else None
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = ct.temporary(output)
    try:
        tmp.write_text(render(pack, ledger), encoding="utf-8")
        os.replace(tmp, output)
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
