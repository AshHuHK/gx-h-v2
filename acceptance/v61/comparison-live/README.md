# Registered live comparison cohort

Candidate `48af72c3861299350047dd3bae25acfe6a0bc1bd` enrolled its static baseline in cohort `comparison-live-48af72c-01`. The static Engine completed six CPU Jobs with six independently reproduced evidence units in 0.484 CPU seconds. Nine of nine conclusion units were supported. There were zero unsupported assertions among nine, zero model turns, zero paid tokens, zero human research interventions and zero adaptive decision opportunities. A fresh-process resume preserved the scientific state and costs. [Blinded enrollment and hashes](enrollment.json) · [Frozen registration](preregistration.json)

Single Lead and multibranch each have one prospectively assigned run with status `NOT RUN` pending a funded model allocation. To enroll those arms after allocation, run from the 48af72c product checkout with its release environment activated. The same operator-private cohort directory contains the original baseline and enforces one enrollment per arm.

```bash
runner=acceptance/v61/adaptive/comparison/run.py
cohort=../v61-validation/comparison-live-48af72c-01
python -B "$runner" live --work "$cohort" --arm single --seconds 900
python -B "$runner" live --work "$cohort" --arm multi --seconds 900
```

The public receipt gives aggregate scores and hashes of the private enrollment records. The previously reported three-arm scripted rehearsal has its own registration. [Scripted rehearsal](../adaptive/comparison/README.md)
