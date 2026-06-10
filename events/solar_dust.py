"""光伏 · 沙尘 dust
判定: dust_aod > 0.5   (MERRA-2 DUEXTTAU, 逐小时)
标定: 真实场站 暴露0.03% 加权损失方向83.4% 净损失65.7%
注意: 沙尘 AOD 非 AI/物理模型输入特征，模型会严重低估沙尘损失；以真实场站口径为准。
"""

TECH, NAME, LABEL = "solar", "dust", "沙尘"

# ====== 阈值(改这里) ======
DUST_MIN = 0.5  # dust_aod > 此值


def signal(w):
    return w["dust_aod"] > DUST_MIN


EXPR = f"dust_aod > {DUST_MIN}"
