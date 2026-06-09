"""风电 · 结冰 icing
判定: temp_C < -2℃  且  rh_pct >= 85%
标定: 真实场站 暴露1.17% 加权损失方向86.8% 净损失56.6%
注意: 与光伏结冰统一用 -2℃(同名事件同阈值, 便于交代)。风电 temp<0 也是强损失(net0.509,
      捕获更全 862GWh), 若只看风电可改回 TEMP_MAX_C=0.0; 统一口径下取 -2℃(net0.566)。
"""
TECH, NAME, LABEL = "wind", "icing", "结冰"

# ====== 阈值(改这里) ======
TEMP_MAX_C = -2.0    # temp_C <  此值 (与光伏统一; 单看风电可用 0.0)
RH_MIN_PCT = 85.0    # rh_pct >= 此值


def signal(w):
    """w: {变量名:(T,K) ndarray}; 返回 (T,K) bool 掩码。NaN 处为 False。"""
    return (w["temp_C"] < TEMP_MAX_C) & (w["rh_pct"] >= RH_MIN_PCT)


EXPR = f"temp_C < {TEMP_MAX_C} & rh_pct >= {RH_MIN_PCT}"
