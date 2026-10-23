# Model fixture provenance

`bts_sample.csv` is a tiny, hand-authored synthetic dataset shaped like selected BTS
flight fields. It is not downloaded from BTS and does not represent measured airline
performance. It exists only to exercise the deterministic training, temporal split,
calibration, metrics, response contract, and JSON serialization paths.

Metrics produced from this fixture are software-path checks, not model-quality claims.
The validation metrics are in-sample calibration-fit diagnostics; only the November–
December holdout metrics are out-of-sample within this synthetic fixture. Neither is
evidence of real-world calibration.
