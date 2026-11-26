// SPDX-License-Identifier: GPL-2.0
// Exec monitor: one event per successful execve, pushed through a BPF ring buffer.
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include "monitor.h"

char LICENSE[] SEC("license") = "GPL";

/* Size is overridden from user space (bpf_map__set_max_entries) before load. */
struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 256 * 1024);
} rb SEC(".maps");

/* Counters readable from user space through the skeleton's .bss mapping. */
__u64 seen = 0;     /* exec events that reached the BPF program */
__u64 dropped = 0;  /* bpf_ringbuf_reserve() failed => buffer full => event lost in kernel */

SEC("tp/sched/sched_process_exec")
int handle_exec(struct trace_event_raw_sched_process_exec *ctx)
{
    struct event *e;
    unsigned int off;

    __sync_fetch_and_add(&seen, 1);
    e = bpf_ringbuf_reserve(&rb, sizeof(*e), 0);
    if (!e) {
        __sync_fetch_and_add(&dropped, 1);
        return 0;
    }
    e->ts_ns = bpf_ktime_get_ns();
    e->pid = bpf_get_current_pid_tgid() >> 32;
    e->cpu = bpf_get_smp_processor_id();
    bpf_get_current_comm(&e->comm, sizeof(e->comm));
    off = ctx->__data_loc_filename & 0xFFFF;
    bpf_probe_read_kernel_str(&e->fname, sizeof(e->fname), (void *)ctx + off);
    bpf_ringbuf_submit(e, 0);
    return 0;
}
