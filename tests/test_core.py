import numpy as np
import pytest

from guardian.bank import VirtualSensorBank
from guardian.features import ewma, lag_stack
from guardian.monitors import PCAMonitor
from guardian.sim import BINARY, Scenario, SensorFault, run


def test_lag_stack_aligns_past_samples():
    x = np.arange(10, dtype=float).reshape(5, 2)
    z = lag_stack(x, 2)
    assert z.shape == (5, 6)
    np.testing.assert_array_equal(z[3], [6, 7, 4, 5, 2, 3])
    np.testing.assert_array_equal(z[0], [0, 1, 0, 1, 0, 1])  # edge padded


def test_ewma_converges_to_constant():
    z = np.ones((200, 3))
    assert np.allclose(ewma(z, 0.2)[-1], 1.0)


def _correlated(n, rng):
    t = rng.normal(size=(n, 2))
    mix = np.array([[1.0, 0.5, -0.3, 0.8, 0.2], [0.2, -0.7, 1.0, 0.1, 0.9]])
    return t @ mix + 0.05 * rng.normal(size=(n, 5))


def test_bank_reconstruction_isolates_biased_sensor():
    rng = np.random.default_rng(0)
    bank = VirtualSensorBank(5, lags=0).fit([_correlated(2000, rng)])
    bank.calibrate([_correlated(1000, rng)])
    x = _correlated(300, rng)
    x[:, 3] += 1.0  # bias on tag 3
    z = bank.residuals(x)
    assert np.argmax(np.abs(z).mean(axis=0)) == 3
    _, z3 = bank.reconstruct(x, 3)
    others = np.delete(np.arange(5), 3)
    assert np.abs(z3[:, others]).mean() < 1.5  # tag 3 explains everything


def test_pca_rbc_points_at_faulty_variable():
    rng = np.random.default_rng(1)
    pca = PCAMonitor(0.95).fit(_correlated(2000, rng))
    x = _correlated(50, rng)
    x[:, 1] += 2.0
    st = pca.stats(x)
    assert np.argmax(st["rbc"].mean(axis=0)) == 1


@pytest.mark.skipif(not BINARY.exists(), reason="simulator not built")
def test_closed_loop_sensor_bias_moves_the_real_process():
    """A +1 degC bias on the reactor temperature transmitter: the controller
    holds the *measurement* at setpoint, so the real reactor runs 1 degC cold."""
    s = Scenario("t", seed=3, hours=8.0, event_class="sensor_fault", event_t=1.0,
                 sensor_fault=SensorFault("xmeas_9", "bias", 1.0, 1.0))
    df = run(s)
    late = df[df.time > 5.0]
    assert abs(late.xmeas_9.mean() - 122.9) < 0.05
    assert abs(late.true_9.mean() - 121.9) < 0.1
    early = df[df.time < 1.0]
    np.testing.assert_allclose(early.xmeas_9, early.true_9)
