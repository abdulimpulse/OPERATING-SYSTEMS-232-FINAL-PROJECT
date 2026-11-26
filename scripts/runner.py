#!/usr/bin/env python3
"""
Experiment runner: LLM (llama-server) vs eBPF exec monitor.

Run as root from the repository root:
    sudo python3 scripts/runner.py --selftest            # 5 s sanity check, no LLM
    sudo python3 scripts/runner.py --pilot               # ~6 min, helps choose --rate
    sudo python3 scripts/runner.py --dry-run             # print the plan and time estimate
    sudo python3 scripts/runner.py                       # the full experiment (resumable)

Each measured run appends one row to results/runs.csv. Raw files go to results/raw/<run_key>/.
If the script is interrupted, just start it again: finished runs are skipped.
Only the Python standard library is used here.
"""
import argparse
import csv
import http.client
import json
import os
import platform
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MON_DIR = os.path.join(ROOT, "monitor")
LAUNCH = os.path.join(ROOT, "scripts", "launch.sh")
LLAMA_SERVER = os.path.join(ROOT, "llama.cpp", "build", "bin", "llama-server")
MODELS = {
    "0.5b": os.path.join(ROOT, "models", "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf"),
    "3b": os.path.join(ROOT, "models", "Qwen2.5-3B-Instruct-Q4_K_M.gguf"),
    "7b": os.path.join(ROOT, "models", "Qwen2.5-7B-Instruct-Q3_K_M.gguf"),
}
CG = "/sys/fs/cgroup/osexp"
PORT = 8089
LINKDIR = "/dev/shm/evl"

# ------------------------------------------------------------------ fixed settings
LLM_THREADS = 4          # fixed thread count for every LLM run (= number of experiment cores)
N_PREDICT = 256
PROMPT = ("You are a helpful assistant. Explain, step by step and in detail, how an operating "
          "system scheduler decides which process runs next, and give a short example.")
SETTLE_S = 3             # LLM generating for this long before the measurement window opens

# ------------------------------------------------------------------ factor levels
RB = {"small": 16 * 1024, "medium": 128 * 1024, "large": 1024 * 1024}
# nice values (monitor, llm). Both processes share ONE cgroup in these runs, because nice only
# competes against tasks of the same cgroup.
NICE = {"default": (0, 0), "mon_favored": (-10, 0), "llm_favored": (10, 0)}
# cgroup cpu.weight (monitor, llm). "shared" = both in the same cgroup (the default setup).
WEIGHT = {"shared": None, "equal": (100, 100), "mon_favored": (1000, 100), "llm_favored": (100, 1000)}
PINS = ("shared", "partial", "reserved")   # CPU lists are computed from the detected cores
BASE = {"nice": "default", "weight": "shared", "pin": "shared", "rb": "medium"}

OFAT_MODELS = ["3b", "7b"]                 # medium and heavy load
COMBO_MODELS = ["3b", "7b"]
# Promising combinations. Edit these after looking at the one-factor results if you have time.
# Do not combine a nice level with a separate-cgroup weight level: nice has no effect across cgroups.
COMBOS = [
    {"name": "c1", "nice": "default", "weight": "mon_favored", "pin": "shared", "rb": "large"},
    {"name": "c2", "nice": "mon_favored", "weight": "shared", "pin": "partial", "rb": "large"},
    {"name": "c3", "nice": "default", "weight": "mon_favored", "pin": "partial", "rb": "medium"},
]


# ------------------------------------------------------------------ helpers
def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def write(path, val):
    with open(path, "w") as f:
        f.write(str(val))


def parse_cpulist(s):
    out = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def cpulist(cpus):
    return ",".join(str(c) for c in cpus)


def detect_cpus():
    """Experiment cores = the big (P) cores; generator = the little (E) cores."""
    online = parse_cpulist(open("/sys/devices/system/cpu/online").read().strip())
    cap = {}
    for c in online:
        p = f"/sys/devices/system/cpu/cpu{c}/cpu_capacity"
        cap[c] = int(open(p).read()) if os.path.exists(p) else 0
    top = max(cap.values())
    exp = [c for c in online if cap[c] == top]
    rest = [c for c in online if cap[c] != top]
    if not rest:  # all cores identical: use the upper half for the experiment
        half = len(online) // 2
        exp, rest = online[half:], online[:half]
    return exp, rest, cap


def pin_sets(pin, exp):
    a = exp
    if pin == "shared":
        return a, a                              # (monitor cpus, llm cpus)
    if pin == "partial":
        return a[-2:], a[:-1]                    # overlap on one core
    if pin == "reserved":
        return a[-1:], a[:-1]                    # one core only for the monitor
    raise ValueError(pin)


def setup_cgroups(strict=True):
    if not os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
        sys.exit("cgroup v2 not mounted at /sys/fs/cgroup")
    has_cpu = "cpu" in open("/sys/fs/cgroup/cgroup.controllers").read().split()
    if not has_cpu:
        if strict:
            sys.exit("the cgroup v2 'cpu' controller is not available")
        print("WARNING: no cgroup cpu controller (ok for --selftest only)")
    os.makedirs(CG, exist_ok=True)
    if has_cpu:
        if "cpu" not in open("/sys/fs/cgroup/cgroup.subtree_control").read().split():
            write("/sys/fs/cgroup/cgroup.subtree_control", "+cpu")
        if "cpu" not in open(f"{CG}/cgroup.subtree_control").read().split():
            write(f"{CG}/cgroup.subtree_control", "+cpu")
    for sub in ("work", "mon", "llm", "gen"):
        os.makedirs(f"{CG}/{sub}", exist_ok=True)
        if has_cpu:
            write(f"{CG}/{sub}/cpu.weight", 100)


def read_proc_stat():
    """Returns {cpu_index: (busy_jiffies, total_jiffies)}."""
    out = {}
    for line in open("/proc/stat"):
        m = re.match(r"cpu(\d+) (.*)", line)
        if m:
            v = [int(x) for x in m.group(2).split()]
            idle = v[3] + v[4]
            out[int(m.group(1))] = (sum(v[:8]) - idle, sum(v[:8]))
    return out


def busy_pct(s0, s1, cpus):
    b = sum(s1[c][0] - s0[c][0] for c in cpus)
    t = sum(s1[c][1] - s0[c][1] for c in cpus)
    return 100.0 * b / t if t else float("nan")


def vmstat(key):
    for line in open("/proc/vmstat"):
        k, v = line.split()
        if k == key:
            return int(v)
    return 0


def meminfo(key):
    for line in open("/proc/meminfo"):
        if line.startswith(key + ":"):
            return int(line.split()[1])
    return 0


def pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


# ------------------------------------------------------------------ LLM
class LLMClient(threading.Thread):
    """Sends fixed streaming requests back to back and timestamps every token."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.token_ts = []
        self.resp_s = []          # wall time of completed requests (N_PREDICT tokens)
        self.tps_server = []      # server-reported predicted_per_second of completed requests
        self.errors = 0

    def run(self):
        body = json.dumps({"prompt": PROMPT, "n_predict": N_PREDICT, "stream": True,
                           "temperature": 0, "ignore_eos": True, "cache_prompt": True})
        while not self.stop.is_set():
            try:
                conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=600)
                t0 = time.monotonic()
                conn.request("POST", "/completion", body, {"Content-Type": "application/json"})
                r = conn.getresponse()
                done = False
                while not self.stop.is_set():
                    line = r.readline()
                    if not line:
                        break
                    if not line.startswith(b"data: "):
                        continue
                    d = json.loads(line[6:])
                    if d.get("stop"):
                        done = True
                        t = d.get("timings") or {}
                        if "predicted_per_second" in t:
                            self.tps_server.append(t["predicted_per_second"])
                        break
                    # one streamed chunk normally carries one generated token
                    n = len(d.get("tokens") or []) or (1 if d.get("content") else 0)
                    now = time.monotonic_ns()
                    self.token_ts.extend([now] * n)
                if done:
                    self.resp_s.append(time.monotonic() - t0)
                conn.close()
            except Exception:
                self.errors += 1
                time.sleep(0.5)


def start_server(model, nice, cpus, cg, log):
    cmd = [LAUNCH, cg, str(nice), cpulist(cpus), LLAMA_SERVER, "-m", MODELS[model],
           "-t", str(LLM_THREADS), "-tb", str(LLM_THREADS), "-c", "2048", "-np", "1",
           "--host", "127.0.0.1", "--port", str(PORT)]
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + 300
    while time.time() < deadline:
        if p.poll() is not None:
            raise RuntimeError("llama-server exited, see " + log.name)
        try:
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
            c.request("GET", "/health")
            if c.getresponse().status == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        raise RuntimeError("llama-server did not become healthy")
    # short warm-up request (loads weights into cache)
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=600)
    c.request("POST", "/completion", json.dumps({"prompt": PROMPT, "n_predict": 16, "temperature": 0}),
              {"Content-Type": "application/json"})
    c.getresponse().read()
    return p


def kill_group(p, sig=signal.SIGTERM, wait=15):
    if p is None or p.poll() is not None:
        return
    try:
        os.killpg(p.pid, sig)
    except ProcessLookupError:
        return
    try:
        p.wait(wait)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()


# ------------------------------------------------------------------ one run
def run_one(cfg, rep, args, exp, gen_cpus, rawroot):
    key = f"{cfg['name']}__r{rep}"
    d = os.path.join(rawroot, key)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    mon_cpus, llm_cpus = pin_sets(cfg["pin"], exp)
    mon_nice, llm_nice = NICE[cfg["nice"]]
    w = WEIGHT[cfg["weight"]]
    if w is None:
        mon_cg = llm_cg = f"{CG}/work"
    else:
        mon_cg, llm_cg = f"{CG}/mon", f"{CG}/llm"
        write(f"{CG}/mon/cpu.weight", w[0])
        write(f"{CG}/llm/cpu.weight", w[1])

    swap0, pswpout0, swapfree0 = vmstat("pswpin"), vmstat("pswpout"), meminfo("SwapFree")
    server = monitor = client = None
    row = {"run_key": key, "config": cfg["name"], "phase": cfg["phase"], "model": cfg["model"],
           "monitor_on": int(cfg["mon"]), "gen_on": int(cfg["gen"]), "nice": cfg["nice"],
           "weight": cfg["weight"], "pin": cfg["pin"], "rb": cfg["rb"], "rb_bytes": RB[cfg["rb"]],
           "rep": rep, "warmup": int(rep == 0), "rate": args.rate, "duration_s": args.duration,
           "work_us": args.work_us, "mon_cpus": cpulist(mon_cpus), "llm_cpus": cpulist(llm_cpus),
           "mon_nice": mon_nice, "llm_nice": llm_nice,
           "mon_weight": w[0] if w else "", "llm_weight": w[1] if w else "",
           "start_time": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        if cfg["model"] != "none":
            server = start_server(cfg["model"], llm_nice, llm_cpus, llm_cg, open(f"{d}/llama.log", "w"))
        if cfg["mon"]:
            monitor = subprocess.Popen(
                [LAUNCH, mon_cg, str(mon_nice), cpulist(mon_cpus), f"{MON_DIR}/monitor",
                 "--rb-bytes", str(RB[cfg["rb"]]), "--out", f"{d}/events.csv",
                 "--summary", f"{d}/monitor.json", "--work-us", str(args.work_us)],
                stderr=subprocess.PIPE, text=True)
            line = monitor.stderr.readline()
            if "READY" not in line:
                raise RuntimeError("monitor failed: " + line + monitor.stderr.read())
        if server:
            client = LLMClient()
            client.start()
            time.sleep(SETTLE_S)

        st0 = read_proc_stat()
        t_win0 = time.monotonic_ns()
        if cfg["gen"]:
            g = subprocess.run(
                [LAUNCH, f"{CG}/gen", "0", cpulist(gen_cpus), f"{MON_DIR}/evgen",
                 "--rate", str(args.rate), "--duration", str(args.duration),
                 "--target", f"{MON_DIR}/evtarget", "--linkdir", LINKDIR, "--out", f"{d}/gen.csv"],
                capture_output=True, text=True)
            m = re.search(r"sent=(\d+).*achieved_rate=([\d.]+)", g.stderr)
            row["gen_sent"] = int(m.group(1)) if m else 0
            row["gen_rate_achieved"] = float(m.group(2)) if m else 0
        else:
            time.sleep(args.duration)
        t_win1 = time.monotonic_ns()
        st1 = read_proc_stat()
        win_s = (t_win1 - t_win0) / 1e9
        row["window_s"] = round(win_s, 3)
        row["cpu_busy_exp_pct"] = round(busy_pct(st0, st1, exp), 2)
        row["cpu_busy_all_pct"] = round(busy_pct(st0, st1, list(st0.keys())), 2)

        if cfg["mon"]:
            time.sleep(args.drain)               # drain: generator stopped, monitor keeps reading
            monitor.send_signal(signal.SIGINT)
            monitor.wait(30)
        if client:
            client.stop.set()
            toks = [t for t in client.token_ts if t_win0 <= t <= t_win1]
            row["llm_tokens_in_window"] = len(toks)
            row["tok_per_s"] = round(len(toks) / win_s, 3)
            row["llm_resp_s_mean"] = round(sum(client.resp_s) / len(client.resp_s), 3) if client.resp_s else ""
            row["llm_tps_server_mean"] = (round(sum(client.tps_server) / len(client.tps_server), 3)
                                          if client.tps_server else "")
            row["llm_requests_done"] = len(client.resp_s)
            row["llm_errors"] = client.errors
    finally:
        if monitor and monitor.poll() is None:
            monitor.kill()
        kill_group(server)
        if client:
            client.stop.set()
            client.join(5)

    row["swap_in_pages"] = vmstat("pswpin") - swap0
    row["swap_out_pages"] = vmstat("pswpout") - pswpout0
    row["swapfree_drop_kb"] = swapfree0 - meminfo("SwapFree")
    if cfg["mon"]:
        row.update(monitor_metrics(d, cfg["gen"]))
    return row


def monitor_metrics(d, gen_on):
    out = {}
    s = json.load(open(f"{d}/monitor.json"))
    out.update({"kernel_seen": s["kernel_seen"], "kernel_dropped": s["kernel_dropped"],
                "user_received": s["user_received"], "mon_maxrss_kb": s["maxrss_kb"]})
    cpu_s = s["utime_s"] + s["stime_s"]
    out["mon_cpu_s"] = round(cpu_s, 4)
    out["mon_cpu_pct"] = round(100 * cpu_s / s["wall_s"], 3) if s["wall_s"] else ""
    out["bpf_ns_per_event"] = round(s["bpf_run_time_ns"] / s["bpf_run_cnt"], 1) if s["bpf_run_cnt"] else ""
    out["bpf_cpu_pct"] = round(100 * s["bpf_run_time_ns"] / 1e9 / s["wall_s"], 4) if s["wall_s"] else ""
    if not gen_on:
        return out
    gen = {}
    with open(f"{d}/gen.csv") as f:
        next(f)
        for line in f:
            i, t = line.split(",")
            gen[int(i)] = int(t)
    seen = {}
    with open(f"{d}/events.csv") as f:
        next(f)
        for line in f:
            i, k, u, _ = line.split(",")
            i = int(i)
            if i >= 0 and i in gen and i not in seen:
                seen[i] = (int(k), int(u))
    lat = sorted((u - gen[i]) / 1e6 for i, (k, u) in seen.items())          # gen -> user (ms)
    buf = sorted((u - k) / 1e6 for i, (k, u) in seen.items())               # kernel -> user (ms)
    n = len(gen)
    out["events_generated"] = n
    out["events_matched"] = len(seen)
    out["loss_pct"] = round(100.0 * (n - len(seen)) / n, 4) if n else ""
    for p in (50, 95, 99):
        out[f"lat_p{p}_ms"] = round(pct(lat, p), 4)
    out["lat_max_ms"] = round(lat[-1], 4) if lat else ""
    out["buf_p50_ms"] = round(pct(buf, 50), 4)
    out["buf_p99_ms"] = round(pct(buf, 99), 4)
    missing = [i for i in gen if i not in seen]
    t0 = min(gen.values()) if gen else 0
    out["first_loss_s"] = round((gen[min(missing)] - t0) / 1e9, 3) if missing else ""
    neg = sum(1 for x in lat if x < 0)
    out["clock_negative"] = neg      # should be 0: proves both sides use the same clock
    return out


# ------------------------------------------------------------------ plan
def make_cfg(phase, model, mon, gen, f, name=None):
    f = {**BASE, **f}
    nm = name or f"{phase}-{model}-" + "-".join(f"{k}={f[k]}" for k in ("nice", "weight", "pin", "rb"))
    return {"name": nm, "phase": phase, "model": model, "mon": mon, "gen": gen, **f}


def build_plan(phases):
    cfgs = []
    if "base" in phases:
        cfgs.append(make_cfg("base", "none", True, True, {}, "base-none-monitor_only"))
        for m in ("0.5b", "3b", "7b"):
            cfgs.append(make_cfg("base", m, False, False, {}, f"base-{m}-llm_only"))
            cfgs.append(make_cfg("base", m, True, True, {}, f"base-{m}-both_default"))
    if "ofat" in phases:
        for m in OFAT_MODELS:
            for lvl in NICE:
                if lvl != BASE["nice"]:
                    cfgs.append(make_cfg("ofat", m, True, True, {"nice": lvl}))
            for lvl in WEIGHT:
                if lvl != BASE["weight"]:
                    cfgs.append(make_cfg("ofat", m, True, True, {"weight": lvl}))
            for lvl in PINS:
                if lvl != BASE["pin"]:
                    cfgs.append(make_cfg("ofat", m, True, True, {"pin": lvl}))
            for lvl in RB:
                if lvl != BASE["rb"]:
                    cfgs.append(make_cfg("ofat", m, True, True, {"rb": lvl}))
    if "combo" in phases:
        for m in COMBO_MODELS:
            for c in COMBOS:
                f = {k: c[k] for k in ("nice", "weight", "pin", "rb")}
                cfgs.append(make_cfg("combo", m, True, True, f, f"combo-{m}-{c['name']}-" +
                                     "-".join(f"{k}={f[k]}" for k in f)))
    if "loss" in phases:
        # Extra phase: push the reader past its limit so that events are dropped.
        # L1 "overloaded reader": 600 us per event at 2000/s needs 1.2 cores, more than one reader can give.
        # L2 "starved reader": normal 400 us work, but the LLM is favoured with nice.
        for rb in RB:
            c = make_cfg("loss", "7b", True, True, {"rb": rb}, f"loss-7b-overloaded-work600-rb={rb}")
            c["work_us"] = 600
            cfgs.append(c)
            cfgs.append(make_cfg("loss", "7b", True, True, {"nice": "llm_favored", "rb": rb},
                                 f"loss-7b-starved-nice=llm_favored-rb={rb}"))
        # Does the recommended setting (a reserved monitor core) prevent the loss in each scenario?
        c = make_cfg("loss", "7b", True, True, {"pin": "reserved"}, "loss-7b-overloaded-work600-pin=reserved")
        c["work_us"] = 600
        cfgs.append(c)
        cfgs.append(make_cfg("loss", "7b", True, True, {"nice": "llm_favored", "pin": "reserved"},
                             "loss-7b-starved-nice=llm_favored-pin=reserved"))
    return cfgs


def order_runs(cfgs, reps, warmup, seed):
    """Group by model (avoids reloading 3 models through 8 GB of RAM), shuffle inside each group.
    Inside a group: one warm-up run per configuration first, then all measured reps shuffled."""
    rnd = random.Random(seed)
    models = sorted({c["model"] for c in cfgs})
    rnd.shuffle(models)
    runs = []
    for m in models:
        group = [c for c in cfgs if c["model"] == m]
        w = [(c, 0) for c in group] if warmup else []
        r = [(c, i) for c in group for i in range(1, reps + 1)]
        rnd.shuffle(w)
        rnd.shuffle(r)
        runs += w + r
    return runs


def write_meta(path, exp, gen_cpus, cap, args):
    with open(path, "w") as f:
        f.write(f"date: {time.ctime()}\nuname: {' '.join(platform.uname())}\n")
        f.write(f"page_size: {os.sysconf('SC_PAGE_SIZE')}\n")
        f.write(f"experiment_cpus: {exp}\ngenerator_cpus: {gen_cpus}\ncpu_capacity: {cap}\n")
        f.write(f"args: {vars(args)}\nLLM_THREADS: {LLM_THREADS}\nN_PREDICT: {N_PREDICT}\n")
        f.write(f"RB: {RB}\nNICE: {NICE}\nWEIGHT: {WEIGHT}\nCOMBOS: {COMBOS}\n")
        f.write("llama.cpp commit: " + sh(f"git -C {ROOT}/llama.cpp rev-parse HEAD") + "\n")
        f.write("models:\n" + sh(f"ls -l {ROOT}/models") + "\n")
        f.write("libbpf: " + sh("rpm -q libbpf") + "\nclang: " + sh("clang --version | head -1") + "\n")
        f.write("sched: " + sh("cat /sys/kernel/debug/sched/features 2>/dev/null | head -c 300") + "\n")
        f.write("lscpu:\n" + sh("lscpu") + "\n")
        f.write("free:\n" + sh("free -m") + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phases", default="base,ofat,combo")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--no-warmup", action="store_true", help="skip the per-config warm-up run")
    ap.add_argument("--duration", type=float, default=60)
    ap.add_argument("--drain", type=float, default=5)
    ap.add_argument("--rate", type=float, default=1000, help="labeled events per second")
    ap.add_argument("--work-us", type=float, default=50, help="simulated per-event analysis cost")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=os.path.join(ROOT, "results"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--pilot", action="store_true")
    args = ap.parse_args()

    exp, gen_cpus, cap = detect_cpus()
    if args.selftest:
        args.duration, args.out = 5, os.path.join(ROOT, "results_selftest")
        runs = [(make_cfg("selftest", "none", True, True, {}, "selftest"), 1)]
    elif args.pilot:
        args.out, args.duration = os.path.join(ROOT, "results_pilot"), 20
        runs = []
        for rate in (500, 1000, 2000):
            for rb in ("small", "medium"):
                c = make_cfg("pilot", "7b", True, True, {"rb": rb}, f"pilot-7b-rate={rate}-rb={rb}")
                c["rate"] = rate
                runs.append((c, 1))
    else:
        runs = order_runs(build_plan(args.phases.split(",")), args.reps, not args.no_warmup, args.seed)

    per_run = args.duration + args.drain + 20
    print(f"experiment cpus {exp}, generator cpus {gen_cpus}; {len(runs)} runs, "
          f"about {len(runs) * per_run / 3600:.1f} h")
    if args.dry_run:
        for c, r in runs:
            print(f"  {c['name']}  rep={r}")
        return
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    for need in ([f"{MON_DIR}/monitor", f"{MON_DIR}/evgen", f"{MON_DIR}/evtarget"] +
                 ([LLAMA_SERVER] if any(c["model"] != "none" for c, _ in runs) else [])):
        if not os.path.exists(need):
            sys.exit(f"missing {need} - run setup.sh first")
    for m in {c["model"] for c, _ in runs} - {"none"}:
        if not os.path.exists(MODELS[m]):
            sys.exit(f"missing model {MODELS[m]}")

    os.chmod(LAUNCH, 0o755)
    setup_cgroups(strict=not args.selftest)
    os.makedirs(os.path.join(args.out, "raw"), exist_ok=True)
    write_meta(os.path.join(args.out, "meta.txt"), exp, gen_cpus, cap, args)
    csv_path = os.path.join(args.out, "runs.csv")
    done = set()
    fields = None
    if os.path.exists(csv_path):
        with open(csv_path) as f:
            rd = csv.DictReader(f)
            fields = rd.fieldnames
            done = {r["run_key"] for r in rd}

    base_rate, base_work = args.rate, args.work_us
    for i, (cfg, rep) in enumerate(runs, 1):
        key = f"{cfg['name']}__r{rep}"
        if key in done:
            continue
        args.rate = cfg.get("rate", base_rate)
        args.work_us = cfg.get("work_us", base_work)
        print(f"[{i}/{len(runs)}] {time.strftime('%H:%M:%S')} {key}", flush=True)
        try:
            row = run_one(cfg, rep, args, exp, gen_cpus, os.path.join(args.out, "raw"))
        except Exception as e:
            print(f"   FAILED: {e}", flush=True)
            continue
        msg = []
        for k in ("gen_rate_achieved", "loss_pct", "kernel_dropped", "first_loss_s", "lat_p95_ms", "tok_per_s", "mon_cpu_pct", "swap_out_pages"):
            if k in row:
                msg.append(f"{k}={row[k]}")
        print("   " + "  ".join(msg), flush=True)
        if row.get("swap_out_pages", 0) > 0:
            print("   WARNING: the system swapped during this run", flush=True)
        if row.get("clock_negative", 0) > 0:
            print("   WARNING: negative latencies - clocks disagree", flush=True)
        if fields is None:
            fields = ALL_FIELDS
        new = not os.path.exists(csv_path)
        with open(csv_path, "a", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                wr.writeheader()
            wr.writerow(row)
    print("done. results in", csv_path)


ALL_FIELDS = [
    "run_key", "config", "phase", "model", "monitor_on", "gen_on", "nice", "weight", "pin", "rb",
    "rb_bytes", "rep", "warmup", "rate", "duration_s", "work_us", "mon_cpus", "llm_cpus", "mon_nice",
    "llm_nice", "mon_weight", "llm_weight", "start_time", "gen_sent", "gen_rate_achieved", "window_s",
    "events_generated", "events_matched", "loss_pct", "kernel_seen", "kernel_dropped", "user_received",
    "lat_p50_ms", "lat_p95_ms", "lat_p99_ms", "lat_max_ms", "buf_p50_ms", "buf_p99_ms", "clock_negative",
    "first_loss_s",
    "mon_cpu_s", "mon_cpu_pct", "mon_maxrss_kb", "bpf_ns_per_event", "bpf_cpu_pct",
    "cpu_busy_exp_pct", "cpu_busy_all_pct", "tok_per_s", "llm_tokens_in_window", "llm_resp_s_mean",
    "llm_tps_server_mean", "llm_requests_done", "llm_errors", "swap_in_pages", "swap_out_pages",
    "swapfree_drop_kb",
]

if __name__ == "__main__":
    main()
