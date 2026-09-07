#!/usr/bin/env python3
"""Patch the pinned guest collector to avoid task_struct ABI dependence."""

from pathlib import Path


TARGET = Path(
    "/opt/clawtune-guest/services/sidecar/src/tool_resource/telemetry.py"
)
CLAUSE_TARGET = Path(
    "/opt/clawtune-guest/services/sidecar/src/tool_resource/clause_bridge.py"
)
FORK_OLD = """/* Fork lineage must be TGID-consistent with every other event (which key on
 * tgid = pid_tgid>>32). The tracepoint's parent_pid/child_pid are TIDs; using
 * parent_pid directly breaks lineage when a non-leader thread forks. Record the
 * forking task's TGID as the parent. child_pid is always the new TID; it is also
 * the TGID for a process fork and supplies the zero I/O baseline for both
 * process and thread children. */
RAW_TRACEPOINT_PROBE(sched_process_fork) {
    if (!wanted()) return 0;
    struct task_struct *child = (struct task_struct *)ctx->args[1];
    u32 child_tid = 0;
    bpf_probe_read_kernel(&child_tid, sizeof(child_tid), &child->pid);
    struct task_key_t child_key = {
        .tid = child_tid,
        .task_ptr = (u64)child,
    };
    current_seq.delete(&child_key);
    pending_seq.delete(&child_key);
"""
FORK_NEW = """/* Fork lineage must be TGID-consistent with every other event (which key on
 * tgid = pid_tgid>>32). Use the stable tracepoint child_pid field rather than
 * dereferencing task_struct: minimal guest kernels can expose headers whose
 * task_struct layout does not match the running kernel, yielding corrupt child
 * IDs and silently dropping every descendant exec from attribution. */
TRACEPOINT_PROBE(sched, sched_process_fork) {
    if (!wanted()) return 0;
    u32 child_tid = args->child_pid;
"""

ISOLATION_OLD = '''    candidates = {
        clause.host_pid
        for clause in clauses
        if clause.host_pid in root_pids
        and len(clause.argv) >= 3
        and Path(clause.argv[0]).name in {"sh", "dash", "bash"}
        and clause.argv[1] in {"-c", "-lc"}
        and clause.argv[2] == command
    }
'''
ISOLATION_NEW = '''    exact_candidates = {
        clause.host_pid
        for clause in clauses
        if clause.host_pid in root_pids
        and len(clause.argv) >= 3
        and Path(clause.argv[0]).name in {"sh", "dash", "bash"}
        and clause.argv[1] in {"-c", "-lc"}
        and clause.argv[2] == command
    }
    truncated_candidates = {
        clause.host_pid
        for clause in clauses
        if clause.host_pid in root_pids
        and len(clause.argv) >= 3
        and Path(clause.argv[0]).name in {"sh", "dash", "bash"}
        and clause.argv[1] in {"-c", "-lc"}
        and bool(clause.argv_capture_flags & (1 << 2))
        and bool(clause.argv[2])
        and command.startswith(clause.argv[2])
    }
    candidates = exact_candidates or truncated_candidates
    candidate_evidence = (
        "exact_registered_root_shell"
        if exact_candidates
        else "unique_truncated_registered_root_shell_prefix"
    )
'''

COMMENT_OLD = '''            # first exact ``/bin/sh -c <registered command>`` image in the
            # exclusive per-execution cgroup is that same trusted process.
            # Remap only on one exact root match; ambiguity still fails closed.
'''
COMMENT_NEW = '''            # first matching ``/bin/sh -c <registered command>`` image in the
            # exclusive per-execution cgroup is that same trusted process.
            # BPF argv capture is intentionally bounded, so a long command may
            # match only by its explicitly truncated argv[2] prefix. Remap only
            # on one root match; ambiguity still fails closed.
'''

ALIGNMENT_OLD = '''    if _incomplete_capture(initial):
        return None, "runtime_argv_incomplete"
    runtime = tuple(initial.argv)
    static = tuple(str(word) for word in clause["argv"])
    if not runtime or not static:
        return None, "empty_argv"
    if runtime[0] != static[0]:
        return None, "executable_head_mismatch"
    if "/" in static[0] and initial.requested_executable_path != static[0]:
        return None, "requested_executable_path_mismatch"

    intents = clause.get("word_intents")
'''
ALIGNMENT_NEW = '''    runtime = tuple(initial.argv)
    static = tuple(str(word) for word in clause["argv"])
    if not runtime or not static:
        return None, "empty_argv"
    if runtime[0] != static[0]:
        return None, "executable_head_mismatch"
    if "/" in static[0] and initial.requested_executable_path != static[0]:
        return None, "requested_executable_path_mismatch"

    intents = clause.get("word_intents")
    if _incomplete_capture(initial):
        # Long literal argv words are intentionally bounded by the BPF
        # collector. They can still prove identity against the registered
        # static command when every word and the kernel-reported argc align,
        # with only explicitly flagged words compared as non-empty prefixes.
        # Capped argv, path evidence truncation, and shell expansions remain
        # unprovable and fail closed.
        truncated = set(initial.truncated_words)
        if (
            initial.argv_capped
            or initial.requested_executable_path_truncated
            or initial.bprm_evidence_truncated
            or not truncated
            or initial.exact_argc != len(static)
            or len(runtime) != len(static)
            or not isinstance(intents, list)
            or len(intents) != len(static)
            or any(index < 0 or index >= len(runtime) for index in truncated)
        ):
            return None, "runtime_argv_incomplete"
        for index, intent in enumerate(intents):
            components = intent.get("components")
            if not isinstance(components, list) or any(
                component.get("kind") != "literal" for component in components
            ):
                return None, "runtime_argv_incomplete"
            expected = str(intent.get("cooked", ""))
            observed = runtime[index]
            if index in truncated:
                if not observed or not expected.startswith(observed):
                    return None, "truncated_word_prefix_mismatch"
            elif observed != expected:
                return None, "word_mismatch"
        return "initial_invocation_unique_truncated_literal_prefix", "ok"
'''

INCOMPLETE_OLD = '''    ownership_only_images = owned_exec_images - mapping_anchors
    incomplete_capture_gaps = [
        MappingGap(
            "runtime_argv_incomplete",
            f"pid={image.host_pid} exec_seq={image.exec_seq} has capped or "
            "truncated argv",
        )
        for image in exec_images
        if _incomplete_capture(image)
        and (image.host_pid, image.exec_seq) not in ownership_only_images
    ]
'''
INCOMPLETE_NEW = '''    ownership_only_images = owned_exec_images - mapping_anchors
    proven_truncated_mapping_images = {
        (pid, chains[pid][0].exec_seq)
        for si, pid in assigned.items()
        if evidence[si] == "initial_invocation_unique_truncated_literal_prefix"
    }
    incomplete_capture_gaps = [
        MappingGap(
            "runtime_argv_incomplete",
            f"pid={image.host_pid} exec_seq={image.exec_seq} has capped or "
            "truncated argv",
        )
        for image in exec_images
        if _incomplete_capture(image)
        and not is_shell(image.host_pid)
        and (image.host_pid, image.exec_seq) not in ownership_only_images
        and (image.host_pid, image.exec_seq) not in proven_truncated_mapping_images
    ]
'''


def main() -> None:
    source = TARGET.read_text()
    replacements = (
        ("fork collector block", FORK_OLD, FORK_NEW),
        ("call isolation candidate block", ISOLATION_OLD, ISOLATION_NEW),
        ("call isolation remap comment", COMMENT_OLD, COMMENT_NEW),
        (
            "call isolation evidence",
            '            provenance["remap_evidence"] = "exact_registered_root_shell"\n',
            '            provenance["remap_evidence"] = candidate_evidence\n',
        ),
    )
    for label, old, new in replacements:
        count = source.count(old)
        if count != 1:
            raise SystemExit(f"expected one pinned {label}, found {count}")
        source = source.replace(old, new)
    TARGET.write_text(source)

    clause_source = CLAUSE_TARGET.read_text()
    for label, old, new in (
        ("truncated literal alignment", ALIGNMENT_OLD, ALIGNMENT_NEW),
        ("incomplete capture filtering", INCOMPLETE_OLD, INCOMPLETE_NEW),
    ):
        count = clause_source.count(old)
        if count != 1:
            raise SystemExit(f"expected one pinned {label}, found {count}")
        clause_source = clause_source.replace(old, new)
    CLAUSE_TARGET.write_text(clause_source)


if __name__ == "__main__":
    main()
