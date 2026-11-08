"""Prove a loop image's payload extents reside on one reviewed physical SSD."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import time
import uuid


def _run(*args):
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def _write(path: Path, value):
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _disk(device: str) -> str:
    rows = _run("lsblk", "-s", "-n", "-r", "-p", "-o", "NAME,TYPE", device).splitlines()
    disks = {row.split()[0] for row in rows if row.split()[-1] == "disk"}
    if len(disks) != 1:
        raise RuntimeError(f"device does not resolve to exactly one physical disk: {device}")
    return disks.pop()


def linear_segments(source: str) -> list[dict]:
    if not Path(source).resolve().name.startswith("dm-"):
        return [{"start": 0, "end": 2**64 - 1, "device": source, "disk": _disk(source), "device_offset": 0}]
    result = []
    for line in _run("dmsetup", "table", source).splitlines():
        start, length, kind, device, offset = line.split()
        if kind != "linear":
            raise ValueError("physical image placement supports only linear device-mapper segments")
        path = str(Path("/dev/block") / device)
        result.append({"start": int(start) * 512, "end": (int(start) + int(length)) * 512,
                       "device": str(Path(path).resolve()), "disk": _disk(path), "device_offset": int(offset) * 512})
    return result


def inspect_backing(source: str) -> dict:
    """Read-only XFS allocation-group capacity and linear physical placement."""
    raw = _run("xfs_db", "-r", "-c", "sb 0", "-c", "p blocksize agblocks agcount", source)
    geometry = {k: int(v) for k, v in re.findall(r"(blocksize|agblocks|agcount) = (\d+)", raw)}
    segments = linear_segments(source)
    commands = ["xfs_db", "-r"]
    for ag in range(geometry["agcount"]):
        commands += ["-c", f"agf {ag}", "-c", "p seqno length freeblks longest"]
    rows = []
    for block in re.split(r"(?=seqno = )", _run(*commands, source)):
        values = {k: int(v) for k, v in re.findall(r"(seqno|length|freeblks|longest) = (\d+)", block)}
        if not values:
            continue
        start = values["seqno"] * geometry["agblocks"] * geometry["blocksize"]
        end = start + values["length"] * geometry["blocksize"]
        disks = sorted({s["disk"] for s in segments if s["start"] < end and s["end"] > start})
        rows.append({"allocation_group": values["seqno"], "physical_devices": disks,
                     "free_bytes": values["freeblks"] * geometry["blocksize"],
                     "largest_free_extent_bytes": values["longest"] * geometry["blocksize"]})
    return {"source": source, "filesystem_uuid": _run("blkid", "-s", "UUID", "-o", "value", source),
            "geometry": geometry, "segments": segments, "allocation_groups": rows,
            "note": "Mounted metadata observation, not a reservation. reserve-image verifies every actual extent."}


def image_extents(image: Path) -> list[dict]:
    size = image.stat().st_size
    result, start = [], 0
    with image.open("rb", buffering=0) as stream:
        while start < size:
            count = 256
            buffer = bytearray(32 + count * 56)
            struct.pack_into("=QQIIII", buffer, 0, start, size - start, 1 if start == 0 else 0, 0, count, 0)
            fcntl.ioctl(stream.fileno(), 0xC020660B, buffer, True)  # FS_IOC_FIEMAP
            mapped = struct.unpack_from("=QQIIII", buffer)[3]
            if not mapped:
                break
            last_flags = 0
            for index in range(mapped):
                logical, physical, length, _, _, flags, _, _, _ = struct.unpack_from("=QQQQQIIII", buffer, 32 + 56 * index)
                length = min(length, size - logical)
                if flags & (0x2 | 0x4 | 0x8 | 0x80 | 0x100 | 0x200 | 0x400 | 0x1000 | 0x2000):
                    raise RuntimeError(f"unprovable/shared backing extent flags: {flags:#x}")
                if logical != start:
                    raise RuntimeError(f"backing image has an unallocated hole at {start}")
                result.append({"logical": logical, "physical": physical, "length": length, "flags": flags})
                start = logical + length
                last_flags = flags
            if last_flags & 1:
                break
    if start != size:
        raise RuntimeError(f"image reservation incomplete: mapped {start} of {size} bytes")
    return result


def verify_image(image: Path, source: str, physical_device: str) -> dict:
    expected = _disk(physical_device)
    segments = linear_segments(source)
    extents = image_extents(image)
    devices: dict[str, int] = {}
    for extent in extents:
        position = extent["physical"]
        remaining = extent["length"]
        while remaining:
            segment = next((s for s in segments if s["start"] <= position < s["end"]), None)
            if segment is None:
                raise RuntimeError("backing extent lies outside the verified linear device map")
            chunk = min(remaining, segment["end"] - position)
            devices[segment["disk"]] = devices.get(segment["disk"], 0) + chunk
            position += chunk
            remaining -= chunk
    result = {"image": str(image), "source": source, "physical_device": expected,
              "filesystem_uuid": _run("blkid", "-s", "UUID", "-o", "value", source),
              "physical_identity": json.loads(_run("lsblk", "-d", "-J", "-b", "-o",
                  "PATH,SERIAL,MODEL,SIZE,WWN,UUID", expected))["blockdevices"][0],
              "logical_bytes": image.stat().st_size, "allocated_bytes": image.stat().st_blocks * 512,
              "device_bytes": devices, "segments": segments, "extents": extents,
              "verified_unix_s": time.time(), "passed": set(devices) == {expected}}
    return result


def allocate_image(image: Path, size_bytes: int, source: str, physical_device: str,
                   allocation_group: int) -> dict:
    """Create a new owned inode in a selected AG, reserve, then verify every byte.

    No existing filesystem or block device is formatted. A failed new reservation
    is removed only after matching its recorded inode; evidence is retained.
    """
    image = image.absolute()
    if image.exists():
        raise RuntimeError(f"refusing to replace existing allocation image: {image}")
    info = _run("xfs_db", "-r", "-c", "sb 0", "-c", "p agblklog inopblog agcount", source)
    values = {key: int(value) for key, value in re.findall(r"(agblklog|inopblog|agcount) = (\d+)", info)}
    if not 0 <= allocation_group < values["agcount"]:
        raise ValueError("allocation_group is outside the backing XFS filesystem")
    inode_shift = values["agblklog"] + values["inopblog"]
    image.parent.mkdir(parents=True, exist_ok=True)
    if image.parent.stat().st_dev != Path(source).stat().st_rdev:
        raise RuntimeError("image parent is not on the reviewed backing block device")
    owner = uuid.uuid4().hex
    receipt = image.with_name(image.name + ".allocation-" + owner + ".json")
    scratch = image.parent / (".allocation-" + owner)
    evidence = {"owner": owner, "image": str(image), "size_bytes": size_bytes,
                "source": source, "physical_device": physical_device,
                "allocation_group": allocation_group, "status": "allocating",
                "created_unix_s": time.time(), "scratch": str(scratch), "candidate_ags": []}
    _write(receipt, evidence)
    created = []
    inode = None
    try:
        scratch.mkdir(mode=0o700)
        selected = None
        for index in range(values["agcount"] * 3):
            candidate = scratch / str(index)
            candidate.mkdir()
            created.append(candidate)
            ag = candidate.stat().st_ino >> inode_shift
            evidence["candidate_ags"].append(ag)
            if ag == allocation_group:
                selected = candidate
                break
        if selected is None:
            raise RuntimeError("no new directory inode reached the reviewed allocation group")
        temporary = selected / "image"
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        os.link(temporary, image)  # Exclusive destination creation; inode AG is retained.
        temporary.unlink()
        inode = image.stat().st_ino
        evidence["inode"] = inode
        evidence["inode_ag"] = inode >> inode_shift
        _write(receipt, evidence)
        if evidence["inode_ag"] != allocation_group:
            raise RuntimeError("new image inode did not retain the selected allocation group")
        with image.open("r+b", buffering=0) as stream:
            os.posix_fallocate(stream.fileno(), 0, size_bytes)
            os.fsync(stream.fileno())
        proof = verify_image(image, source, physical_device)
        evidence["proof"] = proof
        if not proof["passed"]:
            raise RuntimeError(f"allocation crossed reviewed physical SSD: {proof['device_bytes']}")
        evidence["status"] = "verified"
        _write(receipt, evidence)
        return {**evidence, "receipt": str(receipt)}
    except BaseException as exc:
        evidence.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        if inode is not None and image.exists() and image.stat().st_ino == inode:
            image.unlink()
            evidence["owned_failed_image_removed"] = True
        _write(receipt, evidence)
        raise
    finally:
        for path in reversed(created):
            path.rmdir()
        if scratch.exists():
            scratch.rmdir()


def allocate_chunked_image(image: Path, size_bytes: int, source: str, physical_device: str,
                           allocation_group: int, allocation_group_end: int,
                           chunk_bytes: int = 16 * 1024**3,
                           resume_receipt: Path | None = None) -> dict:
    """Assemble written, individually verified extents from a reviewed AG range.

    XFS may balance a large allocation across physical devices. Small allocations
    are verified before initialization and reflink-range assembly. Writing zeroes
    is necessary: reflinking unwritten extents would create unallocated holes.
    """
    image = image.absolute()
    if image.exists() and resume_receipt is None:
        raise RuntimeError(f"refusing to replace existing allocation image: {image}")
    if not 0 <= allocation_group <= allocation_group_end or chunk_bytes < 1024**2:
        raise ValueError("invalid allocation group range or chunk size")
    image.parent.mkdir(parents=True, exist_ok=True)
    if resume_receipt:
        receipt = resume_receipt.absolute()
        evidence = json.loads(receipt.read_text())
        expected = {"image": str(image), "size_bytes": size_bytes, "source": source,
                    "physical_device": physical_device, "allocation_group": allocation_group,
                    "allocation_group_end": allocation_group_end,
                    "method": "verified-written-range-assembly"}
        if any(evidence.get(k) != value for k, value in expected.items()):
            raise RuntimeError("resume arguments differ from the owned allocation receipt")
        if evidence.get("inode") != image.stat().st_ino or evidence.get("completed_bytes") != image.stat().st_size:
            raise RuntimeError("partial image inode/size differs from recorded completed reservation")
        owner = evidence["owner"]
        proof = verify_image(image, source, physical_device)
        if not proof["passed"]:
            raise RuntimeError("partial image failed physical verification before resume")
        if evidence.get("status") == "verified":
            return {**evidence, "receipt": str(receipt)}
        evidence["resumed_unix_s"] = time.time()
        evidence["status"] = "allocating"
        evidence.pop("error", None)
        fd = os.open(image, os.O_RDWR)
    else:
        owner = uuid.uuid4().hex
        receipt = image.with_name(image.name + ".allocation-" + owner + ".json")
        evidence = {"owner": owner, "image": str(image), "size_bytes": size_bytes,
                "source": source, "physical_device": physical_device,
                "allocation_group": allocation_group, "allocation_group_end": allocation_group_end,
                "status": "allocating", "method": "verified-written-range-assembly",
                "created_unix_s": time.time(), "chunks": [], "failures": [], "completed_bytes": 0}
        _write(receipt, evidence)
        fd = os.open(image, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    chunks = image.parent / (".allocation-chunks-" + owner)
    inode = os.fstat(fd).st_ino
    evidence["inode"] = inode
    chunks.mkdir(mode=0o700, exist_ok=bool(resume_receipt))
    _write(receipt, evidence)
    try:
        offset, index = evidence["completed_bytes"], len(evidence["chunks"]) + len(evidence["failures"])
        for ag in range(allocation_group, allocation_group_end + 1):
            piece_bytes = chunk_bytes
            while offset < size_bytes:
                length = min(piece_bytes, size_bytes - offset)
                chunk = chunks / f"ag{ag}-{index}-{uuid.uuid4().hex[:8]}.image"
                index += 1
                try:
                    part = allocate_image(chunk, length, source, physical_device, ag)
                except RuntimeError as exc:
                    evidence["failures"].append({"ag": ag, "error": str(exc)})
                    _write(receipt, evidence)
                    if "allocation crossed" in str(exc) and piece_bytes > 1024**2:
                        piece_bytes = max(1024**2, piece_bytes // 2)
                        continue
                    break
                part_inode = chunk.stat().st_ino
                start = time.monotonic()
                _run("dd", "if=/dev/zero", "of=" + str(chunk), "bs=8M",
                     "count=" + str(length), "iflag=count_bytes", "oflag=direct",
                     "conv=notrunc,fsync", "status=none")
                written = verify_image(chunk, source, physical_device)
                if not written["passed"]:
                    raise RuntimeError("written chunk changed physical placement")
                with chunk.open("rb", buffering=0) as stream:
                    fcntl.ioctl(fd, 0x4020940D, struct.pack("=qQQQ", stream.fileno(), 0, length, offset))
                os.fsync(fd)
                if chunk.stat().st_ino != part_inode:
                    raise RuntimeError("owned chunk inode changed before alias removal")
                chunk.unlink()
                offset += length
                evidence["chunks"].append({"receipt": part["receipt"], "offset": offset - length,
                    "length": length, "ag": ag, "write_seconds": time.monotonic() - start,
                    "physical_proof": written, "temporary_alias_removed": True})
                evidence["completed_bytes"] = offset
                _write(receipt, evidence)
            if offset == size_bytes:
                break
        if offset != size_bytes:
            raise RuntimeError(f"reviewed AGs supplied only {offset} of {size_bytes} bytes")
        os.fsync(fd)
        proof = verify_image(image, source, physical_device)
        evidence["proof"] = proof
        if not proof["passed"]:
            raise RuntimeError(f"assembled image crossed reviewed SSD: {proof['device_bytes']}")
        evidence["status"] = "verified"
        _write(receipt, evidence)
        return {**evidence, "receipt": str(receipt)}
    except BaseException as exc:
        evidence.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        # Keep partial owned reservations and receipts for inspection/recovery.
        _write(receipt, evidence)
        raise RuntimeError(f"{exc}; owned partial reservation preserved; repeat with --resume-receipt {receipt}") from exc
    finally:
        os.close(fd)
