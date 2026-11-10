# Security design

Guardian reads plant data and tells operators what is wrong. An attacker
could try to make it stay silent during a real problem, make it raise false
alarms until operators stop trusting it, or use it as a way into the control
network. This document describes how the design handles that, what is tested,
and what is not covered.

The design takes IEC 62443 (security for industrial automation and control
systems) as its reference. **This is a reference, not a certification.**
Nothing here has been assessed by a certification body.

## Placement: zones and conduits

```
 Level 0-2   field devices, DCS controllers  ─┐  control zone (unchanged)
 Level 2     DCS / historian OPC UA server   ─┤
                     │  conduit: OPC UA, SignAndEncrypt, read-only ruleset,
                     │  ideally through a data diode (one-way)
 Level 3/3.5 Guardian host (monitoring zone) ─┘
                     │  conduit: alarms and reports only, outbound
 Level 3/4   operators, engineers, SIEM / audit storage
```

Guardian sits in its own zone, separate from the control zone. It has a
single inbound conduit and never writes back to the control system. This
follows the monitoring side channel of the NAMUR Open Architecture. If
Guardian is compromised, the attacker gains a view of plant data. They gain
no path to the controllers, because the data flow and the server-side
permissions both forbid writes.

## Assets

| Asset | Why it matters |
|---|---|
| Control system (DCS, controllers) | Must never be affected by Guardian |
| Plant data stream | Guardian's decisions are only as good as its input |
| Model artifact and alarm limits | A swapped model can hide faults or execute code |
| Alarm decisions and audit trail | Evidence for operators and incident investigation |
| Keys and certificates | Authenticate the OPC UA link and sign models and logs |

## Threats and controls

| # | Threat (STRIDE) | Control in this repository | Verified by |
|---|---|---|---|
| T1 | Spoofed client reads plant data (S, I) | Certificate-based client identity. The server maps only registered certificates to a user; unknown certificates get no rights | `tests/test_opcua.py::test_server_refuses_unknown_certificates` |
| T2 | Eavesdropping or tampering on the wire (T, I) | OPC UA SignAndEncrypt only (Aes256Sha256RsaPss, Basic256Sha256). A server offering no security is refused | `test_server_refuses_unencrypted_sessions` |
| T3 | Monitor used to write to the plant (E) | The client has no write method, and the server's read-only permission ruleset refuses writes even from a trusted client | `test_server_refuses_writes_even_from_a_trusted_client` |
| T4 | Replayed sensor data hides an attack (T) | Exact-repetition detector on every transmitter (`P:` channels) leads to an integrity alarm | `results/attack_summary.csv`, `test_replay_detector_flags_repeated_windows_only` |
| T5 | Several transmitters frozen (lost link or DoS) (D) | Simultaneous-flatline rule on 3 or more transmitters leads to an integrity alarm | `results/attack_summary.csv` |
| T6 | Malformed data (NaN, impossible values, broken timestamps) (T, D) | `guardian/validation.py`: schema, finiteness, span and time checks. Bad samples are masked; pipeline failures raise an integrity alarm | `tests/test_security.py` (validation tests) |
| T7 | Malicious model file (T, E) | No pickle. Arrays load with `allow_pickle=False`; SHA-256 manifest is HMAC-signed and checked before anything is read | `test_artifact_tampering_is_refused`, `test_artifact_signature_required_when_key_given` |
| T8 | Rewriting alarm history (R) | HMAC-SHA256 hash-chained audit log. Any edit, deletion or reordering breaks verification | `test_audit_log_detects_tampering` |
| T9 | Vulnerable dependencies (T, E) | Pinned versions in `requirements.txt`. `pip-audit` reported no known vulnerabilities at release | see "Scans" below |
| T10 | Code-level flaws (E) | `bandit` static analysis. The only subprocess call runs the bundled simulator with fixed arguments and no shell | see "Scans" below |
| T11 | Alarm flooding erodes operator trust (D) | Limits calibrated to a false-alarm budget; 3-sample persistence; dashboard debounces diagnosis changes | `results/benchmark_summary.csv` |
| T12 | Another local user or web page drives the dashboard (S, E) | Binds to 127.0.0.1. Random access token, compared in constant time. WebSocket `Origin` check against cross-site WebSocket hijacking | `tests/test_dashboard.py` |
| T13 | Malformed or abusive commands (T, D) | Strict schema (pydantic, no extra fields, ranges), plant-side limit checks, 4 KB message cap, per-connection rate limit | `tests/test_dashboard.py` |
| T14 | Script injection into the operator's browser (T, E) | Content-Security-Policy `script-src 'self'`, no inline script, no third-party CDN (uPlot and fonts are vendored), server strings inserted with `textContent` only, `frame-ancestors 'none'` | `tests/test_dashboard.py::test_page_has_strict_security_headers` |

### Mapping to IEC 62443-3-3 foundational requirements

| Foundational requirement | Where it is addressed |
|---|---|
| FR1 Identification and authentication control | T1: X.509 application certificates on both sides of the OPC UA link |
| FR2 Use control | T3: read-only permission ruleset, least privilege for the monitoring identity |
| FR3 System integrity | T4 to T7: replay, freeze and validation checks; signed model artifacts |
| FR4 Data confidentiality | T2: encrypted OPC UA sessions; private keys written with 0600 permissions |
| FR5 Restricted data flow | One-way architecture, no write path; data diode recommended |
| FR6 Timely response to events | Integrity alarms within minutes (attack results below), audit trail for investigation (T8) |
| FR7 Resource availability | Freeze/DoS detection (T5). Guardian failure has no effect on control, because it is not in the control path |

## Attack results

There are 42 test runs across 14 attack types, 3 seeds each. All methods use
the thresholds calibrated on normal data (`results/attack_summary.csv`).

- **Replay (Stuxnet-style).** The attacker records 2 hours of transmitter
  readings and plays them back to the DCS. At the same time a real valve is
  moved while the DCS logs the controller's command.
- **"Jitter" variants.** The attacker adds small noise so the replay never
  repeats exactly, to defeat a repetition check.
- **Multi-freeze.** 3 to 5 transmitters stop updating at the same moment.
- **Stealthy coordinated bias.** Several transmitters are pushed together
  along the main correlation of normal data, so a correlation-based monitor
  sees a plausible operating move.

| | Limit checks | PCA + RBC | Guardian |
|---|---|---|---|
| Attacks detected | 88 % | 98 % | **100 %** |
| Recognised as an integrity attack | 0 % (no such class) | 0 % (no such class) | **50 %** |
| Plant trips warned in advance | 17 / 20 | 19 / 20 | **20 / 20** |

| Attack type | Guardian: median delay | Recognised as attack |
|---|---|---|
| Replay + valve attack (5 variants) | 6 to 18 min | 100 % |
| Replay of all 22 transmitters | 6 min | 100 % |
| Same replays with attacker noise | 6 to 18 min | **0 %**: reported as a process or sensor fault |
| Multi-freeze | 18 min | 100 % |
| Stealthy coordinated bias | 48 min | **0 %**: reported as a sensor or process fault |

What this means:

- **Every attack raises an alarm.** Replaying a few transmitters makes them
  disagree with the rest of the plant, and the virtual sensor bank sees that.
- **Guardian recognises an attack as an attack only when the attacker leaves
  a fingerprint**: exact repetition, simultaneous freezes, or broken data.
  A careful attacker who adds noise is still detected, but is reported as a
  fault.
- **The stealthy bias is detected, after 48 minutes.** Only 3 of 22
  transmitters carry most of it, and the controllers' reaction makes it
  inconsistent with normal operation.

## Scans

| Tool | Result |
|---|---|
| `bandit -r guardian scripts dashboard` | 0 issues. The low-severity subprocess notes are reviewed and annotated: fixed binary, no shell, typed arguments |
| `pip-audit -r requirements.txt` | No known vulnerabilities (at release time) |
| `pytest` | All security tests pass (validation, audit log, artifacts, replay, OPC UA) |

## Not covered: residual risks

- **Attacks that copy the plant's physics.** An attacker who knows the plant
  model can craft data that is consistent across all transmitters. No purely
  data-driven monitor can rule this out. Physical watermarking or independent
  instruments (analyzers, redundant transmitters) are the known
  countermeasures.
- **Attacks indistinguishable from faults.** A single manipulated transmitter
  looks exactly like a faulty one. Guardian reports it as a transmitter fault,
  which is still the right field action: check that transmitter.
- **Key management.** Keys come from an environment variable and the demo
  certificates are self-signed. A plant would use its certificate authority
  or the OPC UA Global Discovery Server, a hardware or OS secret store, and
  key rotation.
- **The host itself.** Operating system hardening, patching, account
  management and network segmentation of the Guardian host are site
  responsibilities and outside this repository.
- **Compressed historian data.** The replay detector needs raw samples.
  Historians that compress or deduplicate values need a tolerance-based
  version.
- **The dashboard's trainer.** It can change the simulated plant: faults,
  disturbances, setpoints, attacks. That is acceptable only because the plant
  is a simulator. A plant deployment would ship the monitoring views without
  the trainer, behind the site's authentication (SSO), on HTTPS.
- **Guardian is not a safety system.** It does not replace safety
  instrumented systems, interlocks or operator authority, and it never acts on
  the plant.

## Operating it securely

```bash
export GUARDIAN_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")  # keep in a secret store
python scripts/export_model.py     # fit, calibrate, save the HMAC-signed model to models/guardian
python scripts/opcua_demo.py       # secure OPC UA link -> validation -> Guardian -> signed audit log
```
