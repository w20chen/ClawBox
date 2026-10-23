#!/usr/bin/env python3
import collections
import json
from pathlib import Path


ROOT = Path("/data/clawbox-acceptance-20261022T102744Z/fidelity/cases")


def distributions(values):
    return dict(sorted(collections.Counter(values).items(), key=lambda item: str(item[0])))


main_paths = sorted(path for path in ROOT.glob("*.json") if not path.name.endswith(".guest.json"))
guest_paths = sorted(ROOT.glob("*.guest.json"))
main = [json.loads(path.read_text()) for path in main_paths]
guest = [json.loads(path.read_text()) for path in guest_paths]

summary = {
    "main_receipts": len(main),
    "guest_receipts": len(guest),
    "passed": sum(item.get("passed") is True for item in main),
    "nonempty_mismatches": sum(bool(item.get("mismatches")) for item in main),
    "mismatch_keys": distributions(
        key for item in main for key in (item.get("mismatches") or {}).keys()
    ),
    "source_identity": {
        "original_image_present": sum(bool(item.get("original_image")) for item in main),
        "distinct_original_images": len({item.get("original_image") for item in main}),
        "original_manifest_present": sum(bool(item.get("original_manifest")) for item in main),
        "distinct_original_manifests": len({item.get("original_manifest") for item in main}),
        "original_git_head_present": sum(bool(item.get("original_git_head")) for item in main),
        "git_status_matches": sum(item.get("actual_git_status") == item.get("original_git_status") for item in main),
        "tracked_source_sha256_present": sum(bool(item.get("tracked_source_sha256")) for item in main),
        "checked_source_files_total": sum(item.get("checked_source_files", 0) for item in main),
        "checked_source_files_min": min(item.get("checked_source_files", 0) for item in main),
        "checked_source_files_max": max(item.get("checked_source_files", 0) for item in main),
    },
    "dependency_identity": {
        "python_package_count_min": min(item.get("python_package_count", 0) for item in main),
        "python_package_count_max": max(item.get("python_package_count", 0) for item in main),
        "conda_package_count_min": min(item.get("conda_package_count", 0) for item in main),
        "conda_package_count_max": max(item.get("conda_package_count", 0) for item in main),
        "task_python_versions": distributions(item.get("task_python") for item in main),
    },
    "trace_runtime_proof": {
        "trace_evidence_count": sum(len(item.get("trace_evidence") or []) for item in main),
        "container_arch": distributions(
            evidence.get("runtime_proof", {}).get("tool_container_arch")
            for item in main for evidence in (item.get("trace_evidence") or [])
        ),
        "container_python": distributions(
            evidence.get("runtime_proof", {}).get("tool_container_python")
            for item in main for evidence in (item.get("trace_evidence") or [])
        ),
        "container_user": distributions(
            evidence.get("runtime_proof", {}).get("tool_container_user")
            for item in main for evidence in (item.get("trace_evidence") or [])
        ),
        "container_workdir": distributions(
            evidence.get("runtime_proof", {}).get("tool_container_workdir")
            for item in main for evidence in (item.get("trace_evidence") or [])
        ),
    },
    "documented_differences": {
        "architecture": distributions(
            item.get("documented_differences", {}).get("architecture") for item in main
        ),
        "recording_time_digest_available": distributions(
            item.get("documented_differences", {}).get("recording_time_image_digest_available")
            for item in main
        ),
        "cases_with_omitted_os_packages": sum(
            bool(item.get("documented_differences", {}).get("omitted_os_packages")) for item in main
        ),
        "omitted_os_package_names": distributions(
            package
            for item in main
            for package in item.get("documented_differences", {}).get("omitted_os_packages", {})
        ),
        "cases_with_extra_native_conda_packages": sum(
            bool(item.get("documented_differences", {}).get("extra_native_conda_packages")) for item in main
        ),
        "extra_native_conda_package_names": distributions(
            package
            for item in main
            for package in item.get("documented_differences", {}).get("extra_native_conda_packages", {})
        ),
        "cases_with_removed_generated_x86_artifacts": sum(
            bool(item.get("documented_differences", {}).get("removed_generated_x86_artifacts")) for item in main
        ),
    },
    "current_guest": {
        "arch": distributions(item.get("arch") for item in guest),
        "uid": distributions(item.get("uid") for item in guest),
        "system_python": distributions(item.get("system_python") for item in guest),
        "x86_elf_file_count": sum(len(item.get("x86_elf_files") or []) for item in guest),
        "task_inventory_present": sum(bool(item.get("task_inventory")) for item in guest),
        "dpkg_inventory_present": sum(bool(item.get("dpkg")) for item in guest),
    },
}

print(json.dumps(summary, indent=2, sort_keys=True))
