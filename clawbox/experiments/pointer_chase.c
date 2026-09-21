#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/* One next pointer per cache line prevents adjacent-node streaming. */
struct node {
    uint32_t next;
    unsigned char pad[60];
};

static uint64_t rng_state;

static uint64_t xorshift64star(void) {
    uint64_t x = rng_state;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    rng_state = x;
    return x * UINT64_C(2685821657736338717);
}

static int write_marker(const char *path, const char *text) {
    FILE *f = fopen(path, "w");
    if (!f) return -1;
    fputs(text, f);
    fputc('\n', f);
    fflush(f);
    fclose(f);
    return 0;
}

static int marker_exists(const char *path) {
    return access(path, F_OK) == 0;
}

static uint64_t now_ns(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0;
    return (uint64_t)ts.tv_sec * UINT64_C(1000000000) + (uint64_t)ts.tv_nsec;
}

static struct node *alloc_nodes(size_t count) {
    size_t bytes = count * sizeof(struct node);
    unsigned char *raw = malloc(bytes + 64 + sizeof(void *));
    if (!raw) return NULL;
    uintptr_t p = (uintptr_t)(raw + sizeof(void *) + 63u) & ~(uintptr_t)63u;
    ((void **)p)[-1] = raw;
    return (struct node *)p;
}

static void free_nodes(struct node *nodes) {
    if (nodes) free(((void **)nodes)[-1]);
}

int main(int argc, char **argv) {
    /* The 3-argument form is used by the Docker placement runner.  The
       legacy 5/6-argument form remains available for the standalone runner. */
    if ((argc != 4 && argc != 6 && argc != 7)) {
        fprintf(stderr, "usage: %s SIZE_MIB STEPS SEED [READY GO [RESULT]]\n", argv[0]);
        return 2;
    }
    char *end = NULL;
    unsigned long size_mib = strtoul(argv[1], &end, 10);
    if (!end || *end || size_mib == 0) return 2;
    uint64_t steps = strtoull(argv[2], &end, 10);
    if (!end || *end || steps == 0) return 2;
    rng_state = strtoull(argv[3], &end, 0);
    if (!end || *end || rng_state == 0) rng_state = UINT64_C(0x9e3779b97f4a7c15);
    const char *ready = argc >= 6 ? argv[4] : NULL;
    const char *go = argc >= 6 ? argv[5] : NULL;
    const char *result = argc == 7 ? argv[6] : NULL;

    size_t bytes = (size_t)size_mib * 1024u * 1024u;
    size_t n = bytes / sizeof(struct node);
    if (n < 2 || n > UINT32_MAX) {
        fprintf(stderr, "invalid node count: %zu\n", n);
        return 2;
    }
    struct node *nodes = alloc_nodes(n);
    if (!nodes) {
        perror("malloc");
        return 1;
    }
    uint32_t *order = malloc(n * sizeof(*order));
    if (!order) {
        perror("malloc");
        free_nodes(nodes);
        return 1;
    }
    for (size_t i = 0; i < n; ++i) order[i] = (uint32_t)i;
    for (size_t i = n - 1; i > 0; --i) {
        size_t j = (size_t)(xorshift64star() % (i + 1));
        uint32_t t = order[i]; order[i] = order[j]; order[j] = t;
    }
    for (size_t i = 0; i < n; ++i) {
        nodes[order[i]].next = order[(i + 1) % n];
        /* Touch every line once while the process is already CPU-bound. */
        nodes[order[i]].pad[0] = (unsigned char)i;
    }
    free(order);

    char pidbuf[64];
    snprintf(pidbuf, sizeof(pidbuf), "%ld", (long)getpid());
    if (ready) {
        if (write_marker(ready, pidbuf) != 0) {
            perror("ready marker");
            free_nodes(nodes);
            return 1;
        }
        while (!marker_exists(go)) {
            struct timespec pause = {.tv_sec = 0, .tv_nsec = 1000000};
            nanosleep(&pause, NULL);
        }
    }

    uint32_t idx = (uint32_t)(rng_state % n);
    uint64_t start = now_ns();
    for (uint64_t i = 0; i < steps; ++i) {
        idx = nodes[idx].next;
    }
    uint64_t end_ns = now_ns();
    /* Keep the loop observable and prevent an over-aggressive optimizer. */
    if (result) {
        FILE *f = fopen(result, "w");
        if (f) {
            fprintf(f, "pid=%ld\nsize_mib=%lu\nsteps=%" PRIu64 "\nstart_ns=%" PRIu64
                    "\nend_ns=%" PRIu64 "\nelapsed_ns=%" PRIu64 "\nsink=%" PRIu32 "\n",
                    (long)getpid(), size_mib, steps, start, end_ns, end_ns - start, idx);
            fclose(f);
        }
    }
    printf("%" PRIu32 "\n", idx);
    fflush(stdout);
    free_nodes(nodes);
    return 0;
}
