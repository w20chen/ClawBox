#define _POSIX_C_SOURCE 200809L

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

/* A deliberately simple sequential bandwidth control.  The buffer is much
   larger than the candidate LLC domain and every pass reads every cache line.
   It is a negative/control workload, not a replacement for pointer chasing. */
int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: %s SIZE_MIB PASSES\n", argv[0]);
        return 2;
    }
    char *end = NULL;
    size_t size_mib = strtoull(argv[1], &end, 10);
    if (!end || *end || size_mib == 0) return 2;
    unsigned long passes = strtoul(argv[2], &end, 10);
    if (!end || *end || passes == 0) return 2;
    size_t bytes = size_mib * 1024u * 1024u;
    unsigned char *buf = malloc(bytes);
    if (!buf) {
        perror("malloc");
        return 1;
    }
    for (size_t i = 0; i < bytes; i += 64) buf[i] = (unsigned char)i;
    volatile uint64_t sink = 0;
    for (unsigned long pass = 0; pass < passes; ++pass) {
        for (size_t i = 0; i < bytes; i += 64) sink += buf[i];
    }
    printf("%llu\n", (unsigned long long)sink);
    fflush(stdout);
    free(buf);
    return 0;
}
