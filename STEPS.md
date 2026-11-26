# Exact commands, in order

Open **Konsole** on the Fedora laptop. Copy each block, paste it (Ctrl+Shift+V), press Enter.

## 0. Before you start (once)
* Plug in the charger.
* KDE: System Settings > Power Management: set "Suspend session" and "Turn off screen" to **Never**
  while plugged in (the runner also blocks sleep, but this is safer). Do not close the lid.
* Close Firefox and other big apps during the experiment (RAM is only 8 GB).

## 1.  IF IN COMPRESSED FILE Unpack the project
Download `os-llm-ebpf.tar.gz` to `~/Downloads`, then:
```bash
cd ~
tar xzf ~/Downloads/os-llm-ebpf.tar.gz
cd ~/os-llm-ebpf
ls
```
You should see `README.md  STEPS.md  monitor  scripts  setup.sh`.

## 2. Install everything (about 20-40 min, mostly the build and the 6 GB download)
```bash
cd ~/os-llm-ebpf
bash setup.sh 2>&1 | tee setup.log
```
It ends with `Setup finished.` Check that the CPU capacity list shows 4 cores with a bigger number
(the performance cores).

## 3. Self-test: does the monitor see every event? (10 s)
```bash
cd ~/os-llm-ebpf
sudo python3 scripts/runner.py --selftest
```
Expected: `loss_pct=0.0  kernel_dropped=0` and a small `lat_p95_ms` (well under 5 ms).
(also `clock_negative=0` in `results_selftest/runs.csv`).

## 4. Pilot: choose the event rate (about 6 min, uses the 7B model)
```bash
cd ~/os-llm-ebpf
sudo python3 scripts/runner.py --pilot
```
It runs rates 500, 1000, 2000 events/s with the small and medium buffer. Read the printed lines:
* Check `gen_rate_achieved` is close to the asked rate. If 2000 shows e.g. 1400, do not use 2000.
* Pick the **highest rate that the generator reaches and where loss with rb=small is above 0 but
  below about 50%**. If loss is 0 everywhere, use the highest reachable rate. If unsure, keep 1000.
* Also check `swap_out_pages=0`. If it is not 0, close more programs.

## 5. Check the plan and time
```bash
cd ~/os-llm-ebpf
python3 scripts/runner.py --dry-run | head -5
```
Default: 186 runs, about 4.5 h. Short on time? add `--reps 3 --no-warmup` (about 2.3 h) to the
commands below and mention it in the report.

## 6. Full experiment 
Replace 1000 with the rate you chose in step 4:
```bash
cd ~/os-llm-ebpf
sudo systemd-inhibit --what=sleep:idle:handle-lid-switch --why="OS experiment" \
    python3 scripts/runner.py --rate 1000 2>&1 | tee -a run.log
```
Leave the laptop alone. Each run prints one line with loss, p95 latency and tokens/s.
**If anything stops it (crash, reboot, Ctrl+C), run the same command again: finished runs are skipped.**

To watch progress from a second Konsole tab: `tail -f ~/os-llm-ebpf/run.log`

## 7. Analysis and plots (1 min)
```bash
cd ~/os-llm-ebpf
python3 scripts/analyze.py
ls results/analysis
```
Gives `summary.csv`, `overhead.csv`, `recommendation.txt`, `pareto_loss.png`, `pareto_p95.png`,
`load_effect.png` and `ofat_<model>_<factor>.png`. The recommendation limits are 1% loss and
100 ms p95 (change with `--max-loss` / `--max-p95`).

