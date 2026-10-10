Vendored from https://github.com/eunomia-bpf/agentcgroup at commit
`551ca1689b7d2d30db6b8a8613414750103cc68b`, file `agentcg/memcg_controller.py`.

SHA256: `5fcb60bea882aa1bd19f8c646296630ca5425cd07d9bed1eb7b684231aeee62c`.
The Python file is byte-for-byte unchanged. See LICENSE for upstream licensing.

ClawBox loads a private module instance, selects `CgroupMemcgController`
explicitly and makes cgroup read/write failures fatal. Its independent polling
clock and fixed VM/session hierarchy are implemented outside the vendored file.
CPU scheduling, the shell wrapper and the BPF backend are not enabled.
