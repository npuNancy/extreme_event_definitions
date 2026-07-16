"""Tests for station filter reserved behaviour (--stations_dir guard)."""
from __future__ import annotations

import pytest
from argparse import Namespace

from grid_extreme_signals.signal_runner import run_signal_pipeline


class _DummyAdapter:
    """Minimal adapter that does nothing."""
    def iter_tasks(self, a):
        return []


class TestStationsDirReserved:
    def test_none_is_ok(self):
        """Default stations_dir=None should not raise."""
        args = Namespace(stations_dir=None)
        # iter_tasks returns [] so pipeline completes immediately
        run_signal_pipeline(_DummyAdapter(), args)

    def test_nonempty_raises_not_implemented(self):
        """Any non-None value must raise NotImplementedError."""
        args = Namespace(stations_dir="/data/stations")
        with pytest.raises(NotImplementedError, match="场站筛选模式"):
            run_signal_pipeline(_DummyAdapter(), args)

    def test_empty_string_raises(self):
        """Even an empty string path should raise (it's not None)."""
        args = Namespace(stations_dir="")
        with pytest.raises(NotImplementedError):
            run_signal_pipeline(_DummyAdapter(), args)

    def test_error_message_content(self):
        """Error message should mention 'not implemented yet'."""
        args = Namespace(stations_dir="some/path")
        with pytest.raises(NotImplementedError, match="尚未实现"):
            run_signal_pipeline(_DummyAdapter(), args)
