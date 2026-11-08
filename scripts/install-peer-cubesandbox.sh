#!/usr/bin/env bash
# Install the matching peer-domain API/Master/Cubelet on an idle host.
set -Eeuo pipefail
if [[ ${1:-} == --rollback ]]; then
  (( $# == 2 && EUID == 0 )) || { echo 'usage: sudo bash scripts/install-peer-cubesandbox.sh --rollback BACKUP_DIR' >&2; exit 64; }
  rollback_dir=$(realpath "$2")
  [[ -f "$rollback_dir/installation.json" && -f "$rollback_dir/python.txt" ]] || { echo 'supply the successful installation backup directory' >&2; exit 1; }
  grep -qx 'populated 0' /sys/fs/cgroup/cube_sandbox/sandbox/cgroup.events || { echo 'clean all VMs before rollback' >&2; exit 1; }
  rollback_python=$(cat "$rollback_dir/python.txt")
  "$rollback_python" - "$rollback_dir" <<'PY'
import json,sys
from pathlib import Path
for p in Path('/proc').iterdir():
    if not p.name.isdigit():continue
    try:name=Path((p/'cmdline').read_bytes().split(b'\0')[0].decode()).name
    except (FileNotFoundError,ProcessLookupError):continue
    if name in {'containerd-shim-cube-rs','cloud-hypervisor','cube-hypervisor'}:raise SystemExit('live VM '+p.name+' prevents rollback')
receipt=json.loads(Path(sys.argv[1],'installation.json').read_text())
if Path(receipt['backup']).resolve()!=Path(sys.argv[1]).resolve():raise SystemExit('backup receipt path mismatch')
PY
  systemctl stop cube-sandbox-cube-api.service cube-sandbox-cubemaster.service cube-sandbox-cubelet.service
  "$rollback_python" - "$rollback_dir" <<'PY'
import json,os,shutil,sys
from pathlib import Path
root=Path(sys.argv[1]);receipt=json.loads((root/'installation.json').read_text())
for key,item in receipt['binaries'].items():
    destination=Path(item['path']);temporary=destination.with_name(destination.name+'.peer-rollback')
    shutil.copy2(root/key,temporary);os.replace(temporary,destination)
PY
  rollback_config=/etc/systemd/system/cube-sandbox-cubelet.service.d/peer-storage.conf
  if [[ -f "$rollback_dir/peer-storage.conf" ]]; then install -m 644 "$rollback_dir/peer-storage.conf" "$rollback_config"; else rm -f "$rollback_config"; fi
  "$rollback_python" -m pip install --no-deps -e "$(cat "$rollback_dir/sdk-source.txt")" > "$rollback_dir/sdk-manual-rollback.log" 2>&1
  systemctl daemon-reload
  systemctl start cube-sandbox-cubelet.service cube-sandbox-cube-egress.service
  systemctl start cube-sandbox-cubemaster.service cube-sandbox-cube-api.service
  echo "Restored installation backup $rollback_dir; run host check and a real VM qualification before new experiments."
  exit 0
fi
if (( $# != 5 )); then
  echo 'usage: sudo bash scripts/install-peer-cubesandbox.sh CUBE_SOURCE CLAWBOX_DIR HOST_PLAN CONTROL_SOCKET PYTHON' >&2
  exit 64
fi
(( EUID == 0 )) || { echo 'run as root' >&2; exit 1; }
source_dir=$(realpath "$1")
project=$(realpath "$2")
plan=$(realpath "$3")
control_socket=$(realpath -m "$4")
python=$(realpath -s "$5")
[[ -x "$source_dir/Cubelet/bin/peer-storage.test" ]] || {
  echo 'build the peer components including native preflight: bash scripts/build-cubesandbox-release.sh --peer-components --source DIR' >&2; exit 1;
}
grep -qx 'populated 0' /sys/fs/cgroup/cube_sandbox/sandbox/cgroup.events || {
  echo 'finish or abort owned experiments before installing; VM cgroup must be empty' >&2; exit 1;
}
"$python" - <<'PY'
from pathlib import Path
for p in Path('/proc').iterdir():
    if not p.name.isdigit(): continue
    try: executable = Path((p/'cmdline').read_bytes().split(b'\0')[0].decode()).name
    except (FileNotFoundError, ProcessLookupError): continue
    if executable in {'containerd-shim-cube-rs', 'cloud-hypervisor', 'cube-hypervisor'}:
        raise SystemExit('live VM process '+p.name+'; clean VMs before installation')
PY
root=/usr/local/services/cubetoolbox
declare -A targets=( [api]="$root/CubeAPI/bin/cube-api" [master]="$root/CubeMaster/bin/cubemaster" [cubelet]="$root/Cubelet/bin/cubelet" )
declare -A builds=( [api]="$source_dir/CubeAPI/target/release/cube-api" [master]="$source_dir/CubeMaster/bin/cubemaster-incremental" [cubelet]="$source_dir/Cubelet/bin/cubelet-incremental" )
for key in api master cubelet; do
  [[ -x ${targets[$key]} && -x ${builds[$key]} ]] || { echo "missing installed/built $key" >&2; exit 1; }
done
domain_roots=$(PYTHONPATH="$project${PYTHONPATH:+:$PYTHONPATH}" "$python" - "$plan" <<'PY'
import sys
from pathlib import Path
from clawbox.experiments.snapshot_host import load
spec=load(Path(sys.argv[1]))
if spec.topology!='peer-local-ssd':raise SystemExit('peer installer requires peer-local-ssd host plan')
print(':'.join(spec.nodes[node].local_mount for node in ('node0','node1')))
PY
)
CLAWBOX_PEER_PREFLIGHT_ROOTS="$domain_roots" "$source_dir/Cubelet/bin/peer-storage.test" -test.run '^TestPeerDomainHostPreflight$' -test.v
install -d -m 700 /data/clawbox-backend-backups
backup=$(mktemp -d /data/clawbox-backend-backups/peer-XXXXXXXX)
chmod 700 "$backup"
config=/etc/systemd/system/cube-sandbox-cubelet.service.d/peer-storage.conf
for key in api master cubelet; do cp -p "${targets[$key]}" "$backup/$key"; done
shim=/data/cubelet/root/component_versions/cube-shim/v0.7.0-clawbox-standalone/bin/containerd-shim-cube-rs
cp -p "$shim" "$backup/shim"
had_config=0
if [[ -f "$config" ]]; then cp -p "$config" "$backup/peer-storage.conf"; had_config=1; fi
for service in cubelet cubemaster cube-api; do
  systemctl cat "cube-sandbox-$service.service" > "$backup/$service.unit"
done
old_sdk=$("$python" -c 'import pathlib,cubesandbox;print(pathlib.Path(cubesandbox.__file__).resolve().parents[1])')
tar -C "$old_sdk" -czf "$backup/sdk.tar.gz" cubesandbox
printf '%s\n' "$old_sdk" > "$backup/sdk-source.txt"
printf '%s\n' "$python" > "$backup/python.txt"
PYTHONPATH="$project${PYTHONPATH:+:$PYTHONPATH}" "$python" - "$plan" "$control_socket" "$backup/proposed.conf" <<'PY'
import json,sys
from pathlib import Path
from clawbox.experiments.snapshot_host import load,check
spec=load(Path(sys.argv[1]))
if spec.topology != 'peer-local-ssd': raise SystemExit('peer installer requires peer-local-ssd host plan')
roots={name:node.local_mount for name,node in spec.nodes.items()}
for name,root in roots.items():
    if not Path(root).is_mount(): raise SystemExit(f'{name} local pool is not mounted; run snapshot-storage apply')
def quote(value): return '"'+value.replace('\\','\\\\').replace('"','\\"')+'"'
text='[Service]\nEnvironment='+quote('CLAWBOX_STORAGE_DOMAINS='+json.dumps(roots,separators=(',',':')))+'\n'
text+='Environment='+quote('CLAWBOX_PEER_CONTROL_SOCKET='+sys.argv[2])+'\n'
Path(sys.argv[3]).write_text(text)
PY
cat "$backup/proposed.conf"
restart() {
  systemctl daemon-reload
  systemctl start cube-sandbox-cubelet.service
  systemctl start cube-sandbox-cube-egress.service
  systemctl start cube-sandbox-cubemaster.service
  systemctl start cube-sandbox-cube-api.service
}
rollback() {
  trap - ERR
  systemctl stop cube-sandbox-cube-api.service cube-sandbox-cubemaster.service cube-sandbox-cubelet.service || true
  for key in api master cubelet; do install -m 755 "$backup/$key" "${targets[$key]}"; done
  if (( had_config )); then install -m 644 "$backup/peer-storage.conf" "$config"; else rm -f "$config"; fi
  "$python" -m pip install --no-deps -e "$old_sdk" > "$backup/sdk-rollback.log" 2>&1 || true
  restart || true
  echo "peer install failed; original components restored from $backup" >&2
}
trap rollback ERR
systemctl stop cube-sandbox-cube-api.service cube-sandbox-cubemaster.service cube-sandbox-cubelet.service
for key in api master cubelet; do
  install -m 755 "${builds[$key]}" "${targets[$key]}.peer-new"
  mv -f "${targets[$key]}.peer-new" "${targets[$key]}"
done
install -d -m 755 "$(dirname "$config")"
install -m 644 "$backup/proposed.conf" "$config"
"$python" -m pip install --no-deps -e "$source_dir/sdk/python" > "$backup/sdk-install.log" 2>&1
restart
PYTHONPATH="$project${PYTHONPATH:+:$PYTHONPATH}" "$python" - "$control_socket" "$backup" "$source_dir" "$plan" <<'PY'
import hashlib,http.client,json,sys,time
from pathlib import Path
from clawbox.cube.peer_replica import native_control
from clawbox.experiments.snapshot_host import load
socket,backup,source,plan=sys.argv[1:]
expected_domains={name:node.local_mount for name,node in load(Path(plan)).nodes.items()}
deadline=time.monotonic()+1800
last_error='health response unavailable'
while True:
    try:
        health=native_control(socket,'/health')
        if health.get('domains')==expected_domains: break
        last_error='health domains do not match the requested host plan'
    except (OSError,http.client.HTTPException,RuntimeError,ValueError) as exc:
        last_error=str(exc)
    if time.monotonic()>deadline: raise SystemExit('Cubelet peer health did not become ready; inspect journal: '+last_error)
    time.sleep(.5)
paths={'api':'/usr/local/services/cubetoolbox/CubeAPI/bin/cube-api',
       'master':'/usr/local/services/cubetoolbox/CubeMaster/bin/cubemaster',
       'cubelet':'/usr/local/services/cubetoolbox/Cubelet/bin/cubelet',
       'shim':'/data/cubelet/root/component_versions/cube-shim/v0.7.0-clawbox-standalone/bin/containerd-shim-cube-rs'}
digest=lambda p:hashlib.file_digest(open(p,'rb'),'sha256').hexdigest()
import cubesandbox
sdk_root=Path(cubesandbox.__file__).parent
sdk_hash=hashlib.sha256()
for sdk_file in sorted(sdk_root.rglob('*.py')):
    sdk_hash.update(str(sdk_file.relative_to(sdk_root)).encode()+b'\0'+sdk_file.read_bytes())
receipt={'source':source,'host_plan':plan,'control_socket':socket,'sdk_import':cubesandbox.__file__,
         'sdk_source_sha256':sdk_hash.hexdigest(),'sdk_backup_archive_sha256':digest(Path(backup)/'sdk.tar.gz'),
         'binaries':{key:{'path':path,'before':digest(Path(backup)/key),'after':digest(path)} for key,path in paths.items()},
         'backup':backup,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
         'status':'installed; real VM validation required'}
receipt['native_health']=health
Path(backup,'installation.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt,indent=2))
PY
trap - ERR
echo "Peer components installed. Backup and receipt: $backup"
