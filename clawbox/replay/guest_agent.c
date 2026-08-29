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
#include <sys/select.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define AGENT_PORT 18080U
#define LINE_SIZE 65536U
#define TOOL_OUTPUT_LIMIT 32768U

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

static void send_error(int fd, const char *message);

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

static const char base64_table[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static int base64_value(unsigned char value) {
    const char *match = strchr(base64_table, value);
    return match == NULL ? -1 : (int)(match - base64_table);
}

static unsigned char *base64_decode(const char *input, size_t *output_length) {
    size_t input_length = strlen(input);
    if (input_length == 0 || input_length % 4U != 0) return NULL;
    size_t padding = input[input_length - 1] == '=' ? 1U : 0U;
    if (input[input_length - 2] == '=') padding++;
    unsigned char *output = malloc(input_length / 4U * 3U - padding + 1U);
    if (output == NULL) return NULL;
    size_t written = 0;
    for (size_t i = 0; i < input_length; i += 4U) {
        int a = base64_value((unsigned char)input[i]);
        int b = base64_value((unsigned char)input[i + 1]);
        int c = input[i + 2] == '=' ? 0 : base64_value((unsigned char)input[i + 2]);
        int d = input[i + 3] == '=' ? 0 : base64_value((unsigned char)input[i + 3]);
        if (a < 0 || b < 0 || c < 0 || d < 0 ||
            (input[i + 2] == '=' && i + 4U != input_length) ||
            (input[i + 3] == '=' && i + 4U != input_length)) {
            free(output); return NULL;
        }
        uint32_t group = ((uint32_t)a << 18U) | ((uint32_t)b << 12U) |
                         ((uint32_t)c << 6U) | (uint32_t)d;
        output[written++] = (unsigned char)(group >> 16U);
        if (input[i + 2] != '=') output[written++] = (unsigned char)(group >> 8U);
        if (input[i + 3] != '=') output[written++] = (unsigned char)group;
    }
    output[written] = '\0';
    *output_length = written;
    return output;
}

static char *base64_encode(const unsigned char *input, size_t input_length) {
    size_t encoded_length = ((input_length + 2U) / 3U) * 4U;
    char *output = malloc(encoded_length + 1U);
    if (output == NULL) return NULL;
    size_t source = 0, target = 0;
    while (source < input_length) {
        uint32_t a = input[source++];
        int have_b = source < input_length;
        uint32_t b = have_b ? input[source++] : 0;
        int have_c = source < input_length;
        uint32_t c = have_c ? input[source++] : 0;
        uint32_t group = (a << 16U) | (b << 8U) | c;
        output[target++] = base64_table[(group >> 18U) & 63U];
        output[target++] = base64_table[(group >> 12U) & 63U];
        output[target++] = have_b ? base64_table[(group >> 6U) & 63U] : '=';
        output[target++] = have_c ? base64_table[group & 63U] : '=';
    }
    output[target] = '\0';
    return output;
}

static void execute_tool(int fd, const char *encoded) {
    size_t command_length = 0;
    unsigned char *command = base64_decode(encoded, &command_length);
    if (command == NULL || command_length == 0) {
        free(command); send_error(fd, "invalid EXEC payload"); return;
    }
    int stdout_pipe[2], stderr_pipe[2];
    if (pipe(stdout_pipe) != 0 || pipe(stderr_pipe) != 0) {
        free(command); send_error(fd, "cannot create tool pipes"); return;
    }
    pid_t child = fork();
    if (child < 0) { free(command); close(stdout_pipe[0]); close(stdout_pipe[1]); close(stderr_pipe[0]); close(stderr_pipe[1]); send_error(fd, "cannot fork tool"); return; }
    if (child == 0) {
        dup2(stdout_pipe[1], STDOUT_FILENO); dup2(stderr_pipe[1], STDERR_FILENO);
        close(stdout_pipe[0]); close(stdout_pipe[1]); close(stderr_pipe[0]); close(stderr_pipe[1]);
        execl("/bin/sh", "sh", "-c", (char *)command, (char *)NULL);
        _exit(127);
    }
    free(command); close(stdout_pipe[1]); close(stderr_pipe[1]);
    unsigned char stdout_data[TOOL_OUTPUT_LIMIT], stderr_data[TOOL_OUTPUT_LIMIT];
    size_t stdout_length = 0, stderr_length = 0;
    int output_open = 1, error_open = 1;
    while (output_open || error_open) {
        fd_set readable; FD_ZERO(&readable);
        int maximum = -1;
        if (output_open) { FD_SET(stdout_pipe[0], &readable); maximum = stdout_pipe[0]; }
        if (error_open) { FD_SET(stderr_pipe[0], &readable); if (stderr_pipe[0] > maximum) maximum = stderr_pipe[0]; }
        if (select(maximum + 1, &readable, NULL, NULL, NULL) < 0) { if (errno == EINTR) continue; break; }
        int pipes[2] = {stdout_pipe[0], stderr_pipe[0]};
        unsigned char *targets[2] = {stdout_data, stderr_data};
        size_t *lengths[2] = {&stdout_length, &stderr_length};
        int *opens[2] = {&output_open, &error_open};
        for (int index = 0; index < 2; ++index) if (*opens[index] && FD_ISSET(pipes[index], &readable)) {
            unsigned char scratch[4096]; ssize_t count = read(pipes[index], scratch, sizeof(scratch));
            if (count <= 0) { *opens[index] = 0; close(pipes[index]); continue; }
            size_t copy = (size_t)count; if (copy > TOOL_OUTPUT_LIMIT - *lengths[index]) copy = TOOL_OUTPUT_LIMIT - *lengths[index];
            if (copy > 0) memcpy(targets[index] + *lengths[index], scratch, copy);
            *lengths[index] += copy;
        }
    }
    int status = 0; while (waitpid(child, &status, 0) < 0 && errno == EINTR) {}
    int exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
    char *stdout_b64 = base64_encode(stdout_data, stdout_length);
    char *stderr_b64 = base64_encode(stderr_data, stderr_length);
    if (stdout_b64 == NULL || stderr_b64 == NULL) { free(stdout_b64); free(stderr_b64); send_error(fd, "cannot encode tool output"); return; }
    dprintf(fd, "{\"ok\":true,\"result\":{\"exit_code\":%d,\"stdout_b64\":\"%s\",\"stderr_b64\":\"%s\"}}\n", exit_code, stdout_b64, stderr_b64);
    free(stdout_b64); free(stderr_b64);
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

    if (strncmp(line, "EXEC ", 5) == 0 && line[5] != '\0') {
        if (state.inflight_request[0]) {
            send_error(fd, "tool executed while LLM request is in flight");
            return;
        }
        execute_tool(fd, line + 5);
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
