#ifndef MONITOR_H
#define MONITOR_H
#define FNAME_LEN 64
#define COMM_LEN 16
struct event {
    unsigned long long ts_ns;   /* bpf_ktime_get_ns() == CLOCK_MONOTONIC */
    unsigned int pid;
    unsigned int cpu;
    char comm[COMM_LEN];
    char fname[FNAME_LEN];
};
#endif
