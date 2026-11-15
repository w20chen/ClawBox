"""Native snapshot metadata must count the full hardlinked RAM closure."""
import json
import os

import pytest

from clawbox.cube.snapshot import read_snapshot_metadata


@pytest.mark.skipif(os.name != "posix", reason="native st_blocks accounting is Linux-only")
def test_ram_lineage_counts_unique_layer_inodes_and_rejects_missing_base(tmp_path):
    page = os.sysconf("SC_PAGE_SIZE")
    head = tmp_path / "guest.mem"
    layers = tmp_path / "guest.mem.layers"
    layers.mkdir()
    base = layers / "000000.mem"
    delta = layers / "000001.mem"
    base.write_bytes(b"A" * (4 * page))
    with delta.open("wb") as file:
        file.truncate(4 * page)
        file.seek(page)
        file.write(b"\0" * page)
    os.link(delta, head)
    (tmp_path / "guest.mem.lineage.json").write_text(json.dumps({
        "schema_version": 1,
        "logical_bytes": 4 * page,
        "transferred_bytes": page,
        "dirty_bytes": page,
        "full_base": False,
        "layers": [{"name": "000000.mem"}, {"name": "000001.mem"}],
    }))
    metadata = read_snapshot_metadata(str(head), require_lineage=True)
    assert metadata["logical_bytes"] == 4 * page
    assert metadata["dirty_bytes"] == metadata["transferred_bytes"] == page
    assert metadata["allocated_bytes"] == (base.stat().st_blocks + delta.stat().st_blocks +
                                            (tmp_path / "guest.mem.lineage.json").stat().st_blocks) * 512
    base.unlink()
    with pytest.raises(FileNotFoundError):
        read_snapshot_metadata(str(head), require_lineage=True)


@pytest.mark.skipif(os.name != "posix", reason="native st_blocks accounting is Linux-only")
def test_cold_lineages_expose_shared_inode_without_double_counting_transfer(tmp_path):
    page = os.sysconf("SC_PAGE_SIZE")
    base = tmp_path / "template.mem"
    base.write_bytes(b"A" * (4 * page))
    base_bytes = base.stat().st_blocks * 512
    observations = []
    for index in range(2):
        head = tmp_path / f"cold{index}.mem"
        layers = tmp_path / f"cold{index}.mem.layers"
        layers.mkdir()
        os.link(base, layers / "000000.mem")
        delta = layers / "000001.mem"
        delta.write_bytes(bytes([index]) * (4 * page))
        os.link(delta, head)
        sidecar = tmp_path / f"cold{index}.mem.lineage.json"
        sidecar.write_text(json.dumps({
            "schema_version": 1, "logical_bytes": 4 * page,
            "transferred_bytes": 4 * page, "full_base": False,
            "reused_base_allocated_bytes": base_bytes,
            "layers": [{"name": "000000.mem"}, {"name": "000001.mem"}],
        }))
        observations.append(read_snapshot_metadata(str(head), require_lineage=True))
    base.unlink()
    a, b = observations
    assert a["transferred_bytes"] == b["transferred_bytes"] == 4 * page
    assert a["reused_base_allocated_bytes"] == b["reused_base_allocated_bytes"] == base_bytes
    assert a["private_delta_bytes"] == a["layer_allocations"][1]["allocated_bytes"]
    assert b["private_delta_bytes"] == b["layer_allocations"][1]["allocated_bytes"]
    assert a["layer_allocations"][0]["identity"] == b["layer_allocations"][0]["identity"]
    assert a["layer_allocations"][1]["identity"] != b["layer_allocations"][1]["identity"]
    unique = {entry["identity"]: entry["allocated_bytes"]
              for item in observations for entry in item["layer_allocations"]}
    assert sum(unique.values()) == base_bytes + sum(
        item["layer_allocations"][1]["allocated_bytes"] for item in observations)
    assert read_snapshot_metadata(str(tmp_path / "cold1.mem"), require_lineage=True)["layer_allocations"] == b["layer_allocations"]


@pytest.mark.skipif(os.name != "posix", reason="native st_blocks accounting is Linux-only")
def test_external_template_base_is_validated_but_not_charged_to_warm(tmp_path):
    page = os.sysconf("SC_PAGE_SIZE")
    template = tmp_path / "template.mem"
    template.write_bytes(b"A" * (4 * page))
    head = tmp_path / "guest.mem"
    layers = tmp_path / "guest.mem.layers"
    layers.mkdir()
    delta = layers / "000001.mem"
    with delta.open("wb") as file:
        file.truncate(4 * page)
        file.seek(page)
        file.write(b"B" * page)
    os.link(delta, head)
    sidecar = tmp_path / "guest.mem.lineage.json"
    sidecar.write_text(json.dumps({
        "schema_version": 1,
        "logical_bytes": 4 * page,
        "transferred_bytes": page,
        "dirty_bytes": page,
        "full_base": False,
        "layers": [
            {"name": "000000.mem", "external_path": str(template)},
            {"name": "000001.mem"},
        ],
    }))

    metadata = read_snapshot_metadata(str(head), require_lineage=True)

    assert metadata["external_base_path"] == str(template)
    assert metadata["full_base"] is False
    assert metadata["allocated_bytes"] == (delta.stat().st_blocks + sidecar.stat().st_blocks) * 512
    template.unlink()
    with pytest.raises(FileNotFoundError):
        read_snapshot_metadata(str(head), require_lineage=True)


@pytest.mark.skipif(os.name != "posix", reason="native st_blocks accounting is Linux-only")
def test_managed_external_layers_can_occupy_any_position_and_are_not_warm_bytes(tmp_path):
    page = os.sysconf("SC_PAGE_SIZE")
    managed = []
    for index in range(2):
        layer_id = f"cold-layer-{index}"
        path = tmp_path / "cold" / "layers" / layer_id / "000000.mem"
        path.parent.mkdir(parents=True)
        path.write_bytes(bytes([index]) * (4 * page))
        managed.append((layer_id, path))

    label = tmp_path / "guest.mem"
    layers = tmp_path / "guest.mem.layers"
    layers.mkdir()
    head = layers / "000002.mem"
    with head.open("wb") as file:
        file.truncate(4 * page)
        file.seek(page)
        file.write(b"B" * page)
    os.link(head, label)
    sidecar = tmp_path / "guest.mem.lineage.json"
    sidecar.write_text(json.dumps({
        "schema_version": 2,
        "logical_bytes": 4 * page,
        "transferred_bytes": page,
        "dirty_bytes": page,
        "full_base": False,
        "layers": [
            {"name": "000000.mem", "ranges": [{"gpa": 0, "offset": 0, "length": page}],
             "external_path": str(managed[0][1]), "layer_id": managed[0][0], "managed": True},
            {"name": "000001.mem", "ranges": [{"gpa": page, "offset": page, "length": page}],
             "external_path": str(managed[1][1]), "layer_id": managed[1][0], "managed": True},
            {"name": "000002.mem", "ranges": [{"gpa": 2 * page, "offset": 2 * page,
                                               "length": page}]},
        ],
    }))

    metadata = read_snapshot_metadata(str(label), require_lineage=True)

    assert metadata["lineage_schema_version"] == 2
    assert metadata["external_layer_paths"] == [str(path) for _, path in managed]
    assert len(metadata["external_layer_allocations"]) == 2
    assert all(item["managed"] for item in metadata["external_layer_allocations"])
    assert metadata["managed_external_bytes"] == sum(
        path.stat().st_blocks * 512 for _, path in managed
    )
    assert metadata["allocated_bytes"] == (
        head.stat().st_blocks + sidecar.stat().st_blocks
    ) * 512


@pytest.mark.skipif(os.name != "posix", reason="native st_blocks accounting is Linux-only")
def test_unmanaged_external_layer_above_zero_is_rejected(tmp_path):
    page = os.sysconf("SC_PAGE_SIZE")
    head = tmp_path / "guest.mem"
    head.write_bytes(b"A" * (2 * page))
    layers = tmp_path / "guest.mem.layers"
    layers.mkdir()
    (layers / "000000.mem").write_bytes(b"A" * (2 * page))
    external = tmp_path / "external.mem"
    external.write_bytes(b"B" * (2 * page))
    (tmp_path / "guest.mem.lineage.json").write_text(json.dumps({
        "schema_version": 2,
        "logical_bytes": 2 * page,
        "layers": [
            {"name": "000000.mem", "ranges": []},
            {"name": "000001.mem", "ranges": [], "external_path": str(external)},
        ],
    }))

    with pytest.raises(RuntimeError, match="legacy external.*layer zero"):
        read_snapshot_metadata(str(head), require_lineage=True)


@pytest.mark.skipif(os.name != "posix", reason="native st_blocks accounting is Linux-only")
def test_zero_dirty_managed_lineage_accepts_an_empty_placeholder_head(tmp_path):
    page = os.sysconf("SC_PAGE_SIZE")
    layer_id = "cold-zero-dirty"
    inherited = tmp_path / "cold" / "layers" / layer_id / "000000.mem"
    inherited.parent.mkdir(parents=True)
    inherited.write_bytes(b"A" * (4 * page))
    head = tmp_path / "guest.mem"
    with head.open("wb") as file:
        file.truncate(4 * page)
    (tmp_path / "guest.mem.lineage.json").write_text(json.dumps({
        "schema_version": 2,
        "logical_bytes": 4 * page,
        "transferred_bytes": 0,
        "dirty_bytes": 0,
        "full_base": False,
        "layers": [{
            "name": "000000.mem",
            "ranges": [{"gpa": 0, "offset": 0, "length": 4 * page}],
            "external_path": str(inherited),
            "layer_id": layer_id,
            "managed": True,
        }],
    }))

    metadata = read_snapshot_metadata(str(head), require_lineage=True)

    assert metadata["transferred_bytes"] == 0
    assert metadata["private_delta_bytes"] == 0
    assert metadata["managed_external_bytes"] == inherited.stat().st_blocks * 512
