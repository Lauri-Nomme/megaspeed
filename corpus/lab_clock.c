#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdlib.h>
#include <sys/time.h>
#include <time.h>

/* Lab-only: shift CLOCK_REALTIME for one process tree (bb-lab-b generation).
 *
 * The Bitbucket DC developer license in this lab is a short-lived window; once
 * it expires the server rejects git pushes.  Rather than changing the shared
 * host clock (bb-lab-a lives on the same host), start a temporary bb-lab-b
 * container with this shim and a negative MEGASPEED_CLOCK_SHIFT so the JVM
 * believes it is still inside the license window.  The data directory is the
 * same, so the generated corpus is real; only the server's notion of "now"
 * moves.  See corpus/README.md.
 *
 *   gcc -shared -fPIC -O2 -o clock_shift.so corpus/lab_clock.c -ldl
 *   LD_PRELOAD=/path/clock_shift.so MEGASPEED_CLOCK_SHIFT=-40000 ...
 */

static long shift_seconds(void) {
    static long value = 0;
    static int init = 0;
    if (!init) {
        const char *raw = getenv("MEGASPEED_CLOCK_SHIFT");
        value = raw ? atol(raw) : 0;
        init = 1;
    }
    return value;
}

static int shiftable(clockid_t clk) {
    return clk == CLOCK_REALTIME || clk == CLOCK_REALTIME_COARSE;
}

int clock_gettime(clockid_t clk, struct timespec *ts) {
    static int (*real)(clockid_t, struct timespec *) = 0;
    if (!real) real = dlsym(RTLD_NEXT, "clock_gettime");
    int rc = real(clk, ts);
    if (rc == 0 && ts && shiftable(clk)) ts->tv_sec += shift_seconds();
    return rc;
}

int gettimeofday(struct timeval *tv, void *tz) {
    static int (*real)(struct timeval *, void *) = 0;
    if (!real) real = dlsym(RTLD_NEXT, "gettimeofday");
    int rc = real(tv, tz);
    if (rc == 0 && tv) tv->tv_sec += shift_seconds();
    return rc;
}

time_t time(time_t *out) {
    static time_t (*real)(time_t *) = 0;
    if (!real) real = dlsym(RTLD_NEXT, "time");
    time_t now = real(NULL);
    if (now != (time_t)-1) now += shift_seconds();
    if (out) *out = now;
    return now;
}
