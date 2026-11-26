// User-space reader for the exec monitor.
// Usage: sudo ./monitor --rb-bytes 131072 --out events.csv --summary summary.json [--work-us 50]
//   Prints one CSV line per event: id,k_ts_ns,u_ts_ns,cpu   (id = -1 for unlabeled execs)
//   On SIGINT/SIGTERM writes a JSON summary (kernel drop counter, CPU used, BPF run time).
#include <errno.h>
#include <getopt.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/resource.h>
#include <bpf/libbpf.h>
#include <bpf/bpf.h>
#include "monitor.h"
#include "monitor.skel.h"

static volatile sig_atomic_t stop;
static FILE *out;
static long work_ns;
static unsigned long long n_recv, n_labeled;

static void on_sig(int s) { (void)s; stop = 1; }

static unsigned long long now_ns(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (unsigned long long)t.tv_sec * 1000000000ull + t.tv_nsec;
}

/* Labeled events are exec'd via a path ending in "/evXXXXXXXX" (8 digits). */
static long parse_id(const char *fname)
{
    const char *b = strrchr(fname, '/');
    b = b ? b + 1 : fname;
    if (b[0] != 'e' || b[1] != 'v') return -1;
    char *end;
    long v = strtol(b + 2, &end, 10);
    if (end == b + 2 || *end) return -1;
    return v;
}

static int on_event(void *ctx, void *data, size_t sz)
{
    (void)ctx; (void)sz;
    const struct event *e = data;
    unsigned long long t = now_ns();
    long id = parse_id(e->fname);
    n_recv++;
    if (id >= 0) n_labeled++;
    fprintf(out, "%ld,%llu,%llu,%u\n", id, e->ts_ns, t, e->cpu);
    /* Simulated per-event analysis cost (rule matching, enrichment, ...). */
    if (work_ns > 0) {
        unsigned long long end = t + work_ns;
        while (now_ns() < end) ;
    }
    return 0;
}

static int print_libbpf(enum libbpf_print_level lvl, const char *fmt, va_list ap)
{
    if (lvl == LIBBPF_DEBUG) return 0;
    return vfprintf(stderr, fmt, ap);
}

int main(int argc, char **argv)
{
    unsigned long rb_bytes = 128 * 1024;
    const char *out_path = "events.csv", *sum_path = "summary.json";
    double work_us = 0;
    static struct option opts[] = {
        {"rb-bytes", required_argument, 0, 'b'},
        {"out", required_argument, 0, 'o'},
        {"summary", required_argument, 0, 's'},
        {"work-us", required_argument, 0, 'w'},
        {0, 0, 0, 0}};
    int c;
    while ((c = getopt_long(argc, argv, "b:o:s:w:", opts, NULL)) != -1) {
        switch (c) {
        case 'b': rb_bytes = strtoul(optarg, NULL, 0); break;
        case 'o': out_path = optarg; break;
        case 's': sum_path = optarg; break;
        case 'w': work_us = atof(optarg); break;
        default: fprintf(stderr, "bad args\n"); return 2;
        }
    }
    work_ns = (long)(work_us * 1000);
    long pg = sysconf(_SC_PAGESIZE);
    if (rb_bytes < (unsigned long)pg || (rb_bytes & (rb_bytes - 1)) || rb_bytes % pg) {
        fprintf(stderr, "rb-bytes must be a power of 2 and a multiple of the page size (%ld)\n", pg);
        return 2;
    }

    libbpf_set_print(print_libbpf);
    signal(SIGINT, on_sig);
    signal(SIGTERM, on_sig);

    /* Keep BPF run-time statistics enabled while we run (for overhead). */
    int stats_fd = bpf_enable_stats(BPF_STATS_RUN_TIME);

    struct monitor_bpf *skel = monitor_bpf__open();
    if (!skel) { fprintf(stderr, "open failed\n"); return 1; }
    bpf_map__set_max_entries(skel->maps.rb, rb_bytes);
    if (monitor_bpf__load(skel)) { fprintf(stderr, "load failed (are you root?)\n"); return 1; }
    if (monitor_bpf__attach(skel)) { fprintf(stderr, "attach failed\n"); return 1; }

    struct ring_buffer *rb = ring_buffer__new(bpf_map__fd(skel->maps.rb), on_event, NULL, NULL);
    if (!rb) { fprintf(stderr, "ring_buffer__new failed\n"); return 1; }

    out = fopen(out_path, "w");
    if (!out) { perror("fopen"); return 1; }
    setvbuf(out, NULL, _IOFBF, 1 << 20);
    fprintf(out, "id,k_ts_ns,u_ts_ns,cpu\n");

    /* Tell the runner we are attached. */
    fprintf(stderr, "READY rb_bytes=%lu page=%ld work_us=%.1f\n", rb_bytes, pg, work_us);

    unsigned long long t0 = now_ns();
    while (!stop) {
        int err = ring_buffer__poll(rb, 100);
        if (err < 0 && err != -EINTR) { fprintf(stderr, "poll error %d\n", err); break; }
    }
    unsigned long long t1 = now_ns();
    fflush(out);

    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    struct bpf_prog_info info;
    __u32 len = sizeof(info);
    memset(&info, 0, len);
    bpf_prog_get_info_by_fd(bpf_program__fd(skel->progs.handle_exec), &info, &len);

    FILE *s = fopen(sum_path, "w");
    if (s) {
        fprintf(s, "{\"rb_bytes\": %lu, \"page_size\": %ld, \"work_us\": %.2f, "
                   "\"kernel_seen\": %llu, \"kernel_dropped\": %llu, "
                   "\"user_received\": %llu, \"user_labeled\": %llu, "
                   "\"wall_s\": %.3f, \"utime_s\": %.4f, \"stime_s\": %.4f, \"maxrss_kb\": %ld, "
                   "\"bpf_run_time_ns\": %llu, \"bpf_run_cnt\": %llu, \"bpf_stats_ok\": %s}\n",
                rb_bytes, pg, work_us,
                (unsigned long long)skel->bss->seen, (unsigned long long)skel->bss->dropped,
                n_recv, n_labeled,
                (t1 - t0) / 1e9,
                ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6,
                ru.ru_stime.tv_sec + ru.ru_stime.tv_usec / 1e6, ru.ru_maxrss,
                (unsigned long long)info.run_time_ns, (unsigned long long)info.run_cnt,
                stats_fd >= 0 ? "true" : "false");
        fclose(s);
    }
    fclose(out);
    ring_buffer__free(rb);
    monitor_bpf__destroy(skel);
    if (stats_fd >= 0) close(stats_fd);
    return 0;
}
