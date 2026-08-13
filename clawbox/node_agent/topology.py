from __future__ import annotations
import os, platform, re
from pathlib import Path
from clawbox.common.config import settings

def _text(path: str) -> str | None:
    try: return Path(path).read_text(encoding="utf-8").strip()
    except OSError: return None
def _cpus(value: str | None) -> list[int]:
    if not value: return list(range(os.cpu_count() or 0))
    result=[]
    for part in value.split(","):
        if "-" in part:
            a,b=map(int,part.split("-",1)); result.extend(range(a,b+1))
        else: result.append(int(part))
    return sorted(set(result))
def topology() -> dict:
    online=_cpus(_text("/sys/devices/system/cpu/online")); nodes=[]
    root=Path("/sys/devices/system/node")
    if root.exists():
        for path in sorted(root.glob("node*")):
            match=re.fullmatch(r"node(\d+)",path.name)
            if match: nodes.append({"numa_id":int(match.group(1)),"cpus":_cpus(_text(str(path/"cpulist"))),
                "memory_available_bytes":_node_mem(path),"llc_id":None,"cluster_id":None,
                "memory_bandwidth":None,"pmu_metrics":{}})
    if not nodes: nodes=[{"numa_id":0,"cpus":online,"memory_available_bytes":None,
        "llc_id":None,"cluster_id":None,"memory_bandwidth":None,"pmu_metrics":{}}]
    reserved=min(max(0,int(len(online)*settings.reserved_cpu_fraction+0.999)),max(0,len(online)-1))
    return {"platform":platform.platform(),"online_cpus":online,"logical_cpu_count":len(online),
        "reserved_cpu_count":reserved,"allocatable_cpu_count":len(online)-reserved,"numa_nodes":nodes}
def _node_mem(path: Path) -> int | None:
    text=_text(str(path/"meminfo"))
    if not text:return None
    for line in text.splitlines():
        if "MemFree:" in line:
            try:return int(line.split()[-2])*1024
            except (ValueError,IndexError):return None
    return None
def status() -> dict:
    load=os.getloadavg() if hasattr(os,"getloadavg") else None
    return {"load":load,"psi_cpu":_text("/proc/pressure/cpu"),
        "psi_memory":_text("/proc/pressure/memory"),"topology":topology()}
