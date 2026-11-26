#!/usr/bin/env python3
"""
Report-ready figures from results/runs.csv (warm-up runs excluded).
    python3 scripts/report_figures.py [--results results]
Writes <results>/figures/: fig1_tradeoff_3b.png, fig1_tradeoff_7b.png, fig2_llm_overhead.png,
fig3_load_effect.png and table_configs.csv (mean and 95% CI per configuration).
"""
import argparse
import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt   # noqa: E402
import pandas as pd               # noqa: E402

BLUE, ORANGE, AQUA = "#257de0", "#f47230", "#00b894"
RAMP = ["#9cc3ef", "#4f95e5", "#1a5aa6"]          # light -> dark blue for p50/p95/p99
TEXT, GRID, AXIS = "#333333", "#e7e7e7", "#c2c2c2"
T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26}

LABELS = [  # (config suffix after "<phase>-<model>-", readable label)
    ("both_default", "Default (all shared)"),
    ("nice=mon_favored-weight=shared-pin=shared-rb=medium", "nice: monitor -10"),
    ("nice=llm_favored-weight=shared-pin=shared-rb=medium", "nice: monitor +10 (LLM favored)"),
    ("nice=default-weight=equal-pin=shared-rb=medium", "cgroups: equal weight"),
    ("nice=default-weight=mon_favored-pin=shared-rb=medium", "cgroups: monitor 10x"),
    ("nice=default-weight=llm_favored-pin=shared-rb=medium", "cgroups: LLM 10x"),
    ("nice=default-weight=shared-pin=partial-rb=medium", "pin: partial overlap"),
    ("nice=default-weight=shared-pin=reserved-rb=medium", "pin: reserved monitor core"),
    ("nice=default-weight=shared-pin=shared-rb=small", "buffer: 16 KiB"),
    ("nice=default-weight=shared-pin=shared-rb=large", "buffer: 1 MiB"),
    ("c1-nice=default-weight=mon_favored-pin=shared-rb=large", "combo 1: monitor 10x + 1 MiB"),
    ("c2-nice=mon_favored-weight=shared-pin=partial-rb=large", "combo 2: nice -10 + partial pin + 1 MiB"),
    ("c3-nice=default-weight=mon_favored-pin=partial-rb=medium", "combo 3: monitor 10x + partial pin"),
]


def style(ax):
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(AXIS)
    ax.tick_params(colors=TEXT, labelsize=8)


def stats(df, cfg, col):
    v = pd.to_numeric(df[df.config == cfg][col], errors="coerce").dropna()
    if len(v) == 0:
        return float("nan"), 0.0
    ci = T95.get(len(v) - 1, 1.96) * v.std(ddof=1) / math.sqrt(len(v)) if len(v) > 1 else 0.0
    return v.mean(), ci


def find_cfg(df, model, suffix):
    for c in sorted(set(df.config)):
        parts = c.split("-", 2)
        if len(parts) == 3 and parts[1] == model and parts[2] == suffix:
            return c
    return None


def fig_tradeoff(df, model, out):
    rows = []
    for suffix, label in LABELS:
        c = find_cfg(df, model, suffix)
        if c:
            p95, p95ci = stats(df, c, "lat_p95_ms")
            tok, tokci = stats(df, c, "tok_per_s")
            rows.append((label, p95, p95ci, tok, tokci))
    if not rows:
        return
    rows.sort(key=lambda r: r[1])           # best latency at the top
    rows = rows[::-1]
    alone, _ = stats(df, f"base-{model}-llm_only", "tok_per_s")
    y = range(len(rows))
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 0.42 * len(rows) + 1.4), sharey=True)
    colors = [ORANGE if "reserved" in r[0] else BLUE for r in rows]
    a1.barh(y, [r[1] for r in rows], xerr=[r[2] for r in rows], color=colors, height=0.6,
            ecolor=TEXT, capsize=2, edgecolor="white", linewidth=2)
    a1.set_xscale("log")
    a1.set_xlabel("p95 detection latency, ms (log scale, lower is better)", color=TEXT, fontsize=9)
    a2.barh(y, [r[3] for r in rows], xerr=[r[4] for r in rows], color=colors, height=0.6,
            ecolor=TEXT, capsize=2, edgecolor="white", linewidth=2)
    if alone == alone:
        a2.axvline(alone, color=TEXT, ls="--", lw=1)
        a2.text(alone, len(rows) - 0.4, f" LLM alone {alone:.1f}", fontsize=7, color=TEXT, va="bottom")
    a2.set_xlabel("LLM tokens/s (higher is better)", color=TEXT, fontsize=9)
    for i, r in enumerate(rows):
        a1.text(r[1] * 1.08 + r[2], i, f"{r[1]:.2f}", va="center", fontsize=7, color=TEXT)
        a2.text(r[3] + r[4] + 0.05, i, f"{r[3]:.2f}", va="center", fontsize=7, color=TEXT)
    a1.set_yticks(list(y))
    a1.set_yticklabels([r[0] for r in rows], fontsize=8, color=TEXT)
    for a in (a1, a2):
        style(a)
    fig.suptitle(f"{model.upper()} model: every configuration (mean ± 95% CI, n=5). "
                 f"Event loss was 0% in all of them.", fontsize=10, color=TEXT)
    fig.tight_layout()
    fig.savefig(out, dpi=170)
    plt.close(fig)


def fig_overhead(df, out):
    models = ["0.5b", "3b", "7b"]
    series = [("LLM alone", "base-{m}-llm_only", BLUE),
              ("LLM + monitor, default", "base-{m}-both_default", ORANGE),
              ("LLM + monitor, reserved core", None, AQUA)]
    fig, ax = plt.subplots(figsize=(8, 4))
    w = 0.26
    for k, (name, pat, col) in enumerate(series):
        vals, cis = [], []
        for m in models:
            c = pat.format(m=m) if pat else find_cfg(df, m, LABELS[7][0])
            v, ci = stats(df, c, "tok_per_s") if c else (float("nan"), 0)
            vals.append(v)
            cis.append(ci)
        xs = [i + (k - 1) * w for i in range(len(models))]
        ax.bar(xs, vals, w * 0.92, yerr=cis, color=col, label=name, ecolor=TEXT, capsize=2,
               edgecolor="white", linewidth=1.5)
        for x, v, ci in zip(xs, vals, cis):
            if v == v:
                ax.text(x, v + ci + 0.8, f"{v:.1f}", ha="center", fontsize=7, color=TEXT)
    ax.set_xticks(range(len(models)))
    ax.set_xticklabels(["Qwen2.5 0.5B", "Qwen2.5 3B", "Qwen2.5 7B"], fontsize=9)
    ax.set_ylabel("LLM tokens/s", color=TEXT, fontsize=9)
    ax.set_title("Cost of the monitor to the LLM (mean ± 95% CI). Reserved core not run for 0.5B.",
                 fontsize=10, color=TEXT)
    ax.legend(fontsize=8, frameon=False)
    style(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=170)
    plt.close(fig)


def fig_load(df, out):
    cfgs = [("base-none-monitor_only", "No LLM"), ("base-0.5b-both_default", "0.5B"),
            ("base-3b-both_default", "3B"), ("base-7b-both_default", "7B")]
    fig, ax = plt.subplots(figsize=(7, 4))
    for (col, name), color in zip([("lat_p50_ms", "p50"), ("lat_p95_ms", "p95"), ("lat_p99_ms", "p99")], RAMP):
        vals = [stats(df, c, col)[0] for c, _ in cfgs]
        cis = [stats(df, c, col)[1] for c, _ in cfgs]
        ax.errorbar(range(len(cfgs)), vals, yerr=cis, color=color, marker="o", ms=8, lw=2, capsize=3,
                    label=name, mec="white", mew=1.5)
        ax.text(len(cfgs) - 1 + 0.08, vals[-1], f"{name} {vals[-1]:.1f} ms", fontsize=7, color=TEXT, va="center")
    ax.set_yscale("log")
    ax.set_xticks(range(len(cfgs)))
    ax.set_xticklabels([n for _, n in cfgs], fontsize=9)
    ax.set_xlim(-0.3, len(cfgs) - 1 + 0.75)
    ax.set_xlabel("LLM load (default settings)", color=TEXT, fontsize=9)
    ax.set_ylabel("detection latency, ms (log scale)", color=TEXT, fontsize=9)
    ax.set_title("Monitor latency rises with LLM load; event loss stayed 0%", fontsize=10, color=TEXT)
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    style(ax)
    fig.tight_layout()
    fig.savefig(out, dpi=170)
    plt.close(fig)


def table(df, out):
    cols = ["loss_pct", "kernel_dropped", "first_loss_s", "lat_p50_ms", "lat_p95_ms", "lat_p99_ms", "lat_max_ms",
            "tok_per_s", "mon_cpu_pct", "bpf_ns_per_event"]
    rows = []
    for c in sorted(set(df.config)):
        r = {"config": c, "n": int((df.config == c).sum())}
        for k in cols:
            m, ci = stats(df, c, k) if k in df else (float("nan"), 0.0)
            r[k] = round(m, 3) if m == m else ""
            r[k + "_ci95"] = round(ci, 3) if m == m else ""
        rows.append(r)
    pd.DataFrame(rows).to_csv(out, index=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                      "results"))
    ap.add_argument("--loss-results", default=None, help="results folder of the loss phase (optional)")
    a = ap.parse_args()
    df = pd.read_csv(os.path.join(a.results, "runs.csv"))
    df = df[df.warmup == 0]
    out = os.path.join(a.results, "figures")
    os.makedirs(out, exist_ok=True)
    for m in ("3b", "7b"):
        fig_tradeoff(df, m, os.path.join(out, f"fig1_tradeoff_{m}.png"))
    fig_overhead(df, os.path.join(out, "fig2_llm_overhead.png"))
    fig_load(df, os.path.join(out, "fig3_load_effect.png"))
    table(df, os.path.join(out, "table_configs.csv"))
    if a.loss_results:
        fig_loss(a.loss_results, os.path.join(out, "fig4_loss_buffer.png"))
        ld = pd.read_csv(os.path.join(a.loss_results, "runs.csv"))
        table(ld[ld.warmup == 0], os.path.join(out, "table_loss.csv"))
    print("wrote", out)



def fig_loss(results_loss, out):
    """Loss experiment (H3): overloaded reader, loss % and time to first loss by buffer size."""
    df = pd.read_csv(os.path.join(results_loss, "runs.csv"))
    df = df[df.warmup == 0]
    sizes = [("small", "16 KiB"), ("medium", "128 KiB"), ("large", "1 MiB")]
    cfgs = [(f"loss-7b-overloaded-work600-rb={k}", n) for k, n in sizes]
    if not all(c in set(df.config) for c, _ in cfgs):
        return
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.8))
    for ax, col, ylab, fmt in [(a1, "loss_pct", "events lost in 60 s (%)", "{:.1f}%"),
                               (a2, "first_loss_s", "time to first lost event (s)", "{:.1f} s")]:
        vals = [stats(df, c, col) for c, _ in cfgs]
        ax.bar(range(3), [v for v, _ in vals], yerr=[e for _, e in vals], color=[RAMP[0], RAMP[1], RAMP[2]],
               width=0.6, ecolor=TEXT, capsize=3, edgecolor="white", linewidth=2)
        for i, (v, e) in enumerate(vals):
            ax.text(i, v + e + max(v for v, _ in vals) * 0.03, fmt.format(v), ha="center", fontsize=8, color=TEXT)
        ax.set_xticks(range(3))
        ax.set_xticklabels([n for _, n in cfgs], fontsize=9)
        ax.set_xlabel("ring-buffer size", color=TEXT, fontsize=9)
        ax.set_title(ylab, fontsize=10, color=TEXT)
        style(ax)
    fig.suptitle("Overloaded reader (600 µs per event, 2,000 events/s, 7B): a bigger buffer delays drops, "
                 "it does not stop them", fontsize=10, color=TEXT)
    fig.tight_layout()
    fig.savefig(out, dpi=170)
    plt.close(fig)


if __name__ == "__main__":
    main()
