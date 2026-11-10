import json

import numpy as np
import pandas as pd
import pytest

from guardian import artifacts
from guardian.audit import AuditLog, verify
from guardian.features import SP_NAMES
from guardian.methods import Guardian
from guardian.sim import BASE_SP, INTEGRITY
from guardian.tags import FIELD_TAGS, MONITORED
from guardian.validation import SchemaError, sanitize, validate


def _run(seed: int, n: int = 300) -> pd.DataFrame:
    """Synthetic historian run with correlated, noisy, in-span values."""
    rng = np.random.default_rng(seed)
    latent = rng.normal(size=(n, 3)).cumsum(axis=0) * 0.05
    mix = rng.normal(size=(3, len(MONITORED)))
    x = 50.0 + latent @ mix + 0.3 * rng.normal(size=(n, len(MONITORED)))
    df = pd.DataFrame(x, columns=MONITORED)
    df.insert(0, "time", 2.0 + 0.05 * np.arange(n))
    for sp in SP_NAMES:
        df[f"sp_{sp}"] = BASE_SP[sp]
    return df


# ----- validation ------------------------------------------------------------
def test_validation_accepts_clean_data():
    assert validate(_run(0)).ok


def test_validation_flags_nan_span_and_time_problems():
    df = _run(0)
    df.loc[10, "xmeas_9"] = np.nan
    df.loc[20, "xmeas_7"] = 99999.0  # beyond any pressure transmitter span
    df.loc[30, "time"] = df.loc[29, "time"]  # duplicated timestamp
    rep = validate(df)
    codes = rep.summary()
    assert codes["not_finite"] == 1 and codes["out_of_span"] == 1 and codes["not_increasing"] == 1
    assert rep.pipeline_bad[10] and rep.pipeline_bad[30] and not rep.pipeline_bad[20]
    clean = sanitize(df, rep)
    assert np.isfinite(clean[MONITORED].to_numpy()).all()
    assert clean.loc[10, "xmeas_9"] == df.loc[9, "xmeas_9"]  # last valid value held
    assert clean.loc[20, "xmeas_7"] == 4000.0  # saturates at the span limit


def test_sanitize_repairs_one_sensor_without_touching_the_others():
    df = _run(1)
    df.loc[50, "xmeas_9"] = np.nan
    df.loc[60, "xmeas_7"] = -1e6
    df.loc[70, "time"] = df.loc[69, "time"]  # time problem: values themselves are fine
    clean = sanitize(df, validate(df))
    others = [t for t in MONITORED if t not in ("xmeas_9", "xmeas_7")]
    pd.testing.assert_frame_equal(clean[others], df[others])
    assert clean.loc[70, MONITORED].equals(df.loc[70, MONITORED])
    assert clean.loc[60, "xmeas_7"] == 0.0


def test_sanitize_never_uses_future_values():
    df = _run(2)
    df.loc[0:4, "xmeas_9"] = np.nan  # no valid reading yet
    clean = sanitize(df, validate(df), fallback={"xmeas_9": 123.0})
    assert (clean.loc[0:4, "xmeas_9"] == 123.0).all()  # not the value at row 5
    early = sanitize(df.iloc[:20], validate(df.iloc[:20]))
    full = sanitize(df, validate(df))
    pd.testing.assert_frame_equal(early, full.iloc[:20])  # past output fixed once seen


def test_off_scale_sensor_still_alarms(fitted):
    run = _run(8)
    run.loc[200:, "xmeas_7"] = 99999.0  # transmitter driven off scale
    a = fitted.analyze(run)
    r = a.S[:, a.extra["idx"]["R"]]
    assert r[220:].max() > 10 * r[:180].max()


def test_validation_rejects_missing_or_non_numeric_columns():
    with pytest.raises(SchemaError):
        validate(_run(0).drop(columns=["xmeas_3"]))
    df = _run(0)
    df["xmeas_3"] = "bad"
    with pytest.raises(SchemaError):
        validate(df)


# ----- audit log -------------------------------------------------------------
def test_audit_log_detects_tampering(tmp_path):
    p = tmp_path / "audit.jsonl"
    log = AuditLog(p, key=b"secret")
    for i in range(5):
        log.append({"alarm": i, "class": "sensor_fault", "tag": "xmeas_9"})
    assert verify(p, b"secret") == (True, None)
    lines = p.read_text().splitlines()
    rec = json.loads(lines[2])
    rec["event"]["tag"] = "xmeas_21"  # someone rewrites history
    lines[2] = json.dumps(rec, sort_keys=True)
    p.write_text("\n".join(lines) + "\n")
    assert verify(p, b"secret") == (False, 2)
    with pytest.raises(ValueError):
        AuditLog(p, key=b"secret")


def test_audit_log_needs_the_key(tmp_path):
    p = tmp_path / "audit.jsonl"
    AuditLog(p, key=b"secret").append({"alarm": 1})
    assert verify(p, b"secret")[0]
    assert not verify(p, b"wrong")[0]


def test_audit_log_resumes_chain(tmp_path):
    p = tmp_path / "audit.jsonl"
    AuditLog(p).append({"a": 1})
    AuditLog(p).append({"a": 2})
    assert verify(p) == (True, None)


# ----- model artifacts ---------------------------------------------------------
@pytest.fixture(scope="module")
def fitted():
    train = [_run(s) for s in range(3)]
    val = [_run(s) for s in range(10, 12)]
    return Guardian().fit(train, val)


def test_artifact_roundtrip_reproduces_outputs(fitted, tmp_path):
    lim = np.arange(5.0)
    artifacts.save(fitted, tmp_path, limits=lim, key=b"k")
    g2, lim2, _ = artifacts.load(tmp_path, key=b"k")
    run = _run(99)
    np.testing.assert_allclose(fitted.analyze(run).S, g2.analyze(run).S)
    np.testing.assert_array_equal(lim, lim2)


def test_artifact_tampering_is_refused(fitted, tmp_path):
    artifacts.save(fitted, tmp_path, key=b"k")
    cfg = json.loads((tmp_path / "config.json").read_text())
    cfg["lags"] = 0
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    with pytest.raises(artifacts.ArtifactIntegrityError):
        artifacts.load(tmp_path, key=b"k")


def test_artifact_signature_required_when_key_given(fitted, tmp_path):
    artifacts.save(fitted, tmp_path)  # unsigned
    with pytest.raises(artifacts.ArtifactIntegrityError):
        artifacts.load(tmp_path, key=b"k")


# ----- integrity detection -----------------------------------------------------
def test_replay_detector_flags_repeated_windows_only(fitted):
    run = _run(5, n=200)
    raw = run[FIELD_TAGS].to_numpy().copy()
    assert fitted._replay_score(raw).sum() == 0  # live noise never repeats
    raw[150:190] = raw[100:140]  # attacker replays 40 samples
    score = fitted._replay_score(raw)
    assert score[150 + fitted.replay_win:190].all()


def test_nan_injection_is_an_integrity_event(fitted):
    run = _run(7)
    run.loc[200:210, "xmeas_5"] = np.nan
    a = fitted.analyze(run)
    lim = np.full(a.S.shape[1], np.inf)
    lim[a.extra["idx"]["V"]] = 0.5
    cls, _ = fitted.diagnose(a, 205, lim)
    assert cls == INTEGRITY


def test_artifact_manifest_must_cover_every_file(fitted, tmp_path):
    artifacts.save(fitted, tmp_path, key=b"k")
    m = json.loads((tmp_path / "manifest.json").read_text())
    del m["files"]["arrays.npz"]  # attacker drops a file from the manifest to swap it unchecked
    (tmp_path / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(artifacts.ArtifactIntegrityError):
        artifacts.load(tmp_path, allow_unsigned=True)


def test_artifact_load_needs_a_key_unless_explicitly_unsigned(fitted, tmp_path):
    artifacts.save(fitted, tmp_path)  # unsigned
    with pytest.raises(artifacts.ArtifactIntegrityError):
        artifacts.load(tmp_path)
    g2, _, _ = artifacts.load(tmp_path, allow_unsigned=True)
    run = _run(99)
    np.testing.assert_allclose(fitted.analyze(run).S, g2.analyze(run).S)


def test_audit_log_stays_consistent_under_concurrent_writers(tmp_path):
    import threading

    log = AuditLog(tmp_path / "a.jsonl", key=b"k")

    def writer(w):
        for i in range(200):
            log.append({"w": w, "i": i})

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok, bad, tail = log.check(5)
    assert ok and bad is None and tail[-1]["seq"] == 799


def test_audit_verify_reports_garbage_instead_of_crashing(tmp_path):
    path = tmp_path / "a.jsonl"
    log = AuditLog(path, key=b"k")
    log.append({"x": 1})
    with path.open("a") as f:
        f.write('[1, 2]\n{"seq": 1}\nnot json\n')
    assert verify(path, b"k") == (False, 1)
    assert [r["seq"] for r in log.tail(10)] == [0, 1]  # non-records skipped, no exception


# ----- simulator input -----------------------------------------------------------
def test_simulator_refuses_non_finite_and_out_of_range_numbers(tmp_path):
    import subprocess  # nosec B404: the bundled simulator, fixed arguments, no shell

    from guardian.sim import BINARY

    if not BINARY.exists():
        pytest.skip("simulator not built (make -C sim)")
    for bad in (["--hours", "nan"], ["--hours", "2", "--idv", "1@inf"], ["--hours", "2", "--sf", "9:bias@1=nan"],
                ["--hours", "2", "--act", "10@1=1e9"], ["--hours", "2", "--sp", "prod_rate@1=23abc"]):
        r = subprocess.run([str(BINARY), *bad, "--out", str(tmp_path / "x.csv")],  # nosec B603
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 2 and "bad" in r.stderr, bad
    script = "sp prod_rate nan\nsf 9 bias inf\nact 3 1e9\nreplay 9 nan\nsfclear 9x\nsf 9 bias 0.5\nquit\n"
    r = subprocess.run([str(BINARY), "--interactive"], input=script,  # nosec B603
                       capture_output=True, text=True, timeout=60)
    replies = [x for x in r.stdout.splitlines() if x.startswith(("OK", "ERR"))]
    assert [x.split()[0] for x in replies] == ["ERR"] * 5 + ["OK"]
