"""Generate non-low-resource extreme-weather signals for global wind/solar sites.

This script is designed for Spark02:
  /data1/luobaozhen/extreme_event_definitions/input/{wind,solar}_all_2024q2_final.gpkg

It keeps the previous inference-output layout style:
  OUT_ROOT/{Country}/{Year}/station_metadata_{tech}[...].csv
  OUT_ROOT/{Country}/{Year}/era5land_extreme_station_{Country}_{Year}_{tech}[...].nc

Only simple threshold events are produced. low_resource is intentionally skipped
because it is handled by the existing low-resource pipeline.
"""
from __future__ import annotations

import argparse
import glob
import math
import os
import re
import sqlite3
import sys
import time as _time
from dataclasses import dataclass

import numpy as np
import pandas as pd
import xarray as xr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import registry
import weather_loaders as wl


ERA5_ROOTS = {
    "u10": "/data1/luobaozhen/era5_download/ERA5_land/global/u10",
    "v10": "/data1/luobaozhen/era5_download/ERA5_land/global/v10",
    "t2m": "/data1/luobaozhen/era5_download/ERA5_land/global/t2m",
    "tp": "/data1/luobaozhen/era5_download/ERA5_land/global/tp",
    # Dew point files and internal variable are both named d2m.
    "d2m": "/data1/luobaozhen/global_wind_pv/era5_download/ERA5_land/global/d2m",
}

REQUIRED = {
    "wind": ["u10", "v10", "t2m", "tp", "d2m"],
    "solar": ["u10", "v10", "t2m", "tp", "d2m"],  # dust is loaded from MERRA-2 separately.
}

WEATHER_ATTRS = {
    "tas": ("K", "2m air temperature"),
    "sfcWind": ("m s-1", "10m wind speed"),
    "pr": ("mm h-1", "hourly precipitation"),
    "hurs": ("%", "2m relative humidity from t2m and d2m"),
    "dod": ("1", "MERRA-2 DUEXTTAU dust optical depth"),
}


def log(*args):
    print(f"[{_time.strftime('%H:%M:%S')}]", *args, flush=True)


def safe_country(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "_", str(name)).strip("_")
    return s or "Unknown"


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def gpkg_layer(path: str) -> str:
    with sqlite3.connect(path) as con:
        row = con.execute(
            "select table_name from gpkg_contents where data_type='features' limit 1"
        ).fetchone()
    if not row:
        raise RuntimeError(f"No feature layer found in {path}")
    return str(row[0])


def gpkg_columns(path: str, layer: str) -> list[str]:
    with sqlite3.connect(path) as con:
        return [r[1] for r in con.execute(f"pragma table_info({qident(layer)})")]


def load_sites(
    gpkg: str,
    countries: set[str] | None = None,
    limit_per_country: int = 0,
) -> pd.DataFrame:
    layer = gpkg_layer(gpkg)
    cols = gpkg_columns(gpkg, layer)
    country_col = "COUNTRY" if "COUNTRY" in cols else "country"
    id_col = "id" if "id" in cols else "ID"
    keep = [id_col, "lon", "lat", country_col]
    for extra in ("tiles", "construction_year", "construction_quarter"):
        if extra in cols:
            keep.append(extra)

    where = ["lon is not null", "lat is not null", f"{qident(country_col)} is not null"]
    params: list[str] = []
    if countries:
        placeholders = ",".join("?" for _ in countries)
        where.append(f"{qident(country_col)} in ({placeholders})")
        params.extend(sorted(countries))

    sql = (
        "select "
        + ", ".join(qident(c) for c in keep)
        + f" from {qident(layer)} where "
        + " and ".join(where)
        + f" order by {qident(country_col)}, {qident(id_col)}"
    )
    with sqlite3.connect(gpkg) as con:
        df = pd.read_sql_query(sql, con, params=params)
    df = df.rename(columns={id_col: "station_id", country_col: "country"})
    df["station_id"] = df["station_id"].astype(str)
    df["country"] = df["country"].astype(str)
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = wl.to_180(pd.to_numeric(df["lon"], errors="coerce"))
    df = df.dropna(subset=["lat", "lon"]).reset_index(drop=True)
    if limit_per_country > 0:
        df = df.groupby("country", group_keys=False).head(limit_per_country).reset_index(drop=True)
    return df


def check_era5(years: list[int], variables: list[str]):
    missing: list[str] = []
    for var in variables:
        root = ERA5_ROOTS[var]
        for y in years:
            for m in range(1, 13):
                fp = os.path.join(root, f"{var}_{y}_{m:02d}.nc")
                if not os.path.exists(fp):
                    missing.append(fp)
    if missing:
        msg = "\n".join(missing[:20])
        extra = "" if len(missing) <= 20 else f"\n... {len(missing) - 20} more"
        raise FileNotFoundError(f"Missing ERA5 files:\n{msg}{extra}")


def deaccum_tp(tp: np.ndarray, time_index: pd.DatetimeIndex) -> np.ndarray:
    inc = wl._deaccum(tp.astype(np.float32), time_index)
    return (inc * wl.TP_M_TO_MM).astype(np.float32)


def align(arr: np.ndarray, t: pd.DatetimeIndex, canon: pd.DatetimeIndex) -> np.ndarray:
    out = np.full((len(canon), arr.shape[1]), np.nan, np.float32)
    pos = canon.get_indexer(pd.DatetimeIndex(t))
    ok = pos >= 0
    out[pos[ok]] = arr[ok]
    return out


@dataclass
class GridSnap:
    grid_lat: np.ndarray
    grid_lon: np.ndarray
    distance_km: np.ndarray


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1 = np.deg2rad(lat1)
    p2 = np.deg2rad(lat2)
    dp = np.deg2rad(lat2 - lat1)
    dl = np.deg2rad(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return (2 * r * np.arcsin(np.sqrt(a))).astype(np.float32)


def sample_era5_var(
    var: str,
    year: int,
    lat: np.ndarray,
    lon180: np.ndarray,
    bbox: tuple[float, float, float, float],
) -> tuple[np.ndarray, pd.DatetimeIndex, GridSnap | None]:
    root = ERA5_ROOTS[var]
    pieces = []
    times = []
    snap: GridSnap | None = None
    lon360 = wl.to_360(lon180)
    for month in range(1, 13):
        fp = os.path.join(root, f"{var}_{year}_{month:02d}.nc")
        d = xr.open_dataset(fp)
        latn = "latitude" if "latitude" in d.coords else "lat"
        lonn = "longitude" if "longitude" in d.coords else "lon"
        tn = "valid_time" if "valid_time" in d.coords else "time"
        vname = var if var in d.data_vars else list(d.data_vars)[0]
        sub = d[vname].sel({latn: slice(bbox[0], bbox[1]), lonn: slice(bbox[2], bbox[3])}).load()
        gla = np.asarray(sub[latn].values)
        glo = np.asarray(sub[lonn].values)
        tt = pd.to_datetime(sub[tn].values)
        arr = sub.transpose(tn, latn, lonn).values.astype(np.float32)
        ila = np.abs(gla[None, :] - lat[:, None]).argmin(axis=1)
        ilo = np.abs(glo[None, :] - lon360[:, None]).argmin(axis=1)
        pieces.append(arr[:, ila, ilo])
        times.append(tt)
        if snap is None:
            glon180 = wl.to_180(glo[ilo])
            snap = GridSnap(
                grid_lat=gla[ila].astype(np.float32),
                grid_lon=glon180.astype(np.float32),
                distance_km=haversine_km(lat, lon180, gla[ila], glon180),
            )
        d.close()
    data = np.concatenate(pieces, axis=0)
    time = pd.DatetimeIndex(np.concatenate([x.values for x in times]))
    if var == "tp":
        data = deaccum_tp(data, time)
    return data, time, snap


def bbox_for(lat: np.ndarray, lon180: np.ndarray, pad: float = 0.5):
    lon360 = wl.to_360(lon180)
    return (
        min(float(np.nanmax(lat)) + pad, 90.0),
        max(float(np.nanmin(lat)) - pad, -90.0),
        max(float(np.nanmin(lon360)) - pad, 0.0),
        min(float(np.nanmax(lon360)) + pad, 360.0),
    )


def build_weather(
    tech: str,
    year: int,
    stations: pd.DataFrame,
) -> tuple[dict[str, np.ndarray], pd.DatetimeIndex, GridSnap]:
    lat = stations["lat"].to_numpy(float)
    lon = wl.to_180(stations["lon"].to_numpy(float))
    canon = pd.date_range(f"{year}-01-01 00:00", f"{year}-12-31 23:00", freq="h")
    bbox = bbox_for(lat, lon)
    raw: dict[str, np.ndarray] = {}
    snap_ref: GridSnap | None = None
    for var in REQUIRED[tech]:
        arr, tt, snap = sample_era5_var(var, year, lat, lon, bbox)
        raw[var] = align(arr, tt, canon)
        if var == "t2m":
            snap_ref = snap
    weather = {
        "tas": raw["t2m"].astype(np.float32),
        "sfcWind": np.sqrt(raw["u10"] ** 2 + raw["v10"] ** 2).astype(np.float32),
        "pr": raw["tp"].astype(np.float32),
        "hurs": wl.magnus_rh(raw["t2m"], raw["d2m"]),
    }
    if tech == "solar":
        weather["dod"] = wl.sample_dust(year, lat, lon, canon).astype(np.float32)
    if snap_ref is None:
        raise RuntimeError("No t2m grid snap was created")
    return weather, canon, snap_ref


def write_metadata(path: str, stations: pd.DataFrame, snap: GridSnap):
    out = pd.DataFrame(
        {
            "station_id": stations["station_id"].astype(str).to_numpy(),
            "lat": stations["lat"].to_numpy(float),
            "lon": stations["lon"].to_numpy(float),
            "tiles": stations["tiles"].astype(str).to_numpy() if "tiles" in stations else "",
            "country": stations["country"].astype(str).to_numpy(),
            "grid_lat": snap.grid_lat,
            "grid_lon": snap.grid_lon,
            "snap_distance_km": snap.distance_km,
            "snap_dropped": np.zeros(len(stations), dtype=np.int8),
        }
    )
    for c in ("construction_year", "construction_quarter"):
        if c in stations.columns:
            out[c] = stations[c].to_numpy()
    out.to_csv(path, index=False)


def write_dataset(
    path: str,
    tech: str,
    country: str,
    year: int,
    stations: pd.DataFrame,
    time: pd.DatetimeIndex,
    weather: dict[str, np.ndarray],
    masks: dict[str, np.ndarray],
):
    data_vars = {}
    for name, arr in weather.items():
        data_vars[name] = (("time", "station"), arr.astype(np.float32))
    for name, mask in masks.items():
        data_vars[f"signal_{name}"] = (("time", "station"), mask.astype(np.int8))

    ds = xr.Dataset(
        data_vars,
        coords={
            "time": time.values,
            "station": np.arange(len(stations), dtype=np.int32),
            "station_id": ("station", stations["station_id"].astype(str).to_numpy()),
            "lat": ("station", stations["lat"].to_numpy(float)),
            "lon": ("station", stations["lon"].to_numpy(float)),
            "country": ("station", stations["country"].astype(str).to_numpy()),
        },
        attrs={
            "title": f"ERA5-Land non-low-resource extreme signals ({country}, {year}, {tech})",
            "source": "ERA5-Land + MERRA-2 DUEXTTAU; thresholds from extreme_event_definitions/events",
            "tech": tech,
            "country": country,
            "year": str(year),
            "events": ",".join(masks.keys()),
            "low_resource": "not included",
            "time": "UTC",
            "interp": "nearest",
        },
    )
    for v, (units, long_name) in WEATHER_ATTRS.items():
        if v in ds:
            ds[v].attrs["units"] = units
            ds[v].attrs["long_name"] = long_name
    for name in masks:
        ds[f"signal_{name}"].attrs["long_name"] = f"{tech} {name} extreme-weather signal"
        ds[f"signal_{name}"].attrs["flag_values"] = "0, 1"
        ds[f"signal_{name}"].attrs["flag_meanings"] = "false true"

    enc = {v: {"zlib": True, "complevel": 4} for v in ds.data_vars}
    for v in weather:
        enc[v]["dtype"] = "float32"
    for name in masks:
        enc[f"signal_{name}"]["dtype"] = "int8"
    tmp = f"{path}.tmp.{os.getpid()}"
    ds.to_netcdf(tmp, encoding=enc)
    ds.close()
    os.replace(tmp, path)


def output_complete(nc_path: str, meta_path: str) -> bool:
    """Return True when an existing output pair is readable and minimally complete."""
    if not (os.path.exists(nc_path) and os.path.exists(meta_path)):
        return False
    try:
        ds = xr.open_dataset(nc_path)
        ok = (
            "time" in ds.sizes
            and "station" in ds.sizes
            and ds.sizes["time"] > 0
            and ds.sizes["station"] > 0
            and "station_id" in ds.coords
        )
        ds.close()
        return bool(ok)
    except Exception:
        return False


def chunk_ranges(n: int, chunk_size: int):
    if chunk_size <= 0 or n <= chunk_size:
        yield 0, n
        return
    for start in range(0, n, chunk_size):
        yield start, min(start + chunk_size, n)


def run(args):
    years = list(range(args.start_year, args.end_year + 1))
    countries = set(args.countries) if args.countries else None
    check_era5(years, REQUIRED[args.tech])
    sites = load_sites(args.gpkg, countries=countries, limit_per_country=args.limit_per_country)
    if sites.empty:
        raise SystemExit("No stations matched.")
    log(f"loaded {len(sites)} {args.tech} stations from {args.gpkg}")
    log("countries:", sites["country"].nunique())

    groups = [(country, sub.reset_index(drop=True)) for country, sub in sites.groupby("country", sort=True)]
    for year in years:
        log(f"=== year {year} ===")
        for country, sub0 in groups:
            country_dir = safe_country(country)
            out_dir = os.path.join(args.out_root, country_dir, str(year))
            os.makedirs(out_dir, exist_ok=True)
            for start, end in chunk_ranges(len(sub0), args.chunk_size):
                sub = sub0.iloc[start:end].reset_index(drop=True)
                suffix = "" if start == 0 and end == len(sub0) else f"_stations_{start}_{end}_of_{len(sub0)}"
                nc = os.path.join(
                    out_dir,
                    f"era5land_extreme_station_{country_dir}_{year}_{args.tech}{suffix}.nc",
                )
                meta = os.path.join(out_dir, f"station_metadata_{args.tech}{suffix}.csv")
                if not args.overwrite and output_complete(nc, meta):
                    log(f"[skip] {country_dir}/{year}/{args.tech}{suffix}")
                    continue
                if not args.overwrite and (os.path.exists(nc) or os.path.exists(meta)):
                    log(f"[redo] incomplete existing output: {country_dir}/{year}/{args.tech}{suffix}")
                log(f"[run] {country_dir}/{year}/{args.tech}{suffix}: {len(sub)} stations")
                if args.dry_run:
                    continue
                weather, time, snap = build_weather(args.tech, year, sub)
                simple_weather = {
                    "temp_C": (weather["tas"] - 273.15).astype(np.float32),
                    "wind_ms": weather["sfcWind"],
                    "precip_mmh": weather["pr"],
                    "rh_pct": weather["hurs"],
                }
                if "dod" in weather:
                    simple_weather["dust_aod"] = weather["dod"]
                masks = registry.simple_signals(args.tech, simple_weather, skip_missing=False)
                write_metadata(meta, sub, snap)
                write_dataset(nc, args.tech, country, year, sub, time, weather, masks)
                frac = {k: round(float(v.mean()), 6) for k, v in masks.items()}
                log(f"[ok] wrote {nc}; event_frac={frac}")


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tech", required=True, choices=["wind", "solar"])
    ap.add_argument("--gpkg", default="", help="Defaults to input/{tech}_all_2024q2_final.gpkg")
    ap.add_argument("--start_year", type=int, default=2017)
    ap.add_argument("--end_year", type=int, default=2024)
    ap.add_argument("--out_root", default="extreme_simple_global_2017_2024")
    ap.add_argument("--countries", nargs="*", default=None, help="Original country names, e.g. Germany 'United States'")
    ap.add_argument("--chunk_size", type=int, default=10000)
    ap.add_argument("--limit_per_country", type=int, default=0, help="Debug/test: keep first N stations per country")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()
    if not args.gpkg:
        args.gpkg = os.path.join(HERE, "input", f"{args.tech}_all_2024q2_final.gpkg")
    return args


if __name__ == "__main__":
    run(parse_args())
