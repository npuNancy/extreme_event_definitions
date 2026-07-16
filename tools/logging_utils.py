"""项目统一日志配置工具。"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path


def _safe_name(name: str) -> str:
    """把入口名转成适合文件名的形式。"""
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)


def setup_logging(
    name: str | None = None,
    *,
    log_dir: str | Path = "logs",
    level: int = logging.INFO,
) -> Path:
    """初始化控制台和文件日志，并返回本次运行的日志文件路径。"""
    root = logging.getLogger()
    existing = getattr(root, "_eed_log_file", None)
    if existing:
        return Path(existing)

    entry = name or Path(sys.argv[0]).stem or "run"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = Path(log_dir) / f"{_safe_name(entry)}_{timestamp}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(console_handler)
    root.addHandler(file_handler)
    root._eed_log_file = str(log_path)  # type: ignore[attr-defined]
    logging.getLogger(__name__).info("日志文件：%s", log_path)
    return log_path


def setup_entry_logging(name: str) -> Path | None:
    """入口脚本使用：查看帮助时不创建日志文件。"""
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        return None
    return setup_logging(name)
