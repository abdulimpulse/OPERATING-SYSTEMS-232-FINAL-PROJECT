// Labeled event generator. Starts short-lived processes at a fixed rate.
// Every event i is an execve() of the path <linkdir>/ev<8-digit id>, a symlink to a tiny
// static program, so the exec tracepoint sees the ID in the filename (and in comm).
// The timestamp (CLOCK_MONOTONIC) is taken in the vfork child right before execve().
// Usage: ./evgen --rate 1000 --duration 60 --target ./evtarget --linkdir /dev/shm/evl --out gen.csv
#define _GNU_SOURCE
#include <errno.h>
#include <getopt.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/stat.h>

static unsigned long long ts_of(struct timespec *t)
{ return (unsigned long long)t->tv_sec * 1000000000ull + t->tv_nsec; }

int main(int argc, char **argv)
{
    double rate = 1000, duration = 60;
    long start_id = 0;
    const char *target = "./evtarget", *linkdir = "/dev/shm/evl", *outp = "gen.csv";
    static struct option opts[] = {
        {"rate", 1, 0, 'r'}, {"duration", 1, 0, 'd'}, {"target", 1, 0, 't'},
        {"linkdir", 1, 0, 'l'}, {"out", 1, 0, 'o'}, {"start-id", 1, 0, 's'}, {0, 0, 0, 0}};
    int c;
    while ((c = getopt_long(argc, argv, "", opts, NULL)) != -1) {
        switch (c) {
        case 'r': rate = atof(optarg); break;
        case 'd': duration = atof(optarg); break;
        case 't': target = optarg; break;
        case 'l': linkdir = optarg; break;
        case 'o': outp = optarg; break;
        case 's': start_id = atol(optarg); break;
        default: return 2;
        }
    }
    char tgt[4096];
    if (!realpath(target, tgt)) { perror("target"); return 1; }
    mkdir(linkdir, 0755);
    signal(SIGCHLD, SIG_IGN);           /* auto-reap children */

    long n = (long)(rate * duration);
    unsigned long long *tgen = calloc(n, sizeof(*tgen));
    int *ok = calloc(n, sizeof(*ok));
    if (!tgen || !ok) return 1;

    long period_ns = (long)(1e9 / rate);
    struct timespec next, t;
    clock_gettime(CLOCK_MONOTONIC, &next);
    unsigned long long t_start = ts_of(&next);
    char path[4200];
    char *const envp[] = {NULL};
    long late = 0;

    for (long i = 0; i < n; i++) {
        long id = start_id + i;
        snprintf(path, sizeof(path), "%s/ev%08ld", linkdir, id);
        unlink(path);
        if (symlink(tgt, path)) { perror("symlink"); continue; }
        char *const av[] = {path, NULL};
        volatile unsigned long long *slot = &tgen[i];
        pid_t p = vfork();
        if (p == 0) {
            struct timespec tc;
            clock_gettime(CLOCK_MONOTONIC, &tc);
            *slot = ts_of(&tc);
            execve(path, av, envp);
            _exit(127);
        }
        ok[i] = (p > 0);
        unlink(path);
        /* fixed-rate pacing on an absolute schedule */
        next.tv_nsec += period_ns;
        while (next.tv_nsec >= 1000000000L) { next.tv_nsec -= 1000000000L; next.tv_sec++; }
        clock_gettime(CLOCK_MONOTONIC, &t);
        if (ts_of(&t) > ts_of(&next)) late++;
        else clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
    }
    clock_gettime(CLOCK_MONOTONIC, &t);
    unsigned long long t_end = ts_of(&t);

    FILE *f = fopen(outp, "w");
    if (!f) { perror("out"); return 1; }
    fprintf(f, "id,t_gen_ns\n");
    long sent = 0;
    for (long i = 0; i < n; i++)
        if (ok[i] && tgen[i]) { fprintf(f, "%ld,%llu\n", start_id + i, tgen[i]); sent++; }
    fclose(f);
    fprintf(stderr, "GEN sent=%ld target=%ld elapsed_s=%.3f achieved_rate=%.1f late_slots=%ld\n",
            sent, n, (t_end - t_start) / 1e9, sent / ((t_end - t_start) / 1e9), late);
    return 0;
}
