#!/usr/bin/env python
"""Plot the raw-ERA5-Land benchmark: end-to-end time is dominated by fixed bbox I/O."""
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# (K, select+build, simple, lowres_w, lowres_s, K-part) milliseconds  [China bbox, 1 month]
ROWS = [
    (10,      0.7,  0.04,    4.8,    5.8,    11.4),
    (100,     4.4,  0.10,   14.3,   17.3,    36.2),
    (1000,   46.7,  0.62,  119.8,  148.1,   315.2),
    (5000,  286.2,  2.82,  541.6,  689.4,  1519.9),
    (10000, 597.7,  5.80, 1109.6, 1413.9,  3127.0),
    (20000,1247.2, 12.28, 2271.7, 2918.2,  6449.4),
]
IO_COLD = 112.64   # s, cold read (first run)
IO_WARM = 74.57    # s, warm cache (page-cached re-read)

K = np.array([r[0] for r in ROWS], float)
kpart = np.array([r[5] for r in ROWS]) / 1000.0           # s
build = np.array([r[1] for r in ROWS])
loww = np.array([r[3] for r in ROWS])
lows = np.array([r[4] for r in ROWS])

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))

# Panel A: end-to-end (cold I/O floor + K-part) -- shows I/O dominance
ax1.axhline(IO_COLD, color="firebrick", ls="--", lw=1.5, label=f"fixed bbox I/O (cold) = {IO_COLD:.0f}s")
ax1.plot(K, IO_COLD + kpart, marker="o", color="firebrick", label="end-to-end = I/O + compute")
ax1.fill_between(K, IO_COLD, IO_COLD + kpart, color="orange", alpha=0.35, label="K-dependent compute")
ax1.set_xscale("log")
ax1.set_xlabel("number of stations  K")
ax1.set_ylabel("end-to-end time from raw (s)")
ax1.set_title("from raw ERA5-Land (China bbox, 1 month): I/O dominates")
ax1.set_ylim(0, IO_COLD + 12)
ax1.grid(True, which="both", alpha=0.3)
ax1.legend(fontsize=9, loc="center left")
for k, e in zip(K, IO_COLD + kpart):
    ax1.annotate(f"+{(e-IO_COLD):.1f}s", (k, e), textcoords="offset points",
                 xytext=(0, 7), ha="center", fontsize=8)

# Panel B: the K-dependent compute breakdown (log-log) -- linear in K
ax2.loglog(K, build, marker="o", label="select+build weather")
ax2.loglog(K, loww, marker="o", label="wind_low_resource")
ax2.loglog(K, lows, marker="o", label="solar_low_resource")
ax2.loglog(K, K / K[0] * build[0], color="gray", ls=":", label="linear ref (slope 1)")
ax2.set_xlabel("number of stations  K")
ax2.set_ylabel("compute time (ms)")
ax2.set_title("K-dependent part only (compute) — ~linear in K")
ax2.grid(True, which="both", alpha=0.3)
ax2.legend(fontsize=9)

fig.suptitle("eed efficiency FROM RAW: station count vs time  (Spark02, global_era)")
fig.tight_layout()
out = "/data1/luobaozhen/extreme_event_definitions/bench/bench_raw_scaling.png"
fig.savefig(out, dpi=130)
print("SAVED", out)
