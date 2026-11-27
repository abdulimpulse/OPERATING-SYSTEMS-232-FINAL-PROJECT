# Exact commands, in order

These are the steps we used to produce the results in the report. Copy each block into a terminal and
press Enter. Everything runs on native Linux (we used Fedora Asahi Remix 45 on a MacBook Air M1, 8 GB RAM).

## 0. Before you start (once)
* Plug in the charger and do not close the lid.
* **Use a text-only console, not the desktop.** The desktop uses about 2 GB of RAM, which made the 7B
  model swap in our first pilot, and swap would distort the timing results. To switch (then reboot):
  ```bash
  sudo systemctl set-default multi-user.target
  sudo reboot
  ```
  Log in on the text console. To go back to the desktop later:
  `sudo systemctl set-default graphical.target && sudo reboot`.
* Check free memory: `free -m`. "available" should be about 6000 MB and swap used should be 0.

## 1. Get the project
```bash
cd ~
git clone https://github.com/abdulimpulse/OPERATING-SYSTEMS-232-FINAL-PROJECT os-llm-ebpf
cd ~/os-llm-ebpf
ls
```
You should see `README.md  STEPS.md  monitor  results  scripts  setup.sh` and the two PDFs.

## 2. Install everything (about 20-40 min, mostly the build and the 6 GB model download)
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
Also `clock_negative` must be 0 in `results_selftest/runs.csv`.

## 4. Pilot: choose the event rate (optional, about 6 min, uses the 7B model)
We ran this once to choose the settings below. You can skip it and use our values.
```bash
cd ~/os-llm-ebpf
sudo python3 scripts/runner.py --pilot
```
It runs 500, 1000 and 2000 events/s with the small and medium buffer. Check that `gen_rate_achieved` is
close to the asked rate and that `swap_out_pages=0`.
**Our choice: 2000 events/s with `--work-us 400`.** With only 50 us of work per event the reader was too
light for the AI to starve it, so nothing ever changed between settings.

## 5. Check the plan and time
```bash
cd ~/os-llm-ebpf
python3 scripts/runner.py --dry-run | head -5
```
Default plan: 186 runs, about 4.4 h. Short on time? Add `--reps 3 --no-warmup` (about 2.3 h) to the
commands below and say so in your report.

## 6. Night run 1: the main experiment (about 4 hours, 186 runs)
```bash
cd ~/os-llm-ebpf
sudo systemd-inhibit --what=sleep:idle:handle-lid-switch --why="OS experiment" \
    python3 scripts/runner.py --rate 2000 --work-us 400 2>&1 | tee -a run.log
```
Leave the laptop alone. Each run prints one line with loss, p95 latency and tokens/s. Results go to
`results/runs.csv`.
**If anything stops it (crash, reboot, Ctrl+C), run the same command again: finished runs are skipped.**
To watch progress from a second terminal: `tail -f ~/os-llm-ebpf/run.log`

## 7. Night run 2: the loss experiment (about 1 hour, 48 runs, 7B model only)
Night run 1 never lost an event, so this run pushes the reader past its limit on purpose: an overloaded
reader (600 us of work per event) and a starved reader (monitor at nice +10), each with the three buffer
sizes and with a reserved monitor core.
```bash
cd ~/os-llm-ebpf
sudo systemd-inhibit --what=sleep:idle:handle-lid-switch --why="OS experiment" \
    python3 scripts/runner.py --phases loss --rate 2000 --work-us 400 --out results_loss 2>&1 | tee -a run_loss.log
```
Results go to `results_loss/runs.csv`. It is resumable in the same way.

## 8. Analysis and plots (1 min)
```bash
cd ~/os-llm-ebpf
python3 scripts/analyze.py --results results
python3 scripts/report_figures.py --results results --loss-results results_loss
ls results/analysis results/figures
```
This gives `summary.csv`, `overhead.csv`, `recommendation.txt`, `pareto_loss.png`, `pareto_p95.png`,
`load_effect.png`, `ofat_<model>_<factor>.png`, and the report figures and tables. The recommendation
limits are 1% loss and 100 ms p95 (change with `--max-loss` / `--max-p95`).

## Only want to look at our results, without re-running?
The repository already contains our two result files (`results/runs1.csv` and `results/runs2.csv`).
The analysis scripts read `runs.csv` inside a results folder, so copy them first:
```bash
cd ~/os-llm-ebpf
mkdir -p results_loss
cp results/runs1.csv results/runs.csv
cp results/runs2.csv results_loss/runs.csv
python3 scripts/analyze.py --results results
python3 scripts/report_figures.py --results results --loss-results results_loss
```
(`pip install pandas matplotlib` first if they are missing.)
