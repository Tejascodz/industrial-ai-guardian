# Benchmark

How the evaluation is built, what every number in the README means, and the
results that did not fit there.

## Plant and data

- **Plant.** Tennessee Eastman process (Downs & Vogel, 1993) under Ricker's
  decentralised control (1996), using the public-domain NIST C++ port. The
  driver in `sim/te_cli.cpp` adds:
  - scheduled disturbances and setpoint moves
  - a transmitter-fault layer between plant and controller, so the
    controller acts on the faulty value as it would in a real plant
  - replay and actuator attacks
  - logging of measured and true values, which is the ground truth
  - a live mode used by the dashboard
- **Historian.** Data are logged every 3 minutes. Analyzers keep their
  native sampling and dead time: 6 or 15 minutes.
- **Normal operation.** Operators move production rate, product quality and
  reactor level setpoints inside a normal envelope. Normal is not a single
  steady state.

## Events

| Class | What is simulated |
|---|---|
| Transmitter fault | Bias, drift, stuck and noise on all 22 continuous transmitters, sized in engineering units: 0.5 to 1.5 °C on temperatures, 2 to 6 % on levels, 0.5 to 1.5 % on pressures, 1 to 3 % of reading on flows. Drifts reach 2 to 4 times those sizes after 8 hours. |
| Process fault | IDV 1, 2, 4, 5, 7, 8, 10 to 14 and 16 to 20. IDV 3, 9 and 15 are excluded because they are known to be unobservable; IDV 6 trips the plant under this control strategy. |
| Regime change | Setpoint moves outside the envelope: production rate, product quality, reactor pressure, reactor temperature. |
| Integrity attack | Replay of transmitters with a hidden valve attack (5 variants), replay of all transmitters, the same with attacker-added noise, simultaneous freeze of 3 to 5 transmitters, and a stealthy coordinated bias along the main correlation of normal data. |

## Splits

| Split | Runs | Used for |
|---|---|---|
| train | 30 normal runs, 48 h each | fitting every model |
| val | 140 runs (12 normal + events) | residual scaling, alarm limits, every design choice |
| test | 288 runs (24 normal, 176 transmitter, 64 process, 24 regime) | the reported results |
| val_attack / test_attack | 14 / 42 runs | attack evaluation only; no limit is calibrated on attacks |

Every split uses its own random seeds. The first test split (seed base
50000) was run once during development. It exposed a threshold-generalisation
problem (one extreme quantile per channel), which led to family-wise
calibration. Because that split had influenced a design choice, it was
retired. The reported results come from a fresh test split (seed base 60000
for events, 90000 for attacks) generated after every design choice was
frozen and run once. No limit or diagnosis rule was tuned on it.

On the retired split Guardian detected 93.6 % of events; on the fresh one,
94.3 %. The baselines moved by a similar amount in both directions.

## Scoring

- **Alarm.** Any channel above its limit for 3 consecutive samples (9 min).
- **Calibration.** For each method, the limits are set from validation normal
  data so that all methods run at the same false-alarm rate: 0.25 per day on
  validation. Limits are shared within families of similar channels, which
  generalises better than one extreme quantile per channel.
- **False alarms.** Alarm onsets during normal operation, per day. This
  covers whole normal runs plus the pre-event part of event runs, which
  includes legitimate setpoint moves.
- **Detection and delay.** The first alarm that *starts* at or after the
  event. An alarm already standing when the event begins is a false alarm and
  does not count as a detection.
- **Diagnosis.** The class (and, for transmitter faults, the tag) at
  detection and one hour later. The README reports the one-hour value.
- **Plant trips.** Several faults (stuck level transmitters, some attacks)
  trip the plant's own interlocks. "Warned" means the alarm came before the
  trip.

## Results not in the README

### Operating curve

Detection and diagnosis at several false-alarm budgets
(`results/operating_curve.csv`). At about 0.1 false alarms per day on
test, Guardian detects 92 to 93 % of events; the baselines detect 47 to 55 %.

### Ablations (test split, diagnosis one hour after detection)

| Variant | Detected | Correct class | Named |
|---|---|---|---|
| Guardian | 94 % | 82 % | 79 % |
| without instrument checks (flatline / noise) | 89 % | 61 % | 46 % |
| without setpoint context from the DCS | 92 % | 75 % | 79 % |
| static bank (no lagged inputs) | 95 % | 81 % | 78 % |
| without integrity layer | 94 % | 82 % | 79 % |

- **Instrument checks and DCS setpoint context both matter.** Without
  setpoint context, only 5 % of regime changes are recognised as regime
  changes.
- **The integrity layer costs nothing on faults here.** None of the 264
  test events was reported as an integrity attack. (On the retired split, 2
  reactor-level noise faults that freeze several loops at once were.)
- **Lagged inputs did not help on this benchmark.** The static bank is the
  simpler choice; the dynamic one stays the default because it was chosen
  before testing, and changing it after seeing test results would be tuning
  on the test set.

### By fault type

How often the faulty transmitter is named correctly, Guardian vs PCA + RBC:

| Fault type | Guardian | PCA + RBC |
|---|---|---|
| Stuck | 98 % | 0 % |
| Noise | 86 % | 5 % |
| Bias | 64 % | 16 % |
| Drift | 59 % | 41 % |

### Attacks by type (Guardian)

| Attack | Median delay | Recognised as an attack |
|---|---|---|
| Replay + hidden valve attack (5 variants) | 9 to 18 min | 100 % |
| Replay of all 22 transmitters | 6 min | 100 % |
| Same replays with attacker noise | 6 to 21 min | 0 %; reported as a process or sensor fault |
| Freeze of 3 to 5 transmitters | 18 min | 100 % |
| Stealthy coordinated bias | 48 min | 0 %; reported as a sensor or process fault |

## Reproducibility

The simulator is deterministic: the same seeds give byte-identical plant data
on every machine. The monitoring results can differ by one or two runs per
method across platforms and library versions, because floating-point
differences shift alarm limits slightly. The conclusions are unchanged.

This cross-machine check was run on the retired test split, before the fresh
one existed:

| | Reference run (Linux) | Reproduction (macOS, Apple Silicon) |
|---|---|---|
| Guardian: detected / correct class / named | 93.6 % / 79.4 % / 77.2 % | 93.6 % / 80.2 % / 77.8 % |
| Limit checks: detected | 50.4 % | 51.1 % |
| PCA: detected | 56.8 % | 55.3 % |
| Attack results | identical | identical |

Both runs used Python 3.13.
