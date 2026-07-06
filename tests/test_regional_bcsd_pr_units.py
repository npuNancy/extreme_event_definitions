"""Regression tests for regional BCSD precipitation units."""
from __future__ import annotations

from argparse import Namespace

import numpy as np
import xarray as xr

from grid_extreme_signals.adapters.regional_bcsd import RegionalBcsdAdapter
from tests.conftest import make_bcsd_dataset


def test_missing_pr_units_default_to_precipitation_flux(tmp_path):
    """BCSD pr_bcsd without units is kg m-2 s-1 / mm s-1, not mm h-1."""
    for var in ("tas", "uas", "vas", "rsds", "pr"):
        make_bcsd_dataset(tmp_path, var=var)

    pr_path = (
        tmp_path
        / "bcsd_outputs"
        / "TEST-MODEL"
        / "TestRegion"
        / "TEST-MODEL"
        / "pr_3h_bcsd_on_0p1deg_TestRegion_TEST-MODEL_ssp126_2015-2016.nc"
    )
    ds = xr.open_dataset(pr_path)
    original = ds["pr_bcsd"].values.copy()
    ds["pr_bcsd"].attrs.pop("units", None)
    ds.load()
    ds.to_netcdf(pr_path)
    ds.close()

    args = Namespace(
        data_dir=str(tmp_path / "bcsd_outputs"),
        model="TEST-MODEL",
        region="TestRegion",
        scenario="ssp126",
        years="2015-2016",
        output_root=str(tmp_path / "out"),
        allow_unit_inference=False,
        allow_missing_optional=False,
    )
    adapter = RegionalBcsdAdapter(args)
    bundle = adapter.load_solar_weather(adapter.iter_tasks(args)[0])

    np.testing.assert_allclose(
        bundle.dataset["precip_mmh"].values,
        original * 3600.0,
        rtol=1e-6,
        atol=1e-6,
    )
