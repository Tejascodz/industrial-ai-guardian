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
    assert clean.loc[20, "xmeas_7"] == clean.loc[19, "xmeas_7"]  # last valid value held


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
        artifacts.load(tmp_path)
