# Patchify extreme-event SCNet goal

One unit is `model × scenario × patch × tech`. The patchify entry point runs
ordinary and low-resource events in one job, computes the 2015–2024 clim288/P5
cache once, and writes station-only signals. Generators never submit jobs.
