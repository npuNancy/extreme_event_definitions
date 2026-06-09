"""光伏 · 冻雨 freezing_rain
判定: temp_C < 3℃  且  precip_mmh > 0
标定: 真实场站 暴露4.43% 加权损失方向88.1% 净损失38.9%
"""
TECH, NAME, LABEL = "solar", "freezing_rain", "冻雨"

# ====== 阈值(改这里) ======
TEMP_MAX_C = 3.0       # temp_C <  此值
PRECIP_MIN_MMH = 0.0   # precip_mmh > 此值


def signal(w):
    return (w["temp_C"] < TEMP_MAX_C) & (w["precip_mmh"] > PRECIP_MIN_MMH)


EXPR = f"temp_C < {TEMP_MAX_C} & precip_mmh > {PRECIP_MIN_MMH}"
