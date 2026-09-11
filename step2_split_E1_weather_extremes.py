#!/usr/bin/env python3
"""历史兼容入口：请使用 ``step2_complete_extreme_events.py``。"""

from step2_complete_extreme_events import main
from tools.logging_utils import setup_entry_logging


if __name__ == "__main__":
    setup_entry_logging("step2_complete_extreme_events")
    main()
