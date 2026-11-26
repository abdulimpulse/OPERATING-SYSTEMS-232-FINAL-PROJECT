#!/usr/bin/env python3
"""
Analysis: aggregates results/runs.csv (warm-up runs excluded) and makes the plots.
    python3 scripts/analyze.py                      # uses results/
    python3 scripts/analyze.py --results results_pilot
Outputs in <results>/analysis/:
    summary.csv          one row per configuration: mean, std, 95% CI of every metric
    overhead.csv         monitor overhead per load level
    recommendation.txt   Pareto set and the recommended configuration under the limits
    pareto_loss.png, pareto_p95.png, ofat_*.png, load_effect.png
"""
import argparse
import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt   # noqa: E402
import pandas as pd               # noqa: E402

COLORS = {"base": "#257de0", "ofat": "#f47230", "combo": "#00b894", "pilot": "#257de0"}
MARKERS = {"base": "o", "ofat": "s", "combo": "D", "pilot": "o"}
TEXT, GRID, AXIS = "#333333", "#e7e7e7", "#c2c2c2"
METRICS = ["loss_pct", "kernel_dropped", "lat_p50_ms", "lat_p95_ms", "lat_p99_ms", "buf_p99_ms",
           "tok_per_s", "llm_resp_s_mean", "mon_cpu_pct", "bpf_ns_per_event", "bpf_cpu_pct",
           "mon_maxrss_kb", "cpu_busy_exp_pct", "cpu_busy_all_pct", "gen_rate_achieved",
           "swap_out_pages"]
T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23}


def style(ax):
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(AXIS)
    ax.tick_params(colors=TEXT, labelsize=8)


def short(cfg):
    """Readable label: only the settings that differ from the default."""
    parts = cfg.split("-")
    if parts[0] == "base":
        return parts[-1]
    keep = [p for p in parts[2:] if "=" in p and p.split("=")[1] not in ("default", "shared", "medium")
            or p.startswith("rb=") and p != "rb=medium"]
    name = parts[2] + ": " if parts[0] == "combo" else ""
    return name + (", ".join(dict.fromkeys(keep)) or "default")


def aggregate(df):
    rows = []
    for cfg, g in df.groupby("config"):
        r = {"config": cfg, "label": short(cfg), "n": len(g)}
        for c in ("phase", "model", "nice", "weight", "pin", "rb", "monitor_on", "gen_on"):
            r[c] = g[c].iloc[0]
        for m in METRICS:
            if m not in g:
                continue
            v = pd.to_numeric(g[m], errors="coerce").dropna()
            if len(v) == 0:
                continue
            r[m] = v.mean()
            r[m + "_std"] = v.std(ddof=1) if len(v) > 1 else 0.0
            r[m + "_ci95"] = T95.get(len(v) - 1, 1.96) * r[m + "_std"] / math.sqrt(len(v)) if len(v) > 1 else 0.0
        rows.append(r)
    return pd.DataFrame(rows)


def pareto(sub, ycol):
    """Non-dominated configs: higher tok/s is better, lower y is better."""
    pts = sub[["tok_per_s", ycol]].values
    keep = []
    for i, (x, y) in enumerate(pts):
        dom = any((x2 >= x and y2 <= y) and (x2 > x or y2 < y) for j, (x2, y2) in enumerate(pts) if j != i)
        keep.append(not dom)
    return sub[keep]


def pareto_plot(s, ycol, ylabel, path, limit=None):
    sub = s[(s.monitor_on == 1) & (s.model != "none") & s.tok_per_s.notna() & s[ycol].notna()]
    models = [m for m in ("0.5b", "3b", "7b") if m in set(sub.model)]
    if not models:
        return
    fig, axes = plt.subplots(1, len(models), figsize=(6.2 * len(models), 5), squeeze=False)
    for ax, m in zip(axes[0], models):
        d = sub[sub.model == m]
        for ph, g in d.groupby("phase"):
            ax.errorbar(g.tok_per_s, g[ycol], xerr=g.get("tok_per_s_ci95"), yerr=g.get(ycol + "_ci95"),
                        fmt=MARKERS.get(ph, "o"), color=COLORS.get(ph, "#257de0"), ecolor=COLORS.get(ph, "#257de0"), ms=8, lw=1.2,
                        capsize=3, label=ph, mec="white", mew=1.5)
        front = pareto(d, ycol).sort_values("tok_per_s")
        ax.plot(front.tok_per_s, front[ycol], color=TEXT, lw=1, ls="--", label="Pareto front")
        for _, r in front.iterrows():
            ax.annotate(r.label, (r.tok_per_s, r[ycol]), textcoords="offset points", xytext=(6, 6),
                        fontsize=7, color=TEXT)
        if limit is not None:
            ax.axhline(limit, color=AXIS, lw=1, ls=":")
            ax.text(ax.get_xlim()[0], limit, f" limit {limit}", va="bottom", fontsize=7, color=TEXT)
        ax.set_title(f"{m.upper()} model", color=TEXT, fontsize=10)
        ax.set_xlabel("LLM tokens/s (higher is better)", color=TEXT, fontsize=9)
        ax.set_ylabel(ylabel + " (lower is better)", color=TEXT, fontsize=9)
        style(ax)
        ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def ofat_plots(s, outdir):
    for m in sorted(set(s[s.phase == "ofat"].model)):
        base = s[(s.config == f"base-{m}-both_default")]
        for factor in ("nice", "weight", "pin", "rb"):
            d = s[(s.phase == "ofat") & (s.model == m) & (s[factor] != {"nice": "default", "weight": "shared",
                                                                          "pin": "shared", "rb": "medium"}[factor])]
            d = pd.concat([base, d])
            if len(d) < 2:
                continue
            labels = [("default" if c.startswith("base") else str(v)) for c, v in zip(d.config, d[factor])]
            fig, axes = plt.subplots(1, 3, figsize=(13, 3.6))
            for ax, (col, yl) in zip(axes, [("loss_pct", "event loss (%)"), ("lat_p95_ms", "p95 latency (ms)"),
                                            ("tok_per_s", "LLM tokens/s")]):
                if col not in d:
                    continue
                ax.bar(range(len(d)), d[col], yerr=d.get(col + "_ci95"), color="#257de0", width=0.6,
                       capsize=3, edgecolor="white", linewidth=2, ecolor=TEXT)
                ax.set_xticks(range(len(d)))
                ax.set_xticklabels(labels, fontsize=8)
                ax.set_title(yl, fontsize=9, color=TEXT)
                style(ax)
            fig.suptitle(f"One factor at a time: {factor} ({m.upper()} model, mean ± 95% CI)", fontsize=10,
                         color=TEXT)
            fig.tight_layout()
            fig.savefig(os.path.join(outdir, f"ofat_{m}_{factor}.png"), dpi=150)
            plt.close(fig)


def load_plot(s, outdir):
    order = ["base-none-monitor_only", "base-0.5b-both_default", "base-3b-both_default", "base-7b-both_default"]
    d = s.set_index("config").reindex([o for o in order if o in set(s.config)])
    if len(d) < 2:
        return
    labels = ["no LLM", "0.5B", "3B", "7B"][:len(d)] if len(d) == 4 else list(d.index)
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.6))
    for ax, (col, yl) in zip(axes, [("loss_pct", "event loss (%)"), ("lat_p95_ms", "p95 latency (ms)"),
                                    ("lat_p99_ms", "p99 latency (ms)")]):
        ax.bar(range(len(d)), d[col], yerr=d.get(col + "_ci95"), color="#257de0", width=0.6, capsize=3, ecolor=TEXT,
               edgecolor="white", linewidth=2)
        ax.set_xticks(range(len(d)))
        ax.set_xticklabels(labels, fontsize=8)
        ax.set_title(yl, fontsize=9, color=TEXT)
        style(ax)
    fig.suptitle("Effect of LLM load with default settings (mean ± 95% CI)", fontsize=10, color=TEXT)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "load_effect.png"), dpi=150)
    plt.close(fig)


def overhead(s):
    rows = []
    for m in ("0.5b", "3b", "7b"):
        llm = s[s.config == f"base-{m}-llm_only"]
        both = s[s.config == f"base-{m}-both_default"]
        if len(llm) and len(both):
            a, b = llm.iloc[0], both.iloc[0]
            rows.append({"model": m, "tok_s_llm_only": a.tok_per_s, "tok_s_with_monitor": b.tok_per_s,
                         "llm_slowdown_pct": 100 * (1 - b.tok_per_s / a.tok_per_s),
                         "host_cpu_llm_only_pct": a.cpu_busy_all_pct,
                         "host_cpu_with_monitor_pct": b.cpu_busy_all_pct,
                         "extra_host_cpu_pts": b.cpu_busy_all_pct - a.cpu_busy_all_pct,
                         "mon_cpu_pct_of_one_core": b.get("mon_cpu_pct"),
                         "bpf_ns_per_event": b.get("bpf_ns_per_event"),
                         "mon_maxrss_kb": b.get("mon_maxrss_kb")})
    mo = s[s.config == "base-none-monitor_only"]
    if len(mo):
        r = mo.iloc[0]
        rows.append({"model": "none", "mon_cpu_pct_of_one_core": r.get("mon_cpu_pct"),
                     "bpf_ns_per_event": r.get("bpf_ns_per_event"), "mon_maxrss_kb": r.get("mon_maxrss_kb"),
                     "host_cpu_with_monitor_pct": r.cpu_busy_all_pct})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                      "results"))
    ap.add_argument("--max-loss", type=float, default=1.0, help="limit for the recommendation (%%)")
    ap.add_argument("--max-p95", type=float, default=100.0, help="limit for the recommendation (ms)")
    args = ap.parse_args()
    df = pd.read_csv(os.path.join(args.results, "runs.csv"))
    df = df[df.warmup == 0]
    out = os.path.join(args.results, "analysis")
    os.makedirs(out, exist_ok=True)

    s = aggregate(df)
    s.sort_values(["phase", "model", "config"]).to_csv(os.path.join(out, "summary.csv"), index=False)
    ov = overhead(s)
    ov.to_csv(os.path.join(out, "overhead.csv"), index=False)

    if "lat_p95_ms" in s:
        pareto_plot(s, "loss_pct", "event loss (%)", os.path.join(out, "pareto_loss.png"), args.max_loss)
        pareto_plot(s, "lat_p95_ms", "p95 detection latency (ms)", os.path.join(out, "pareto_p95.png"),
                    args.max_p95)
        ofat_plots(s, out)
        load_plot(s, out)

    lines = []
    show = ["label", "n", "loss_pct", "lat_p95_ms", "lat_p99_ms", "tok_per_s", "mon_cpu_pct"]
    for m in ("3b", "7b", "0.5b"):
        d = s[(s.model == m) & (s.monitor_on == 1) & s.get("tok_per_s", pd.Series(dtype=float)).notna()]
        if not len(d) or "lat_p95_ms" not in d:
            continue
        lines.append(f"\n===== {m.upper()} model =====")
        lines.append("All configurations:\n" + d[show].sort_values("tok_per_s", ascending=False)
                     .to_string(index=False, float_format=lambda x: f"{x:.3f}"))
        front = pd.concat([pareto(d, "loss_pct"), pareto(d, "lat_p95_ms")]).drop_duplicates("config")
        lines.append("\nPareto set (not beaten on both loss/p95 and tokens/s):\n" +
                     front[show].to_string(index=False, float_format=lambda x: f"{x:.3f}"))
        ok = d[(d.loss_pct < args.max_loss) & (d.lat_p95_ms < args.max_p95)]
        if len(ok):
            best = ok.sort_values("tok_per_s", ascending=False).iloc[0]
            lines.append(f"\nRECOMMENDED (loss < {args.max_loss}% and p95 < {args.max_p95} ms, highest tokens/s): "
                         f"{best.config}\n   loss={best.loss_pct:.3f}%  p95={best.lat_p95_ms:.2f} ms  "
                         f"tok/s={best.tok_per_s:.2f}")
        else:
            lines.append(f"\nNo configuration met loss < {args.max_loss}% and p95 < {args.max_p95} ms.")
    if len(ov):
        lines.append("\n===== Monitor overhead =====\n" + ov.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    swapped = df[pd.to_numeric(df.get("swap_out_pages"), errors="coerce").fillna(0) > 0]
    if len(swapped):
        lines.append(f"\nWARNING: {len(swapped)} runs swapped: {', '.join(swapped.run_key)}")
    txt = "\n".join(lines)
    open(os.path.join(out, "recommendation.txt"), "w").write(txt + "\n")
    print(txt)
    print(f"\nwrote {out}/")


if __name__ == "__main__":
    main()
