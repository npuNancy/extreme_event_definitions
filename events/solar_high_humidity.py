"""光伏 · 高湿度 high_humidity
判定: rh_pct >= 95%
标定: 真实场站 暴露3.86% 加权损失方向81.3% 净损失32.5%
"""
TECH, NAME, LABEL = "solar", "high_humidity", "高湿度"

# ====== 阈值(改这里) ======
RH_MIN_PCT = 95.0    # rh_pct >= 此值


def signal(w):
    return w["rh_pct"] >= RH_MIN_PCT


EXPR = f"rh_pct >= {RH_MIN_PCT}"
