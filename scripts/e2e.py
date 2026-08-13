from __future__ import annotations
import json, os, subprocess, sys, tempfile, time, uuid
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
def main():
    with tempfile.TemporaryDirectory() as tmp:
        env=os.environ.copy(); env.update(DATABASE_URL=f"sqlite:///{tmp}/e2e.db",
            CONTROLLER_BACKEND="subprocess",WORKSPACE_ROOT=f"{tmp}/workspaces",
            ALLOCATOR_URL="http://127.0.0.1:18081",CONTROLLER_URL="http://127.0.0.1:18082",
            NUMA_CAPACITY="0:64")
        processes=[]
        try:
            for app,port in (("clawbox.allocator.app:app",18081),("clawbox.controller.app:app",18082),
                             ("clawbox.scheduler.app:app",18080)):
                processes.append(subprocess.Popen([sys.executable,"-m","uvicorn",app,"--port",str(port),
                    "--log-level","warning"],cwd=ROOT,env=env))
            wait_ready([18080,18081,18082])
            eid=f"e2e-{uuid.uuid4().hex[:10]}"; headers={"Authorization":"Bearer development-only-token"}
            payload={"tenant_id":"tenant-a","execution_id":eid,"session_id":"session-1","run_id":"run-1",
                "tool_name":"exec","command":f'"{sys.executable}" -c "print(42)"',"argv":[],
                "repo_fingerprint":"clawbox","tool_image":"clawbox-tool-agent:latest",
                "workspace_id":"workspace-a","timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
            response=httpx.post("http://127.0.0.1:18080/v1/executions/run",json=payload,headers=headers,timeout=60)
            response.raise_for_status(); result=response.json(); print_report(result)
            assert result["stdout"].strip()=="42" and result["lease_final_state"]=="RELEASED"
            assert result["kb_generation_after"]==result["kb_generation_before"]+1
        finally:
            for process in processes: process.terminate()
            for process in processes:
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill()
def wait_ready(ports):
    for _ in range(80):
        if all(_ok(port) for port in ports):return
        time.sleep(.1)
    raise RuntimeError("services did not become ready")
def _ok(port):
    try:return httpx.get(f"http://127.0.0.1:{port}/healthz",timeout=.2).status_code==200
    except Exception:return False
def print_report(r):
    print(f"execution_id: {r['execution_id']}\ntenant_id: {r['tenant_id']}")
    p=r["prediction"];print(f"prediction: cpu_p90={p['cpu_p90']} memory_p90={p['memory_p90']} duration_p90={p['duration_p90']}")
    l=r["lease"];print(f"lease: cpu_count={l['cpu_count']} numa={l['numa_hint']} allocated -> {r['lease_final_state']}")
    print(f"tool_pod_uid: {r['tool']['tool_pod_uid']}\ncgroup: {r['observation']['cgroup']}")
    print(f"actual: cpu={r['observation']['cpu']} memory={r['observation']['memory']}")
    print(f"KB generation: {r['kb_generation_before']} -> {r['kb_generation_after']}")
if __name__=="__main__":main()
