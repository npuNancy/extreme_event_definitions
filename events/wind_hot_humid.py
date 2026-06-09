"""风电 · 高温高湿 hot_humid
判定: temp_C > 30℃  且  rh_pct >= 85%
标定: 真实场站 暴露0.02% 加权损失方向69.4% 净损失50.4%
"""
TECH, NAME, LABEL = "wind", "hot_humid", "高温高湿"

# ====== 阈值(改这里) ======
TEMP_MIN_C = 30.0    # temp_C >  此值
RH_MIN_PCT = 85.0    # rh_pct >= 此值


def signal(w):
    return (w["temp_C"] > TEMP_MIN_C) & (w["rh_pct"] >= RH_MIN_PCT)


EXPR = f"temp_C > {TEMP_MIN_C} & rh_pct >= {RH_MIN_PCT}"
