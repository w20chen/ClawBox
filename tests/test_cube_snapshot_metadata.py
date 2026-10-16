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
