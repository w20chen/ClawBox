// Copyright (c) 2026 Tencent Inc.
// SPDX-License-Identifier: Apache-2.0

package cubebox

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"

	"github.com/tencentcloud/CubeSandbox/Cubelet/api/services/cubebox/v1"
	"github.com/tencentcloud/CubeSandbox/Cubelet/api/services/errorcode/v1"
	"github.com/tencentcloud/CubeSandbox/Cubelet/storage"
	"github.com/tencentcloud/CubeSandbox/Cubelet/storage/cow"
)

// relocatePauseSnapshot performs WARM -> COLD without an SSD staging file:
// copy to a temporary file in COLD, sync, atomically publish, persist the
// authoritative catalog, then remove WARM. Any failure before the catalog
// update leaves the original snapshot authoritative and restorable.
func (s *service) relocatePauseSnapshot(
	ctx context.Context,
	req *cubebox.UpdateCubeSandboxRequest,
	rsp *cubebox.UpdateCubeSandboxResponse,
) (*cubebox.UpdateCubeSandboxResponse, error) {
	destination, direct, err := directPauseMemoryPath(req)
	if err != nil || !direct || strings.TrimSpace(req.GetAnnotations()["clawbox.snapshot.tier"]) != "cold" {
		if err == nil {
			err = fmt.Errorf("relocation requires a direct COLD destination")
		}
		rsp.Ret.RetCode = errorcode.ErrorCode_InvalidParamFormat
		rsp.Ret.RetMsg = err.Error()
		return rsp, nil
	}
	snapshotID, err := resolvePauseSnapshotID(req)
	if err != nil {
		rsp.Ret.RetCode = errorcode.ErrorCode_InvalidParamFormat
		rsp.Ret.RetMsg = err.Error()
		return rsp, nil
	}
	entry, err := storage.GetLocalSnapshotFor(ctx, cow.BackendXFS, snapshotID)
	if err != nil || entry.Kind != storage.CatalogKindPauseSnapshot || entry.MemoryKind != directMemoryKind {
		if err == nil {
			err = fmt.Errorf("snapshot %s is not a direct pause snapshot", snapshotID)
		}
		rsp.Ret.RetCode = errorcode.ErrorCode_Conflict
		rsp.Ret.RetMsg = err.Error()
		return rsp, nil
	}
	source := filepath.Clean(entry.MemoryVol)
	warmRoot := filepath.Clean(strings.TrimSpace(os.Getenv("CLAWBOX_WARM_SNAPSHOT_ROOT")))
	rel, relErr := filepath.Rel(warmRoot, source)
	if warmRoot == "." || relErr != nil || rel == "." || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		rsp.Ret.RetCode = errorcode.ErrorCode_Conflict
		rsp.Ret.RetMsg = "authoritative source is not in the configured WARM root"
		return rsp, nil
	}
	if err := os.MkdirAll(filepath.Dir(destination), 0o755); err != nil {
		rsp.Ret.RetCode = errorcode.ErrorCode_Unknown
		rsp.Ret.RetMsg = err.Error()
		return rsp, nil
	}
	if _, statErr := os.Stat(destination); statErr == nil || !os.IsNotExist(statErr) {
		rsp.Ret.RetCode = errorcode.ErrorCode_Conflict
		rsp.Ret.RetMsg = "COLD relocation destination already exists or cannot be inspected"
		return rsp, nil
	}
	tmp := destination + ".copying"
	_ = os.Remove(tmp)
	copied, err := copySnapshot(source, tmp)
	if err == nil {
		err = os.Rename(tmp, destination)
	}
	if err != nil {
		_ = os.Remove(tmp)
		rsp.Ret.RetCode = errorcode.ErrorCode_Unknown
		rsp.Ret.RetMsg = err.Error()
		return rsp, nil
	}
	previous := entry.MemoryVol
	entry.MemoryVol = destination
	if err = storage.WriteSnapshotCatalogFor(cow.BackendXFS, entry); err != nil {
		entry.MemoryVol = previous
		_ = os.Remove(destination)
		rsp.Ret.RetCode = errorcode.ErrorCode_Unknown
		rsp.Ret.RetMsg = fmt.Sprintf("persist relocated catalog: %v", err)
		return rsp, nil
	}
	if err = os.Remove(source); err != nil {
		entry.MemoryVol = previous
		revertErr := storage.WriteSnapshotCatalogFor(cow.BackendXFS, entry)
		if revertErr == nil {
			_ = os.Remove(destination)
		}
		rsp.Ret.RetCode = errorcode.ErrorCode_Unknown
		rsp.Ret.RetMsg = fmt.Sprintf("relocation copied %d bytes but could not remove WARM source: %v (catalog revert: %v)", copied, err, revertErr)
		return rsp, nil
	}
	return rsp, nil
}

func copySnapshot(source, destination string) (int64, error) {
	in, err := os.Open(source)
	if err != nil {
		return 0, err
	}
	defer in.Close()
	info, err := in.Stat()
	if err != nil {
		return 0, err
	}
	out, err := os.OpenFile(destination, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return 0, err
	}
	written, copyErr := io.Copy(out, in)
	if copyErr == nil && written != info.Size() {
		copyErr = fmt.Errorf("short snapshot copy: %d of %d bytes", written, info.Size())
	}
	if copyErr == nil {
		copyErr = out.Sync()
	}
	if copyErr == nil && os.Getenv("CUBE_RESTORE_PRIVATE_COPY") == "1" {
		// A COLD snapshot must not retain an unaccounted DRAM cache copy.
		copyErr = unix.Fadvise(int(out.Fd()), 0, 0, unix.FADV_DONTNEED)
	}
	closeErr := out.Close()
	if copyErr == nil {
		copyErr = closeErr
	}
	return written, copyErr
}
