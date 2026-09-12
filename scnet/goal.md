# global_bcsd patchify extreme-event SCNet goal

- Canonical generator: `scnet/create_extreme_patch_jobs.py`; it only writes scripts and a manifest.
- One unit: `model × scenario × patch × tech`, with an explicit active-patch list from global_bcsd.
- Runtime command: `scripts/station_signals_patchify.py`; it checks input sidecars and writes station-only signals.
- Ordinary and low-resource events run together. For each unit, calculate the 24-hour rolling array,
  2015–2024 `clim288`, and station P5 once, then pass them to the low-resource event definition.
- Runtime environment: `source /work/home/acbpgywfpz/miniconda3/bin/activate climate`.
- Job/log directories are external; generation never submits jobs.

