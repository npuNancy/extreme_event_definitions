"""光伏 · 结冰 icing
判定: temp_C < -2℃  且  rh_pct >= 85%
标定: 真实场站 暴露0.32% 加权损失方向90.0% 净损失66.9%
注意: 风电/光伏结冰统一用 -2℃(同名事件同阈值, 便于交代)。光伏尤其需要更冷阈值 ——
      0℃附近低温常伴晴空, 光伏反而增益(temp<0 net -0.044); 净损失转折点在 -1℃,
      取 -2℃ 为更纯净的真结冰损失(net 0.669)。
"""
TECH, NAME, LABEL = "solar", "icing", "结冰"

# ====== 阈值(改这里) ======
TEMP_MAX_C = -2.0    # temp_C <  此值 (光伏需更冷; -1=转折点, -2=更纯净)
RH_MIN_PCT = 85.0    # rh_pct >= 此值


def signal(w):
    """w: {变量名:(T,K) ndarray}; 返回 (T,K) bool 掩码。NaN 处为 False。"""
    return (w["temp_C"] < TEMP_MAX_C) & (w["rh_pct"] >= RH_MIN_PCT)


EXPR = f"temp_C < {TEMP_MAX_C} & rh_pct >= {RH_MIN_PCT}"
