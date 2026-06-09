"""风电 · 高温 high_temp
判定: temp_C > 35℃
标定: 真实场站 暴露0.26% 加权损失方向63.1% 净损失22.1%
注意: 高温对风电是最弱的净损失(仅22%, 远低于低资源80%/结冰51%等)，物理上接近中性、判别力弱且极罕见(暴露0.26%)，列出供参考，按需取用。
"""
TECH, NAME, LABEL = "wind", "high_temp", "高温"

# ====== 阈值(改这里) ======
TEMP_MIN_C = 35.0    # temp_C > 此值


def signal(w):
    return w["temp_C"] > TEMP_MIN_C


EXPR = f"temp_C > {TEMP_MIN_C}"
