"""Figures for the README.

    python scripts/make_figures.py

Needs results from run_benchmark.py, run_softsensor.py and summarize_results.py.
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
from dashboard.plant import ISA  # noqa: E402
from guardian.evaluate import calibrate, persisted, prepare  # noqa: E402
from guardian.features import load_split, runs, signature_runs  # noqa: E402
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
    "legend.frameon": True, "legend.framealpha": 1.0, "legend.edgecolor": "none", "legend.facecolor": "white",
})


def _fitted():
    train, _ = load_split("train")
    val, vm = load_split("val")
    test, tm = load_split("test")
    tr = [g for _, g in runs(train)]
    vr = list(runs(val))
    g = Guardian().fit(tr, [r for n, r in vr if vm[n]["event_class"] == NORMAL])
    g.fit_signatures(signature_runs())
    lim, _ = calibrate(prepare(g, vr, vm), 0.25)
    ss = SoftSensor().fit(tr, scaler=g.scaler)
    return g, lim, ss, dict(runs(test)), tm


def _case(g, lim, test, tm, scenario, tag):
    run = test[scenario]
    p = prepare(g, [(scenario, run)], tm)[0]
    alarm = persisted((p.a.S > lim).any(axis=1))
    rising = alarm & ~np.r_[False, alarm[:-1]]
    i_alarm = int(np.flatnonzero(rising & (p.time >= tm[scenario]["event_t"]))[0])
    cls, diag_tag = g.diagnose(p.a, min(i_alarm + 20, len(p.time) - 1), lim)
    k = MONITORED.index(tag)
    x = g.scaler.transform(run)
    xk, _ = g.bank.reconstruct(x, k)
    virtual = xk[:, k] * g.scaler.std[k] + g.scaler.mean[k]
    return run, x, xk, virtual, i_alarm, cls, diag_tag


def _mark(ax, t_ev, t_alarm, text):
    """Fault and alarm as vertical lines, explained in a line under the title (never on the data)."""
    for a_ in ax:
        a_.axvline(t_ev, color=MUTED, ls="--", lw=0.9)
        a_.axvline(t_alarm, color=RED, ls="-", lw=0.9)
        a_.set_xlim(8, 30)
    ax[0].text(0, 1.03, f"Dashed line: fault starts.  Red line: {text}.", transform=ax[0].transAxes,
               fontsize=9, color=MUTED)


def _verdict(delay_h: float, cls: str, tag: str | None) -> str:
    return f"alarm {delay_h * 60:.0f} min later, diagnosed as {cls.replace('_', ' ')} on {ISA.get(tag, tag)}"


def loop_fault(g, lim, test, tm, scenario="test_sf_xmeas_9_bias_0") -> None:
    run, x, xk, virtual, i, cls, tag = _case(g, lim, test, tm, scenario, "xmeas_9")
    t = run["time"].to_numpy()
    t_ev = tm[scenario]["event_t"]
    fig, ax = plt.subplots(2, 1, figsize=(9, 5.6), sharex=True)
    ax[0].plot(t, run["true_9"], color=INK, lw=1.4, label="True reactor temperature")
    ax[0].plot(t, run["xmeas_9"], color=ORANGE, lw=1.2, label="Transmitter reading (what the DCS sees)")
    ax[0].set_ylabel("Reactor temp. [°C]")
    ax[0].set_title("A biased transmitter hides behind its own control loop, the cooling water does not",
                    loc="left", fontsize=11, fontweight="bold", pad=20)
    ax[1].plot(t, run["xmeas_21"], color=BLUE, lw=1.2, label="Reactor cooling-water outlet temperature")
    ax[1].set_ylabel("CW outlet [°C]")
    ax[1].set_xlabel("Time [h]")
    _mark(ax, t_ev, t[i], _verdict(t[i] - t_ev, cls, tag))
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
                    loc="left", fontsize=11, fontweight="bold", pad=20)
    ax[1].plot(t, aligned_truth(run), color=INK, lw=1.4, label="Product analyzer (aligned for dead time)")
    ax[1].plot(t, raw, color=ORANGE, lw=1.1, label="Soft sensor, raw inputs")
    ax[1].plot(t, prot, color=BLUE, lw=1.1, label="Soft sensor, Guardian-protected")
    ax[1].set_ylabel("Product E [mol%]")
    ax[1].set_xlabel("Time [h]")
    _mark(ax, t_ev, t[i], _verdict(t[i] - t_ev, cls, tag))
    ax[0].legend(loc="lower left", fontsize=8.5)
    ax[1].legend(loc="upper left", fontsize=8.5)
    fig.tight_layout()
    fig.savefig(FIG / "softsensor_protection.png", dpi=160)
    plt.close(fig)


def benchmark_bars() -> None:
    """Hero chart: all four methods at the same false-alarm budget, with 95 % intervals."""
    t = pd.read_csv(RES / "table_main.csv")  # python scripts/summarize_results.py
    order = ["Limit checks", "PCA + contributions", "PCA + RBC", "Guardian"]
    metrics = [("detected", "Events detected", "of 264 events"),
               ("correct_class", "Diagnosis class correct", "of detected events"),
               ("transmitter_named", "Faulty transmitter named", "of detected transmitter faults")]
    fig, ax = plt.subplots(figsize=(10, 4.4))
    w = 0.19
    colors = ["#d0d7de", "#afb8c1", "#8c959f", BLUE]
    for n, mth in enumerate(order):
        r = t[t.method == mth].set_index("metric")
        vals = np.array([r.loc[m, "value"] for m, _, _ in metrics])
        lo = vals - np.array([r.loc[m, "ci_low"] for m, _, _ in metrics])
        hi = np.array([r.loc[m, "ci_high"] for m, _, _ in metrics]) - vals
        pos = np.arange(len(metrics)) + (n - 1.5) * w
        ax.bar(pos, vals, w * 0.92, color=colors[n], label=mth, zorder=2)
        ax.errorbar(pos, vals, yerr=[lo, hi], fmt="none", ecolor=INK, elinewidth=0.9, capsize=2.5, zorder=3)
        for x_, v, h in zip(pos, vals, hi, strict=True):
            ax.text(x_, v + h + 0.02, f"{v:.0%}", ha="center", fontsize=8.5, color=INK,
                    fontweight="bold" if mth == "Guardian" else "normal")
    ax.set_xticks(np.arange(len(metrics)), [f"{a}\n{b}" for _, a, b in metrics], fontsize=9.5)
    for lab in ax.get_xticklabels():
        lab.set_color(INK)
    ax.set_ylim(0, 1.12)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0], ["0%", "25%", "50%", "75%", "100%"])
    ax.grid(axis="x", visible=False)
    fa = t[t.metric == "false_alarms_per_day"].set_index("method").loc[order, "value"]
    ax.set_title("Guardian vs. classical process monitoring, same false-alarm budget",
                 loc="left", fontsize=12, fontweight="bold", pad=22)
    ax.text(0, 1.035, f"Closed-loop Tennessee Eastman, 288 held-out test runs. "
            f"False alarms {fa.min():.2f} to {fa.max():.2f} per day. Error bars: 95 % intervals.",
            transform=ax.transAxes, fontsize=9, color=MUTED)
    ax.legend(ncol=4, fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.2))
    fig.tight_layout()
    fig.savefig(FIG / "benchmark.png", dpi=200, facecolor="white")
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


def hai_bars() -> None:
    """HAI hardware-in-the-loop results, from results/hai_summary.csv (scripts/run_hai.py)."""
    t = pd.read_csv(RES / "hai_summary.csv")
    order = ["Limit checks", "PCA + contributions", "PCA + RBC", "Guardian"]
    metrics = [("attacks_detected", "Attacks detected on the HIL testbed", "of 50 labelled attacks"),
               ("injected_detected", "Injected transmitter faults detected", "of 104 faults"),
               ("injected_transmitter_named", "Faulty transmitter named", "of detected injected faults")]
    fig, ax = plt.subplots(figsize=(10, 4.4))
    w = 0.19
    colors = ["#d0d7de", "#afb8c1", "#8c959f", BLUE]
    for n, mth in enumerate(order):
        r = t[t.method == mth].set_index("metric")
        vals = np.array([r.loc[m, "value"] for m, _, _ in metrics])
        lo = vals - np.array([r.loc[m, "ci_low"] for m, _, _ in metrics])
        hi = np.array([r.loc[m, "ci_high"] for m, _, _ in metrics]) - vals
        pos = np.arange(len(metrics)) + (n - 1.5) * w
        ax.bar(pos, vals, w * 0.92, color=colors[n], label=mth, zorder=2)
        ax.errorbar(pos, vals, yerr=[lo, hi], fmt="none", ecolor=INK, elinewidth=0.9, capsize=2.5, zorder=3)
        for x_, v, h in zip(pos, vals, hi, strict=True):
            ax.text(x_, v + h + 0.02, f"{v:.0%}", ha="center", fontsize=8.5, color=INK,
                    fontweight="bold" if mth == "Guardian" else "normal")
    ax.set_xticks(np.arange(len(metrics)), [f"{a}\n{b}" for _, a, b in metrics], fontsize=9.5)
    ax.set_ylim(0, 1.12)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0], ["0%", "25%", "50%", "75%", "100%"])
    ax.grid(axis="x", visible=False)
    fa = t[t.metric == "attack_false_alarms_per_day"].set_index("method").loc[order, "value"]
    ax.set_title("HAI 21.03 hardware-in-the-loop testbed, method unchanged from Tennessee Eastman",
                 loc="left", fontsize=12, fontweight="bold", pad=22)
    short = {"Limit checks": "limits", "PCA + contributions": "PCA", "PCA + RBC": "PCA+RBC", "Guardian": "Guardian"}
    ax.text(0, 1.035, "False alarms per day on test (target 0.25): "
            + ", ".join(f"{short[m]} {v:.2f}" for m, v in fa.items()) + ". Error bars: 95 % intervals.",
            transform=ax.transAxes, fontsize=9, color=MUTED)
    ax.legend(ncol=4, fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.2))
    fig.tight_layout()
    fig.savefig(FIG / "hai_results.png", dpi=200, facecolor="white")
    plt.close(fig)


GREYS = {"Limit checks": "#d0d7de", "PCA + contributions": "#afb8c1", "PCA + RBC": "#8c959f", "Guardian": BLUE}
DASHES = {"Limit checks": (0, (1, 1.5)), "PCA + contributions": (0, (4, 2)), "PCA + RBC": (0, (6, 2, 1, 2)),
          "Guardian": "solid"}


def _title(ax, title: str, subtitle: str) -> None:
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold", pad=22)
    ax.text(0, 1.035, subtitle, transform=ax.transAxes, fontsize=9, color=MUTED)


def operating_curve() -> None:
    """Detection against false alarms per day on test, one point per calibration budget."""
    c = pd.read_csv(RES / "operating_curve.csv")
    fig, ax = plt.subplots(figsize=(10, 4.2))
    for m in ["Limit checks", "PCA + contributions", "PCA + RBC", "Guardian"]:
        d = c[c.method == m].sort_values("false_alarms_per_day")
        ax.plot(d.false_alarms_per_day, d.detection_rate, color="#57606a" if m != "Guardian" else BLUE,
                linestyle=DASHES[m], linewidth=2 if m == "Guardian" else 1.4, marker="o", markersize=4.5,
                label=m, zorder=3 if m == "Guardian" else 2)
    ax.axvline(0.25, color=MUTED, linewidth=0.8, linestyle=":")
    ax.text(0.26, 0.05, "budget used in the tables", fontsize=8.5, color=MUTED)
    ax.set_xscale("log")
    ax.set_xticks([0.05, 0.1, 0.25, 0.5, 1, 2], ["0.05", "0.1", "0.25", "0.5", "1", "2"])
    ax.minorticks_off()
    ax.set_ylim(0, 1.02)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0], ["0%", "25%", "50%", "75%", "100%"])
    ax.set_xlabel("False alarms per day on test (log scale)")
    ax.set_ylabel("Events detected")
    _title(ax, "The gap holds at every false-alarm budget",
           "Each method recalibrated on validation for 6 budgets, then scored on the 288 test runs. "
           "The two PCA methods detect the same events, so their lines coincide.")
    ax.legend(ncol=4, fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.2))
    fig.tight_layout()
    fig.savefig(FIG / "operating_curve.png", dpi=200, facecolor="white")
    plt.close(fig)


def detection_time() -> None:
    """Share of the 264 test events detected within a given time of their start."""
    df = pd.read_csv(RES / "benchmark_runs.csv")
    grid = np.linspace(0, 12, 241)
    fig, ax = plt.subplots(figsize=(10, 4.2))
    for m in ["Limit checks", "PCA + contributions", "PCA + RBC", "Guardian"]:
        ev = df[(df.method == m) & (df.true_class != NORMAL)]
        delay = ev.delay_h.to_numpy()
        share = [(np.nan_to_num(delay, nan=np.inf) <= h).mean() for h in grid]
        ax.plot(grid, share, color="#57606a" if m != "Guardian" else BLUE, linestyle=DASHES[m],
                linewidth=2 if m == "Guardian" else 1.4, label=m, zorder=3 if m == "Guardian" else 2)
    g = df[(df.method == "Guardian") & (df.true_class != NORMAL)].delay_h
    ax.annotate(f"{(g <= 1).mean():.0%} within 1 h", xy=(1, (g <= 1).mean()), xytext=(1.6, 0.6),
                fontsize=9, color=INK, arrowprops={"arrowstyle": "-", "color": MUTED, "linewidth": 0.8})
    ax.set_xlim(0, 12)
    ax.set_xticks(range(0, 13, 2))
    ax.set_ylim(0, 1.02)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0], ["0%", "25%", "50%", "75%", "100%"])
    ax.set_xlabel("Hours since the event started")
    ax.set_ylabel("Events detected so far")
    _title(ax, "How fast each method raises the alarm",
           "264 test events at 0.25 false alarms per day; the two PCA lines coincide. "
           "Shown for the first 12 h; events detected later, or never, keep every line below 100%.")
    ax.legend(ncol=4, fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.2))
    fig.tight_layout()
    fig.savefig(FIG / "detection_time.png", dpi=200, facecolor="white")
    plt.close(fig)


def loop_bars() -> None:
    """Transmitter named, outside and inside control loops (results/table_loops.csv)."""
    t = pd.read_csv(RES / "table_loops.csv")
    order = ["PCA + RBC", "Guardian w/o loop signatures", "Guardian"]
    labels = {"PCA + RBC": "PCA + RBC", "Guardian w/o loop signatures": "Guardian without loop signatures",
              "Guardian": "Guardian"}
    colors = ["#afb8c1", "#93b4f5", BLUE]
    groups = ["outside control loops", "inside a control loop"]
    fig, ax = plt.subplots(figsize=(10, 4.2))
    w = 0.26
    for n, m in enumerate(order):
        d = t[t.method == m].set_index("transmitters").loc[groups]
        pos = np.arange(2) + (n - 1) * w
        ax.bar(pos, d.named_of_detected, w * 0.92, color=colors[n], label=labels[m], zorder=2)
        for x_, v, k, nn, wr in zip(pos, d.named_of_detected, d.named_k, d.detected_k, d.wrong_transmitter_k,
                                    strict=True):
            ax.text(x_, v + 0.075, f"{v:.0%}", ha="center", fontsize=9.5, color=INK,
                    fontweight="bold" if m == "Guardian" else "normal")
            ax.text(x_, v + 0.025, f"{k} of {nn}, {wr} wrong", ha="center", fontsize=7.5, color=MUTED)
    ax.set_xticks(range(2), ["Transmitters outside control loops\n72 test runs",
                             "Transmitters inside a control loop\n104 test runs"], fontsize=9.5)
    ax.set_ylim(0, 1.18)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0], ["0%", "25%", "50%", "75%", "100%"])
    ax.grid(axis="x", visible=False)
    _title(ax, "Naming the faulty transmitter, outside and inside control loops",
           "Share of detected transmitter faults where the right transmitter is named. "
           "Under each value: named of detected, and faults blamed on another transmitter.")
    ax.legend(ncol=3, fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.2))
    fig.tight_layout()
    fig.savefig(FIG / "loops.png", dpi=200, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    FIG.mkdir(parents=True, exist_ok=True)
    benchmark_bars()
    hai_bars()
    confusion()
    operating_curve()
    detection_time()
    loop_bars()
    g, lim, ss, test, tm = _fitted()
    loop_fault(g, lim, test, tm)
    protected_softsensor(g, lim, ss, test, tm)
    print("figures written to", FIG)
