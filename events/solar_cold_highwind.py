"""光伏 · 低温大风 cold_highwind
判定: temp_C < 5℃  且  wind_ms > 8 m/s
标定: 真实场站 暴露0.05% 加权损失方向86.5% 净损失38.7%
"""
TECH, NAME, LABEL = "solar", "cold_highwind", "低温大风"

# ====== 阈值(改这里) ======
TEMP_MAX_C = 5.0     # temp_C <  此值
WIND_MIN_MS = 8.0    # wind_ms > 此值


def signal(w):
    return (w["temp_C"] < TEMP_MAX_C) & (w["wind_ms"] > WIND_MIN_MS)


EXPR = f"temp_C < {TEMP_MAX_C} & wind_ms > {WIND_MIN_MS}"
