# Does the AI starve the security monitor?

OS final project (Zainab Sadika, Sarah Faisal, Abdul Rehman). We measure how `nice`, cgroup v2
`cpu.weight`, CPU pinning (`taskset`) and eBPF ring-buffer size change **event loss**, **detection
latency** and **monitor overhead** when a CPU-only local LLM (llama.cpp, Qwen2.5 0.5B/3B/7B)
competes with an eBPF exec monitor, and which setting best balances monitoring against LLM tokens/s.

## What is in here

| Path | What it does |
|---|---|
| `setup.sh` | Installs packages (dnf), builds the monitor and llama.cpp (CPU only), downloads the 3 models |
| `monitor/monitor.bpf.c` | eBPF program on `tracepoint/sched/sched_process_exec`. Writes one event per exec into a **BPF ring buffer**. Counts reserve failures (= kernel drops) |
| `monitor/monitor.c` | User-space reader (libbpf). `--rb-bytes` sets the ring-buffer size. Logs `id, kernel ts, user ts` (CLOCK_MONOTONIC). `--work-us` adds a simulated per-event analysis cost. Writes a JSON summary: kernel drops, CPU time, max RSS, BPF run time (`bpf_enable_stats`) |
| `monitor/evgen.c` | Labeled event generator. Event *i* = `execve("/dev/shm/evl/ev0000000i")` (a symlink to `evtarget`), at a fixed rate. Timestamp taken with CLOCK_MONOTONIC right before `execve` |
| `monitor/evtarget.c` | Tiny static program that exits immediately |
| `scripts/launch.sh` | Starts a command in a cgroup with a nice value and CPU list |
| `scripts/runner.py` | Runs the whole experiment, writes `results/runs.csv` (one row per run). Resumable |
| `scripts/analyze.py` | Mean / 95% CI per configuration, loss, p50/p95/p99, overhead, tokens/s, Pareto plots, recommendation |

## Design decisions (put these in the report's method section)

* **Cores.** The experiment (LLM + monitor) runs on the 4 performance cores (detected from
  `cpu_capacity`, normally CPUs 4-7 on M1). The event generator runs on the 4 efficiency cores so that
  the event rate stays the same in every run. The LLM always uses **4 threads**, which saturates the
  4 experiment cores and creates real contention.
* **Baselines:** monitor+generator alone; LLM alone (per model); both with default settings (per model).
* **One factor at a time** on the 3B (medium) and 7B (heavy) models, everything else at default:

  | Factor | Default | Other levels |
  |---|---|---|
  | nice (monitor, LLM) | 0, 0 | monitor favored (-10, 0); LLM favored (+10, 0) |
  | cpu.weight (monitor, LLM) | both in one cgroup | separate cgroups 100/100 (equal), 1000/100 (monitor favored), 100/1000 (LLM favored) |
  | pinning (monitor CPUs / LLM CPUs) | all 4 / all 4 | partial: 2 / 3 (overlap on 1 core); reserved: 1 / other 3 |
  | ring buffer | 128 KiB | 16 KiB (small), 1 MiB (large) |

  `nice` only competes between tasks **in the same cgroup**, so the nice runs keep both processes in one
  cgroup (this is also the default on a desktop, where everything you start from a terminal shares a
  cgroup). With separate cgroups, `cpu.weight` decides instead. This is worth a sentence in the report.
  Ring-buffer sizes must be a power of 2 and a multiple of the page size (16 KiB on Asahi).
* **Combinations:** 3 combos on 3B and 7B (`COMBOS` at the top of `runner.py`).
* **Each run:** start llama-server with its settings, 16-token warm-up request, start monitor, start
  the LLM client (streams 256-token requests back to back, greedy, fixed prompt), 3 s settle, then a
  **60 s window** with the generator at **1000 events/s**, then a **5 s drain** (generator stopped,
  monitor still reading). Tokens/s = tokens streamed inside the window / window length.
* **Repetitions:** 1 warm-up run per configuration (discarded) + **5 measured runs**. Runs are grouped
  by model (so the 8 GB of RAM never has to hold all three models) and shuffled inside each group.
* **Total:** 31 configurations, 186 runs, about **4.5 hours**. Use `--reps 3 --no-warmup` for about 2.3 h.
* **Loss** = generated IDs that never reached user space / generated. The kernel drop counter is
  recorded too (`kernel_dropped`). **Latency** = user-space receive time minus generator timestamp, same
  clock (`clock_negative` must be 0). **Overhead** = monitor CPU (% of one core), BPF ns/event,
  monitor max RSS, extra host CPU and LLM slowdown versus the LLM-only baseline.
* `--work-us 50` simulates 50 µs of analysis per event (rule matching, logging) like a real monitor.
  Without it the reader does almost nothing and never falls behind.
* Swap is checked on every run (`swap_out_pages`); the runner prints a warning if it is not 0.

## Step by step

See `STEPS.md` for the exact commands.
