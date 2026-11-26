#!/usr/bin/env bash
# One-time setup on Fedora (tested target: Fedora Asahi Remix, M1, kernel 7.x, 16k pages).
# Run from the repository root as your normal user:   bash setup.sh
# It asks for your sudo password once (for dnf). Safe to run again: finished steps are skipped.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(pwd)
LLAMA_COMMIT=36d818e161c400eb856e4591be49f422fbeeea53   # commit this toolkit was tested with

step() { echo; echo "=== $* ==="; }

step "1/6 Installing packages (dnf)"
sudo dnf install -y git cmake make gcc gcc-c++ clang llvm bpftool libbpf-devel \
    elfutils-libelf-devel zlib-devel curl util-linux procps-ng \
    python3 python3-pandas python3-matplotlib python3-numpy

step "2/6 Checking kernel support"
[ -f /sys/kernel/btf/vmlinux ] && echo "BTF: ok" || { echo "ERROR: /sys/kernel/btf/vmlinux missing (kernel built without BTF)"; exit 1; }
[ -f /sys/fs/cgroup/cgroup.controllers ] && echo "cgroup v2: ok ($(cat /sys/fs/cgroup/cgroup.controllers))" || { echo "ERROR: cgroup v2 not mounted"; exit 1; }
grep -qw cpu /sys/fs/cgroup/cgroup.controllers || { echo "ERROR: cgroup cpu controller missing"; exit 1; }
if [ ! -d /sys/kernel/tracing/events/sched ]; then
    sudo mount -t tracefs nodev /sys/kernel/tracing 2>/dev/null || true
fi
sudo test -d /sys/kernel/tracing/events/sched/sched_process_exec && echo "tracepoint sched_process_exec: ok" \
    || echo "WARNING: tracepoint not visible yet; the monitor may still work (libbpf mounts tracefs if needed)"
echo "page size: $(getconf PAGESIZE)"
echo "CPU capacities (bigger = performance core):"
for c in /sys/devices/system/cpu/cpu[0-9]*; do
    printf "  %s capacity=%s\n" "$(basename "$c")" "$(cat "$c/cpu_capacity" 2>/dev/null || echo n/a)"
done

step "3/6 Building the eBPF monitor, event generator and target"
make -C monitor
echo "built: $(ls monitor/monitor monitor/evgen monitor/evtarget | tr '\n' ' ')"

step "4/6 Building llama.cpp (CPU only, this takes 10-20 minutes)"
if [ ! -x llama.cpp/build/bin/llama-server ]; then
    if [ ! -d llama.cpp ]; then
        git clone --depth 1 https://github.com/ggml-org/llama.cpp
        (cd llama.cpp && git fetch --depth 1 origin "$LLAMA_COMMIT" && git checkout -q "$LLAMA_COMMIT") \
            || echo "NOTE: could not pin commit $LLAMA_COMMIT, using latest master"
    fi
    cmake -S llama.cpp -B llama.cpp/build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON \
        -DGGML_METAL=OFF -DGGML_VULKAN=OFF -DGGML_OPENCL=OFF -DGGML_BLAS=OFF \
        -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_OPENSSL=OFF
    # -j4 keeps memory use reasonable on 8 GB
    cmake --build llama.cpp/build --target llama-server -j4
fi
ls -l llama.cpp/build/bin/llama-server

step "5/6 Downloading models (about 6.2 GB total)"
mkdir -p models
dl() {  # dl <repo> <file>
    if [ -s "models/$2" ] && [ "$(stat -c %s "models/$2")" -gt 100000000 ]; then
        echo "have $2"; return
    fi
    echo "downloading $2"
    curl -L --fail --retry 5 -C - -o "models/$2" "https://huggingface.co/$1/resolve/main/$2"
}
dl bartowski/Qwen2.5-0.5B-Instruct-GGUF Qwen2.5-0.5B-Instruct-Q4_K_M.gguf
dl bartowski/Qwen2.5-3B-Instruct-GGUF   Qwen2.5-3B-Instruct-Q4_K_M.gguf
dl bartowski/Qwen2.5-7B-Instruct-GGUF   Qwen2.5-7B-Instruct-Q3_K_M.gguf
ls -lh models

step "6/6 Quick checks"
free -m
swapon --show || true
echo
echo "Setup finished. Next: sudo python3 scripts/runner.py --selftest"
