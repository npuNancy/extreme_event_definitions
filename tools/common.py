"""低资源(low_resource)共享算法。被 wind_low_resource.py / solar_low_resource.py 复用。

严格复刻权威实现 compute_low_resource.py：
  R_roll(t)  = 24h 居中滚动均值 (t-12 .. t+11);  窗口不完整 -> NaN
  Anomaly(t) = R_roll(t) - clim288(month,hour)   [clim 用基线期]
  ev(t)      = Anomaly(t) <= 每站 P{pct}
  signal     = 标记 t 与 t+1 (ev2[1:] |= ev[:-1]); 不完整窗口/NaN -> 0
  光伏       : 夜间(太阳高度角<=0)强制 0
"""
from __future__ import annotations
import numpy as np
import pandas as pd

# ====== 参数(改这里) ======
PCT = 5.0                       # 距平百分位 (P5)
HALF_BACK, HALF_FWD = 12, 12    # 小时级数据的居中 24h 窗口


def roll_centered(a, window_steps=24):
    """居中滚动均值 (T,K); 窗口不完整 -> NaN。"""
    win = int(window_steps)
    if win < 1:
        raise ValueError(f"window_steps must be positive, got {window_steps!r}")
    return (pd.DataFrame(a).rolling(win, center=True, min_periods=win)
            .mean().to_numpy().astype(np.float32))


def roll24c(a):
    """24h 居中滚动均值 (T,K); 小时级数据使用 24 个时间步。"""
    return roll_centered(a, HALF_BACK + HALF_FWD)


def clim288(roll, time, base_mask=None):
    """月×时气候态 (12,24,K)。time: DatetimeIndex; base_mask: 基线期(如1987-2016)。"""
    t = pd.DatetimeIndex(time)
    mo = t.month.to_numpy() - 1
    hr = t.hour.to_numpy()
    sel0 = np.ones(len(t), bool) if base_mask is None else np.asarray(base_mask, bool)
    K = roll.shape[1]
    tbl = np.full((12, 24, K), np.nan, np.float32)
    for m in range(12):
        for h in range(24):
            sel = sel0 & (mo == m) & (hr == h)
            if sel.any():
                with np.errstate(all="ignore"):
                    tbl[m, h] = np.nanmean(roll[sel], axis=0)
    return tbl


def solar_elevation(lats, lons, times):
    """太阳高度角(deg)，复刻参考实现，返回 (K,T)。夜间 = elev<=0。"""
    lats = np.asarray(lats, float); lons = np.asarray(lons, float)
    ts = pd.to_datetime(times)
    doy = ts.dayofyear.values.astype(float)
    hutc = ts.hour.values + ts.minute.values / 60.0
    rad = np.deg2rad
    B = rad(360.0 / 365.0 * (doy - 1))
    decl = (0.006918 - 0.399912*np.cos(B) + 0.070257*np.sin(B) - 0.006758*np.cos(2*B)
            + 0.000907*np.sin(2*B) - 0.002697*np.cos(3*B) + 0.00148*np.sin(3*B))
    eot = (0.0000075 + 0.001868*np.cos(B) - 0.032077*np.sin(B) - 0.014615*np.cos(2*B)
           - 0.04089*np.sin(2*B)) * 229.18 / 60.0
    st = hutc[None, :] + lons[:, None] / 15.0 + eot[None, :]
    ha = rad(15.0) * (st - 12.0)
    lr = rad(lats)[:, None]; dr = decl[None, :]
    se = np.sin(lr)*np.sin(dr) + np.cos(lr)*np.cos(dr)*np.cos(ha)
    return np.rad2deg(np.arcsin(np.clip(se, -1, 1))).astype(np.float32)   # (K,T)


def low_resource(resource, time, pct=PCT, base_mask=None, night=None,
                 clim_tbl=None, thr=None, window_steps=None,
                 mark_next_step=True):
    """低资源信号 (T,K) bool。严格复刻参考(含 mark t&t+1)。
    resource : 资源时序 (风=10m风速 wind_ms; 光=辐照 rsds) (T,K)
    time     : DatetimeIndex (T,)
    base_mask: 基线期布尔(T,) 推荐固定 1987-2016 (默认全期)
    night    : 光伏夜间布尔 (T,K) (太阳高度角<=0); 风电传 None
    clim_tbl/thr: 可传外部基线预计算的气候态(12,24,K)/每点阈值(K,)复用；
                  未传时才按 base_mask 在当前 resource 内部计算。"""
    t = pd.DatetimeIndex(time)
    roll = roll24c(resource) if window_steps is None else roll_centered(resource, window_steps)
    if clim_tbl is None:
        clim_tbl = clim288(roll, t, base_mask)
    anom = roll - clim_tbl[t.month.to_numpy() - 1, t.hour.to_numpy()]
    if thr is None:
        ab = anom if base_mask is None else anom[np.asarray(base_mask, bool)]
        with np.errstate(all="ignore"):
            thr = np.nanpercentile(np.where(np.isfinite(ab), ab, np.nan), pct, axis=0)
    ev = anom <= thr[None, :]
    ev2 = ev.copy()
    if mark_next_step:
        ev2[1:] |= ev[:-1]             # mark t AND t+1 (复刻参考)
    sig = ev2.copy()
    sig[~np.isfinite(anom)] = False    # 不完整窗口/NaN -> 无事件
    if night is not None:
        sig &= ~np.asarray(night, bool)
    return sig.astype(bool)
