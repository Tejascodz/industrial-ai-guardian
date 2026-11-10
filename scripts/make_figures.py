"""Figures for the README.

    python scripts/make_figures.py

Needs results from run_benchmark.py and run_softsensor.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardian.evaluate import calibrate, persisted, prepare  # noqa: E402
from guardian.features import load_split, runs  # noqa: E402
from guardian.methods import Guardian  # noqa: E402
from guardian.sim import NORMAL  # noqa: E402
from guardian.softsensor import SoftSensor, aligned_truth  # noqa: E402
from guardian.tags import MONITORED  # noqa: E402

FIG = ROOT / "docs" / "figures"
RES = ROOT / "results"
INK, MUTED, GRID = "#1f2328", "#6e7781", "#d8dee4"
BLUE, ORANGE, GREEN, RED = "#2563eb", "#ea580c", "#16a34a", "#dc2626"

plt.rcParams.update({
    "font.size": 10, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "text.color": INK,
    "xtick.color": MUTED, "ytick.color": MUTED, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.6, "axes.spines.top": False, "axes.spines.right": False,
    "legend.frameon": False,
})


def _fitted():
    train, _ = load_split("train")
    val, vm = load_split("val")
    test, tm = load_split("test")
    tr = [g for _, g in runs(train)]
    vr = list(runs(val))
    g = Guardian().fit(tr, [r for n, r in vr if vm[n]["event_class"] == NORMAL])
    lim, _ = calibrate(prepare(g, vr, vm), 0.25)
    ss = SoftSensor().fit(tr, scaler=g.scaler)
    return g, lim, ss, dict(runs(test)), tm


def _case(g, lim, test, tm, scenario, tag):
    run = test[scenario]
    p = prepare(g, [(scenario, run)], tm)[0]
    alarm = persisted((p.a.S > lim).any(axis=1))
    i_alarm = int(np.flatnonzero(alarm & (p.time >= tm[scenario]["event_t"]))[0])
    cls, diag_tag = g.diagnose(p.a, min(i_alarm + 20, len(p.time) - 1), lim)
    k = MONITORED.index(tag)
    x = g.scaler.transform(run)
    xk, _ = g.bank.reconstruct(x, k)
    virtual = xk[:, k] * g.scaler.std[k] + g.scaler.mean[k]
    return run, x, xk, virtual, i_alarm, cls, diag_tag


def _mark(ax, t_ev, t_alarm, text):
    for a_ in ax:
        a_.axvline(t_ev, color=MUTED, ls="--", lw=0.9)
        a_.axvline(t_alarm, color=RED, ls="-", lw=0.9)
        a_.legend(loc="upper left", fontsize=8.5)
        a_.set_xlim(8, 30)
    y0, y1 = ax[0].get_ylim()
    ax[0].text(t_ev, y0, " fault starts", color=MUTED, fontsize=8, va="bottom")
    ax[0].text(t_alarm, y1, text, color=RED, fontsize=8, va="top")


def loop_fault(g, lim, test, tm, scenario="test_sf_xmeas_9_bias_0") -> None:
    run, x, xk, virtual, i, cls, tag = _case(g, lim, test, tm, scenario, "xmeas_9")
    t = run["time"].to_numpy()
    t_ev = tm[scenario]["event_t"]
    fig, ax = plt.subplots(2, 1, figsize=(9, 5.6), sharex=True)
    ax[0].plot(t, run["true_9"], color=INK, lw=1.4, label="True reactor temperature")
    ax[0].plot(t, run["xmeas_9"], color=ORANGE, lw=1.2, label="Transmitter reading (what the DCS sees)")
    ax[0].set_ylabel("Reactor temp. [°C]")
    ax[0].set_title("A biased transmitter hides behind its own control loop, the cooling water does not",
                    loc="left", fontsize=11, fontweight="bold")
    ax[1].plot(t, run["xmeas_21"], color=BLUE, lw=1.2, label="Reactor cooling-water outlet temperature")
    ax[1].set_ylabel("CW outlet [°C]")
    ax[1].set_xlabel("Time [h]")
    _mark(ax, t_ev, t[i], f" alarm +{(t[i] - t_ev) * 60:.0f} min: {cls.replace('_', ' ')} on {tag}")
    for a_ in ax:
        a_.legend(loc="center right", fontsize=8.5)
    fig.tight_layout()
    fig.savefig(FIG / "loop_fault_reactor_tt.png", dpi=160)
    plt.close(fig)


def protected_softsensor(g, lim, ss, test, tm, scenario="test_sf_xmeas_18_drift_0") -> None:
    run, x, xk, virtual, i, cls, tag = _case(g, lim, test, tm, scenario, "xmeas_18")
    t = run["time"].to_numpy()
    t_ev = tm[scenario]["event_t"]
    raw = ss.predict_x(x)
    prot = raw.copy()
    prot[i:] = ss.predict_x(xk)[i:]
    fig, ax = plt.subplots(2, 1, figsize=(9, 5.6), sharex=True)
    ax[0].plot(t, run["true_18"], color=INK, lw=1.4, label="True stripper temperature")
    ax[0].plot(t, run["xmeas_18"], color=ORANGE, lw=1.2, label="Drifting transmitter")
    ax[0].plot(t, virtual, color=BLUE, lw=1.0, alpha=0.85, label="Guardian virtual sensor")
    ax[0].set_ylabel("Stripper temp. [°C]")
    ax[0].set_title("Keeping the product-quality soft sensor alive through a transmitter drift",
                    loc="left", fontsize=11, fontweight="bold")
    ax[1].plot(t, aligned_truth(run), color=INK, lw=1.4, label="Product analyzer (aligned for dead time)")
    ax[1].plot(t, raw, color=ORANGE, lw=1.1, label="Soft sensor, raw inputs")
    ax[1].plot(t, prot, color=BLUE, lw=1.1, label="Soft sensor, Guardian-protected")
    ax[1].set_ylabel("Product E [mol%]")
    ax[1].set_xlabel("Time [h]")
    _mark(ax, t_ev, t[i], f" alarm +{(t[i] - t_ev) * 60:.0f} min: {cls.replace('_', ' ')} on {tag}")
    ax[0].legend(loc="center left", fontsize=8.5)
    ax[1].legend(loc="lower left", fontsize=8.5)
    fig.tight_layout()
    fig.savefig(FIG / "softsensor_protection.png", dpi=160)
    plt.close(fig)


def benchmark_bars() -> None:
    sm = pd.read_csv(RES / "benchmark_summary.csv")
    sm = sm[sm.diagnosis == "1 h after detection"]
    order = ["Limit checks", "PCA + contributions", "PCA + RBC", "Guardian"]
    sm = sm.set_index("method").loc[order]
    metrics = [("detection_rate", "Events detected"),
               ("class_accuracy", "Correct class\n(of detected)"),
               ("sensor_correct_tag", "Faulty transmitter\nnamed correctly")]
    fig, ax = plt.subplots(figsize=(9, 3.6))
    w = 0.2
    colors = [GRID, "#9ca3af", MUTED, BLUE]
    for n, mth in enumerate(order):
        vals = [sm.loc[mth, m] for m, _ in metrics]
        pos = np.arange(len(metrics)) + (n - 1.5) * w
        ax.bar(pos, vals, w, color=colors[n], label=mth)
        for x_, v in zip(pos, vals):
            ax.text(x_, v + 0.015, f"{v:.0%}", ha="center", fontsize=7.5, color=INK)
    ax.set_xticks(np.arange(len(metrics)), [m[1] for m in metrics])
    ax.set_ylim(0, 1.1)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0], ["0%", "25%", "50%", "75%", "100%"])
    ax.grid(axis="x", visible=False)
    fa = sm["false_alarms_per_day"]
    ax.set_title(f"Test set, same false-alarm budget ({fa.min():.1f}-{fa.max():.1f} false alarms/day on test)",
                 loc="left", fontsize=11, fontweight="bold")
    ax.legend(ncol=4, fontsize=8.5, loc="upper center", bbox_to_anchor=(0.5, -0.18))
    fig.tight_layout()
    fig.savefig(FIG / "benchmark.png", dpi=160)
    plt.close(fig)


def confusion() -> None:
    df = pd.read_csv(RES / "benchmark_runs.csv")
    d = df[(df.method == "Guardian") & (df.true_class != NORMAL) & df.detected]
    labels = ["sensor_fault", "process_fault", "regime_change"]
    names = ["Sensor fault", "Process fault", "Regime change"]
    m = pd.crosstab(d.true_class, d.diag_class_1h).reindex(index=labels, columns=labels, fill_value=0)
    frac = m.div(m.sum(axis=1), axis=0)
    fig, ax = plt.subplots(figsize=(4.8, 3.8))
    ax.imshow(frac.to_numpy(), cmap="Blues", vmin=0, vmax=1)
    for r in range(3):
        for c in range(3):
            v = frac.iloc[r, c]
            ax.text(c, r, f"{v:.0%}\n({m.iloc[r, c]})", ha="center", va="center",
                    color="white" if v > 0.6 else INK, fontsize=9)
    ax.set_xticks(range(3), names, fontsize=8.5)
    ax.set_yticks(range(3), names, fontsize=8.5)
    ax.set_xlabel("Guardian diagnosis")
    ax.set_ylabel("Ground truth")
    ax.grid(False)
    ax.set_title("Guardian: what the alarm was", loc="left", fontsize=11, fontweight="bold")
    fig.tight_layout()
    fig.savefig(FIG / "confusion.png", dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    FIG.mkdir(parents=True, exist_ok=True)
    benchmark_bars()
    confusion()
    g, lim, ss, test, tm = _fitted()
    loop_fault(g, lim, test, tm)
    protected_softsensor(g, lim, ss, test, tm)
    print("figures written to", FIG)
