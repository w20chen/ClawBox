#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

/* Keep the host dependency to the runtime libzstd ABI; kunpeng has libzstd.so.1. */
size_t ZSTD_compressBound(size_t srcSize);
size_t ZSTD_compress(void *dst, size_t dstCapacity, const void *src, size_t srcSize, int level);
size_t ZSTD_decompress(void *dst, size_t dstCapacity, const void *src, size_t compressedSize);
unsigned ZSTD_isError(size_t code);
const char *ZSTD_getErrorName(size_t code);

#define MAGIC "CBZPG001"
#define VERSION 1U
#define ENCODING_ZERO 0U
#define ENCODING_ZSTD 1U
#define ENCODING_RAW 2U

struct __attribute__((packed)) header {
    char magic[8];
    uint32_t version;
    uint32_t page_size;
    uint64_t logical_size;
    uint64_t page_count;
    uint64_t index_offset;
    uint64_t data_offset;
    uint64_t reserved[2];
};

struct __attribute__((packed)) entry {
    uint64_t offset;
    uint32_t length;
    uint8_t encoding;
    uint8_t reserved[3];
};

static double seconds(struct timespec a, struct timespec b) {
    return (double)(b.tv_sec - a.tv_sec) + (double)(b.tv_nsec - a.tv_nsec) / 1e9;
}

static double cpu_seconds(const struct rusage *r) {
    return (double)r->ru_utime.tv_sec + r->ru_utime.tv_usec / 1e6
         + (double)r->ru_stime.tv_sec + r->ru_stime.tv_usec / 1e6;
}

static int all_zero(const uint8_t *data, size_t size) {
    size_t i;
    for (i = 0; i < size; ++i) if (data[i] != 0) return 0;
    return 1;
}

static int write_full(int fd, const void *buffer, size_t size, off_t offset) {
    const uint8_t *cursor = buffer;
    while (size) {
        ssize_t written = pwrite(fd, cursor, size, offset);
        if (written < 0) { if (errno == EINTR) continue; return -1; }
        cursor += written; size -= (size_t)written; offset += written;
    }
    return 0;
}

static int read_full(int fd, void *buffer, size_t size, off_t offset) {
    uint8_t *cursor = buffer;
    while (size) {
        ssize_t got = pread(fd, cursor, size, offset);
        if (got < 0) { if (errno == EINTR) continue; return -1; }
        if (got == 0) { errno = EIO; return -1; }
        cursor += got; size -= (size_t)got; offset += got;
    }
    return 0;
}

static uint64_t allocated_bytes(const struct stat *st) { return (uint64_t)st->st_blocks * 512U; }

static void report(const char *operation, const struct stat *input, const struct stat *output,
                   const struct header *h, uint64_t zero, uint64_t zstd, uint64_t raw,
                   uint64_t payload, double wall, double cpu, long peak_kib) {
    uint64_t in_alloc = input ? allocated_bytes(input) : 0;
    uint64_t out_alloc = output ? allocated_bytes(output) : 0;
    double mib = h->logical_size / 1048576.0;
    printf("{\"operation\":\"%s\",\"page_size\":%u,\"logical_bytes\":%" PRIu64
           ",\"page_count\":%" PRIu64 ",\"zero_pages\":%" PRIu64
           ",\"zstd_pages\":%" PRIu64 ",\"raw_pages\":%" PRIu64
           ",\"index_bytes\":%" PRIu64 ",\"payload_bytes\":%" PRIu64
           ",\"input_allocated_bytes\":%" PRIu64 ",\"output_allocated_bytes\":%" PRIu64
           ",\"wall_seconds\":%.9f,\"cpu_seconds\":%.9f,\"logical_mib_per_second\":%.3f"
           ",\"peak_rss_kib\":%ld}\n",
           operation, h->page_size, h->logical_size, h->page_count, zero, zstd, raw,
           h->page_count * (uint64_t)sizeof(struct entry), payload, in_alloc, out_alloc,
           wall, cpu, wall > 0 ? mib / wall : 0.0, peak_kib);
}

static int compress_file(const char *input_path, const char *output_path) {
    int in = -1, out = -1, rc = 1;
    struct stat in_st, out_st;
    struct header h = {0};
    struct entry *entries = NULL;
    uint8_t *page = NULL, *compressed = NULL;
    uint64_t zero = 0, zstd = 0, raw = 0, payload = 0;
    struct timespec wall_a, wall_b;
    struct rusage usage_a, usage_b;
    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0 || page_size > (1 << 20)) { fprintf(stderr, "invalid host page size\n"); return 1; }
    if ((in = open(input_path, O_RDONLY | O_CLOEXEC)) < 0 || fstat(in, &in_st) < 0) { perror(input_path); goto done; }
    if (!S_ISREG(in_st.st_mode)) { fprintf(stderr, "input is not a regular file\n"); goto done; }
    memcpy(h.magic, MAGIC, 8); h.version = VERSION; h.page_size = (uint32_t)page_size;
    h.logical_size = (uint64_t)in_st.st_size;
    h.page_count = (h.logical_size + h.page_size - 1) / h.page_size;
    h.index_offset = sizeof(h); h.data_offset = sizeof(h) + h.page_count * sizeof(struct entry);
    if (h.page_count > SIZE_MAX / sizeof(struct entry)) { fprintf(stderr, "snapshot too large\n"); goto done; }
    entries = calloc((size_t)h.page_count, sizeof(*entries));
    page = malloc((size_t)page_size); compressed = malloc(ZSTD_compressBound((size_t)page_size));
    if (!entries || !page || !compressed) { perror("allocate"); goto done; }
    if ((out = open(output_path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, in_st.st_mode & 0777)) < 0) { perror(output_path); goto done; }
    if (ftruncate(out, (off_t)h.data_offset) < 0 || write_full(out, &h, sizeof(h), 0) < 0) { perror("initialize output"); goto done; }
    clock_gettime(CLOCK_MONOTONIC, &wall_a); getrusage(RUSAGE_SELF, &usage_a);
    off_t cursor = 0;
    while ((uint64_t)cursor < h.logical_size) {
        off_t data = lseek(in, cursor, SEEK_DATA);
        if (data < 0) { if (errno == ENXIO) break; perror("SEEK_DATA"); goto done; }
        off_t hole = lseek(in, data, SEEK_HOLE);
        if (hole < 0) { perror("SEEK_HOLE"); goto done; }
        off_t first = data - data % page_size;
        off_t limit = ((hole + page_size - 1) / page_size) * page_size;
        if ((uint64_t)limit > h.logical_size) limit = (off_t)h.logical_size;
        for (off_t position = first; position < limit; position += page_size) {
            size_t valid = (size_t)(((uint64_t)position + (uint64_t)page_size <= h.logical_size)
                         ? (uint64_t)page_size : h.logical_size - (uint64_t)position);
            memset(page, 0, (size_t)page_size);
            if (read_full(in, page, valid, position) < 0) { perror("read page"); goto done; }
            uint64_t index = (uint64_t)position / h.page_size;
            if (all_zero(page, valid)) continue;
            size_t encoded = ZSTD_compress(compressed, ZSTD_compressBound(valid), page, valid, 1);
            if (ZSTD_isError(encoded)) { fprintf(stderr, "zstd: %s\n", ZSTD_getErrorName(encoded)); goto done; }
            const void *source; size_t stored; uint8_t kind;
            if (encoded < valid) { source = compressed; stored = encoded; kind = ENCODING_ZSTD; ++zstd; }
            else { source = page; stored = valid; kind = ENCODING_RAW; ++raw; }
            off_t target = (off_t)(h.data_offset + payload);
            if (write_full(out, source, stored, target) < 0) { perror("write payload"); goto done; }
            entries[index].offset = (uint64_t)target;
            entries[index].length = (uint32_t)stored;
            entries[index].encoding = kind;
            payload += stored;
        }
        cursor = hole;
    }
    zero = h.page_count - zstd - raw;
    if (write_full(out, entries, (size_t)(h.page_count * sizeof(*entries)), (off_t)h.index_offset) < 0
        || ftruncate(out, (off_t)(h.data_offset + payload)) < 0 || fsync(out) < 0) { perror("finalize output"); goto done; }
    clock_gettime(CLOCK_MONOTONIC, &wall_b); getrusage(RUSAGE_SELF, &usage_b);
    if (fstat(out, &out_st) < 0) { perror("stat output"); goto done; }
    report("compress", &in_st, &out_st, &h, zero, zstd, raw, payload,
           seconds(wall_a, wall_b), cpu_seconds(&usage_b) - cpu_seconds(&usage_a), usage_b.ru_maxrss);
    rc = 0;
done:
    if (in >= 0) close(in);
    if (out >= 0) close(out);
    if (rc) unlink(output_path);
    free(entries); free(page); free(compressed); return rc;
}

static int decompress_file(const char *input_path, const char *output_path) {
    int in = -1, out = -1, rc = 1;
    struct stat in_st, out_st;
    struct header h = {0}; struct entry *entries = NULL;
    uint8_t *page = NULL, *payload_buffer = NULL; size_t payload_capacity = 0;
    uint64_t zero = 0, zstd = 0, raw = 0, payload = 0;
    struct timespec wall_a, wall_b; struct rusage usage_a, usage_b;
    if ((in = open(input_path, O_RDONLY | O_CLOEXEC)) < 0 || fstat(in, &in_st) < 0) { perror(input_path); goto done; }
    if (read_full(in, &h, sizeof(h), 0) < 0 || memcmp(h.magic, MAGIC, 8) || h.version != VERSION
        || h.page_size == 0 || h.page_count != (h.logical_size + h.page_size - 1) / h.page_size
        || h.index_offset != sizeof(h) || h.data_offset != sizeof(h) + h.page_count * sizeof(struct entry)) {
        fprintf(stderr, "invalid zstd page snapshot\n"); goto done;
    }
    if (h.page_count > SIZE_MAX / sizeof(struct entry)) goto done;
    entries = malloc((size_t)(h.page_count * sizeof(*entries))); page = malloc(h.page_size);
    if (!entries || !page || read_full(in, entries, (size_t)(h.page_count * sizeof(*entries)), (off_t)h.index_offset) < 0) { perror("read index"); goto done; }
    if ((out = open(output_path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600)) < 0 || ftruncate(out, (off_t)h.logical_size) < 0) { perror(output_path); goto done; }
    clock_gettime(CLOCK_MONOTONIC, &wall_a); getrusage(RUSAGE_SELF, &usage_a);
    for (uint64_t i = 0; i < h.page_count; ++i) {
        struct entry *e = &entries[i]; size_t valid = (size_t)(((i + 1) * h.page_size <= h.logical_size) ? h.page_size : h.logical_size - i * h.page_size);
        if (e->encoding == ENCODING_ZERO) { ++zero; continue; }
        if (e->length == 0 || e->offset < h.data_offset) { fprintf(stderr, "invalid page entry\n"); goto done; }
        if (e->length > payload_capacity) { uint8_t *next = realloc(payload_buffer, e->length); if (!next) { perror("payload buffer"); goto done; } payload_buffer = next; payload_capacity = e->length; }
        if (read_full(in, payload_buffer, e->length, (off_t)e->offset) < 0) { perror("read payload"); goto done; }
        if (e->encoding == ENCODING_ZSTD) {
            size_t decoded = ZSTD_decompress(page, valid, payload_buffer, e->length);
            if (ZSTD_isError(decoded) || decoded != valid) { fprintf(stderr, "zstd decode failed\n"); goto done; }
            ++zstd;
        } else if (e->encoding == ENCODING_RAW && e->length == valid) { memcpy(page, payload_buffer, valid); ++raw; }
        else { fprintf(stderr, "invalid encoding\n"); goto done; }
        if (write_full(out, page, valid, (off_t)(i * h.page_size)) < 0) { perror("write page"); goto done; }
        payload += e->length;
    }
    if (fsync(out) < 0) { perror("sync output"); goto done; }
    clock_gettime(CLOCK_MONOTONIC, &wall_b); getrusage(RUSAGE_SELF, &usage_b);
    if (fstat(out, &out_st) < 0) goto done;
    report("decompress", &in_st, &out_st, &h, zero, zstd, raw, payload,
           seconds(wall_a, wall_b), cpu_seconds(&usage_b) - cpu_seconds(&usage_a), usage_b.ru_maxrss);
    rc = 0;
done:
    if (in >= 0) close(in);
    if (out >= 0) close(out);
    if (rc) unlink(output_path);
    free(entries); free(page); free(payload_buffer); return rc;
}

int main(int argc, char **argv) {
    if (argc != 4 || (strcmp(argv[1], "compress") && strcmp(argv[1], "decompress"))) {
        fprintf(stderr, "usage: %s compress|decompress INPUT OUTPUT\n", argv[0]); return 2;
    }
    return !strcmp(argv[1], "compress") ? compress_file(argv[2], argv[3]) : decompress_file(argv[2], argv[3]);
}
