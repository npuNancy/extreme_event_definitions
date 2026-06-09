"""光伏 · 暴雨 rainstorm
判定: precip_mmh > 2 mm/h
标定: 真实场站 暴露1.59% 加权损失方向93.2% 净损失57.4%
"""
TECH, NAME, LABEL = "solar", "rainstorm", "暴雨"

# ====== 阈值(改这里) ======
PRECIP_MIN_MMH = 2.0   # precip_mmh > 此值


def signal(w):
    return w["precip_mmh"] > PRECIP_MIN_MMH


EXPR = f"precip_mmh > {PRECIP_MIN_MMH}"
