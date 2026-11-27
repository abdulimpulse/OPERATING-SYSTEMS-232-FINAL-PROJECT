# Does the AI starve the security monitor?

Operating Systems final project (Zainab Sadika, Sarah Faisal, Abdul Rehman).

When a CPU-only local LLM (llama.cpp, Qwen2.5 0.5B / 3B / 7B) and an eBPF security monitor compete for
the CPU, how do `nice`, cgroup v2 `cpu.weight`, CPU pinning (`taskset`) and the eBPF ring-buffer size
change **event loss**, **detection latency** and **monitor overhead**? Which setting best balances
monitoring against LLM tokens/s?

**Short answer from our data:** give the monitor its own CPU core. It lost no events, cut p95 delay from
5.2 ms to 0.35 ms, and made the LLM 23-78% faster.

## What is in this repository

| Path | What it is |
|---|---|
| `OS-Project-report.pdf` | The final report |
| `OS-Project-ppt.pdf` | The presentation slides |
| `setup.sh` | Installs packages (dnf), builds the monitor and llama.cpp (CPU only), downloads the 3 models |
| `STEPS.md` | The exact commands, in order, to reproduce everything |
| `monitor/monitor.bpf.c` | eBPF program on `tracepoint/sched/sched_process_exec`. Puts one event per exec into a BPF ring buffer and counts reserve failures (= kernel drops) |
| `monitor/monitor.c`, `monitor.h` | User-space reader (libbpf). `--rb-bytes` sets the buffer size, `--work-us` adds a simulated per-event analysis cost. Logs `id, kernel timestamp, user timestamp` and writes a JSON summary (kernel drops, CPU time, memory, BPF run time) |
| `monitor/evgen.c` | Labelled event generator. Event *i* runs `/dev/shm/evl/ev0000000i` (a symlink to `evtarget`) at a fixed rate, timestamped with CLOCK_MONOTONIC just before `execve` |
| `monitor/evtarget.c` | Tiny static program that exits immediately |
| `monitor/Makefile` | Builds the monitor, generator and target |
| `scripts/launch.sh` | Starts a command in a cgroup with a nice value and CPU list |
| `scripts/runner.py` | Runs the experiment, one row per run in `<out>/runs.csv`. Resumable. Phases: `base`, `ofat`, `combo`, `loss` |
| `scripts/analyze.py` | Mean and 95% CI per configuration, Pareto front, recommendation, plots |
| `scripts/report_figures.py` | Report-ready figures and tables |
| `results/runs1.csv` | **Night run 1** results: 186 runs (31 configurations x 6), main experiment |
| `results/runs2.csv` | **Night run 2** results: 48 runs (8 configurations x 6), loss experiment (7B only) |
| `results/run1.log`, `run_loss2.log` | Console logs of the two runs |

## Requirements

Native Linux (we used Fedora Asahi Remix 45, kernel 7.3.4, 8 GB RAM, Apple M1) with root access,
cgroup v2, BTF (`/sys/kernel/btf/vmlinux`), and about 8 GB of free disk. A text console
(no desktop) is recommended so the machine never swaps. Everything else is installed by `setup.sh`.

## How to run (short version; see `STEPS.md` for every detail)

```bash
git clone https://github.com/abdulimpulse/OPERATING-SYSTEMS-232-FINAL-PROJECT
cd OPERATING-SYSTEMS-232-FINAL-PROJECT
bash setup.sh 2>&1 | tee setup.log                      # install, build, download models (20-40 min)

sudo python3 scripts/runner.py --selftest               # 10 s check: expect loss_pct=0, kernel_dropped=0
python3 scripts/runner.py --dry-run                     # show the plan: 186 runs, about 4.4 h

# Night run 1: main experiment (about 4 h). Safe to stop and restart: finished runs are skipped.
sudo systemd-inhibit --what=sleep:idle:handle-lid-switch --why="OS experiment" \
    python3 scripts/runner.py --rate 2000 --work-us 400 --out results

# Night run 2: loss experiment (about 1 h, 48 runs)
sudo python3 scripts/runner.py --phases loss --rate 2000 --work-us 400 --out results_loss

# Analysis and figures
python3 scripts/analyze.py --results results
python3 scripts/report_figures.py --results results --loss-results results_loss
```

To analyse our own data without re-running, copy the CSVs into the folders the scripts expect:

```bash
mkdir -p results results_loss
cp results/runs1.csv results/runs.csv
cp results/runs2.csv results_loss/runs.csv
python3 scripts/analyze.py --results results
python3 scripts/report_figures.py --results results --loss-results results_loss
```

Outputs go to `results/analysis/` (`summary.csv`, `overhead.csv`, `recommendation.txt`, Pareto plots)
and `results/figures/`.

## Experiment design

* **Cores.** The LLM and the monitor share the 4 performance cores (CPUs 4-7 on the M1, detected from
  `cpu_capacity`). The generator runs on the 4 efficiency cores so the event rate never changes. The LLM
  always uses 4 threads, which saturates the 4 experiment cores and creates real contention.
* **Events.** 2,000 labelled exec events per second, a 60 s window (120,000 events per run), then a 5 s
  drain. The reader does 400 us of simulated analysis per event (`--work-us 400`).
* **Settings tested** (one at a time on the 3B and 7B models, everything else default):

  | Factor | Default | Other levels |
  |---|---|---|
  | nice (monitor, LLM) | 0, 0 | monitor favoured (-10, 0); LLM favoured (+10, 0) |
  | cpu.weight | both in one cgroup | separate cgroups 100:100, 1000:100 (monitor favoured), 100:1000 (LLM favoured) |
  | pinning | all 4 cores | partial (overlap on 1 core); reserved (monitor alone on 1 core) |
  | ring buffer | 128 KiB | 16 KiB, 1 MiB |

  `nice` only competes between tasks in the same cgroup, so the nice runs keep both programs in one cgroup.
* **Baselines:** monitor alone, LLM alone, and both at default settings, per model.
* **Combinations:** 3 combinations (`COMBOS` at the top of `runner.py`) on 3B and 7B.
* **Loss phase:** 7B only. An *overloaded* reader (600 us per event, needs 1.2 cores) and a *starved* reader
  (400 us, monitor at nice +10), each with the three buffer sizes and with a reserved core.
* **Repetitions:** 1 warm-up run (discarded) + 5 measured runs per configuration, shuffled inside each
  model group (seed 42).
* **Metrics.** *Loss* = generated IDs that never reached user space. *Latency* = user receive time minus
  generator time (same clock; `clock_negative` must be 0). *Overhead* = monitor CPU, BPF ns/event,
  memory, LLM slowdown versus LLM alone. Swap is checked on every run.
* **Data checks.** In every run the kernel drop counter equalled the number of missing IDs.

## Results at a glance (7B model)

| Setting | p95 delay | LLM tokens/s | Events lost |
|---|---|---|---|
| Default | 5.24 ms | 2.95 | 0% |
| Monitor nice -10 | 3.25 ms | 2.94 | 0% |
| Monitor nice +10 (LLM favoured) | 41.4 ms | 3.00 | 0% (0.044% with a 16 KiB buffer) |
| **Reserved core for the monitor** | **0.35 ms** | **3.64** | **0%** |

With an overloaded reader (600 us per event) every buffer overflowed; a bigger buffer only delayed the
first lost event (0.4 s with 16 KiB, 27.9 s with 1 MiB).
