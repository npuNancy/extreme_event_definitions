"""风电 · 大风 high_wind
判定: wind_ms > 17 m/s   (10m 口径)
标定: 真实场站 暴露~0.0003% 加权损失方向100% 净损失100% (极罕见)
注意: 风机切出损失发生在轮毂高度(~100m ≈ 25 m/s); 10m 口径无法体现切出，
      如需评估切出请改用轮毂高度风速并把阈值改到 ~25。
"""
TECH, NAME, LABEL = "wind", "high_wind", "大风"

# ====== 阈值(改这里) ======
WIND_MIN_MS = 18.0   # wind_ms > 此值 (10m)


def signal(w):
    return w["wind_ms"] > WIND_MIN_MS


EXPR = f"wind_ms > {WIND_MIN_MS}"
