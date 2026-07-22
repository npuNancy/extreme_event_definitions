#!/usr/bin/env python3
"""本地监控并补充提交 step2 E2 作业。"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from infos.hpc_step2_monitor_common import build_parser, run_monitor  # noqa: E402

STATE_DIR = _PROJECT_ROOT / "infos/hpc_step2_E2/completion_status"


def main() -> None:
    run_monitor("E2", STATE_DIR, build_parser("E2").parse_args())


if __name__ == "__main__":
    main()
