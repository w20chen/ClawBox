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
