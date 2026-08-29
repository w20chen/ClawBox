#define _GNU_SOURCE

#include <errno.h>
#include <inttypes.h>
#include <linux/vm_sockets.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define AGENT_PORT 18080U
#define LINE_SIZE 1024U

struct agent_state {
    uint64_t boot_nonce;
    uint64_t turn;
    uint64_t tool_count;
    char inflight_request[256];
    uint64_t predicted_ms;
    char gpu_id[128];
    uint64_t kv_bytes;
    uint64_t resident_bytes;
};

static struct agent_state state;
static volatile unsigned char *working_set;
static size_t working_set_bytes;

static void touch_working_set(void) {
    for (size_t offset = 0; offset < working_set_bytes; offset += 4096U) {
        working_set[offset] ^= (unsigned char)(state.tool_count + 1U);
    }
}

static void write_all(int fd, const char *buffer, size_t length) {
    while (length > 0) {
        ssize_t written = write(fd, buffer, length);
        if (written < 0 && errno == EINTR) {
            continue;
        }
        if (written <= 0) {
            return;
        }
        buffer += (size_t)written;
        length -= (size_t)written;
    }
}

static void send_state(int fd) {
    char response[1024];
    const char *inflight_prefix = state.inflight_request[0] ? "\"" : "";
    const char *inflight_value = state.inflight_request[0] ? state.inflight_request : "null";
    const char *inflight_suffix = state.inflight_request[0] ? "\"" : "";
    int size = snprintf(
        response, sizeof(response),
        "{\"ok\":true,\"state\":{\"boot_nonce\":%" PRIu64
        ",\"turn\":%" PRIu64 ",\"tool_count\":%" PRIu64
        ",\"inflight_request\":%s%s%s,\"predicted_ms\":%" PRIu64
        ",\"gpu_id\":\"%s\",\"kv_bytes\":%" PRIu64
        ",\"resident_bytes\":%" PRIu64 "}}\n",
        state.boot_nonce, state.turn, state.tool_count,
        inflight_prefix, inflight_value, inflight_suffix, state.predicted_ms,
        state.gpu_id, state.kv_bytes, state.resident_bytes
    );
    if (size > 0 && (size_t)size < sizeof(response)) {
        write_all(fd, response, (size_t)size);
    }
}

static void send_error(int fd, const char *message) {
    char response[512];
    int size = snprintf(response, sizeof(response),
                        "{\"ok\":false,\"error\":\"%s\"}\n", message);
    if (size > 0 && (size_t)size < sizeof(response)) {
        write_all(fd, response, (size_t)size);
    }
}

static ssize_t read_line(int fd, char *buffer, size_t capacity) {
    size_t length = 0;
    while (length + 1 < capacity) {
        char value;
        ssize_t count = read(fd, &value, 1);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            break;
        }
        if (value == '\n') {
            buffer[length] = '\0';
            return (ssize_t)length;
        }
        if (value != '\r') {
            buffer[length++] = value;
        }
    }
    buffer[length] = '\0';
    return -1;
}

static void handle_command(int fd, char *line) {
    if (strcmp(line, "PING") == 0 || strcmp(line, "STATE") == 0) {
        send_state(fd);
        return;
    }

    char request_id[256] = {0};
    char gpu_id[128] = {0};
    uint64_t predicted_ms = 0;
    uint64_t kv_bytes = 0;
    if (sscanf(line, "BEGIN %255s %" SCNu64 " %127s %" SCNu64,
               request_id, &predicted_ms, gpu_id, &kv_bytes) == 4) {
        if (state.inflight_request[0]) {
            send_error(fd, "request already in flight");
            return;
        }
        memcpy(state.inflight_request, request_id, sizeof(state.inflight_request) - 1);
        memcpy(state.gpu_id, gpu_id, sizeof(state.gpu_id) - 1);
        state.predicted_ms = predicted_ms;
        state.kv_bytes = kv_bytes;
        send_state(fd);
        return;
    }

    if (sscanf(line, "COMPLETE %255s", request_id) == 1) {
        if (!state.inflight_request[0] || strcmp(state.inflight_request, request_id) != 0) {
            send_error(fd, "in-flight request mismatch");
            return;
        }
        state.turn += 1;
        state.inflight_request[0] = '\0';
        state.predicted_ms = 0;
        state.gpu_id[0] = '\0';
        state.kv_bytes = 0;
        send_state(fd);
        return;
    }

    int exit_code = 0;
    if (sscanf(line, "TOOL %255s %d", request_id, &exit_code) == 2) {
        (void)exit_code;
        if (state.inflight_request[0]) {
            send_error(fd, "tool completed while LLM request is in flight");
            return;
        }
        touch_working_set();
        state.tool_count += 1;
        send_state(fd);
        return;
    }

    send_error(fd, "unknown command");
}

static int listen_vsock(void) {
    int fd = socket(AF_VSOCK, SOCK_STREAM, 0);
    if (fd < 0) {
        return -1;
    }
    struct sockaddr_vm address = {
        .svm_family = AF_VSOCK,
        .svm_cid = VMADDR_CID_ANY,
        .svm_port = AGENT_PORT,
    };
    if (bind(fd, (struct sockaddr *)&address, sizeof(address)) < 0 || listen(fd, 16) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static void allocate_working_set(void) {
    if (mkdir("/proc", 0555) < 0 && errno != EEXIST) {
        dprintf(STDERR_FILENO, "clawbox runtime agent: cannot create /proc: %s\n", strerror(errno));
        exit(1);
    }
    if (mount("proc", "/proc", "proc", 0, NULL) < 0 && errno != EBUSY) {
        dprintf(STDERR_FILENO, "clawbox runtime agent: cannot mount /proc: %s\n", strerror(errno));
        exit(1);
    }
    FILE *cmdline = fopen("/proc/cmdline", "r");
    char buffer[4096] = {0};
    unsigned long long touch_mib = 0;
    if (cmdline != NULL) {
        if (fgets(buffer, sizeof(buffer), cmdline) != NULL) {
            char *argument = strstr(buffer, "clawbox.touch_mib=");
            if (argument != NULL) {
                touch_mib = strtoull(argument + strlen("clawbox.touch_mib="), NULL, 10);
            }
        }
        fclose(cmdline);
    }
    if (touch_mib == 0) {
        return;
    }
    if (touch_mib > UINT64_MAX / (1024ULL * 1024ULL)) {
        dprintf(STDERR_FILENO, "clawbox runtime agent: invalid working set\n");
        exit(1);
    }
    size_t bytes = (size_t)(touch_mib * 1024ULL * 1024ULL);
    working_set = calloc(1, bytes);
    if (working_set == NULL) {
        dprintf(STDERR_FILENO, "clawbox runtime agent: cannot allocate %zu bytes\n", bytes);
        exit(1);
    }
    working_set_bytes = bytes;
    touch_working_set();
    state.resident_bytes = (uint64_t)bytes;
}

int main(void) {
    signal(SIGPIPE, SIG_IGN);
    struct timespec now = {0};
    clock_gettime(CLOCK_REALTIME, &now);
    state.boot_nonce = ((uint64_t)now.tv_sec << 32U) ^ (uint64_t)now.tv_nsec ^ (uint64_t)getpid();
    memcpy(state.gpu_id, "none", 5);
    allocate_working_set();

    int listener = -1;
    for (int attempt = 0; attempt < 200 && listener < 0; ++attempt) {
        listener = listen_vsock();
        if (listener < 0) {
            usleep(10000);
        }
    }
    if (listener < 0) {
        dprintf(STDERR_FILENO, "clawbox runtime agent: vsock listen failed: %s\n", strerror(errno));
        return 1;
    }
    dprintf(STDERR_FILENO, "clawbox runtime agent: ready on vsock port %u\n", AGENT_PORT);

    for (;;) {
        int connection = accept(listener, NULL, NULL);
        if (connection < 0) {
            if (errno == EINTR) {
                continue;
            }
            usleep(1000);
            continue;
        }
        char line[LINE_SIZE];
        if (read_line(connection, line, sizeof(line)) >= 0) {
            handle_command(connection, line);
        } else {
            send_error(connection, "invalid command line");
        }
        close(connection);
    }
}
