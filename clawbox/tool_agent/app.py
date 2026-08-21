from fastapi import FastAPI
from clawbox.common.models import ExecuteRequest
from .service import ToolAgent
app = FastAPI(title="ClawBox Tool Agent"); agent = ToolAgent()
@app.get("/healthz")
def health(): return {"status": "ok", "tool_pod_uid": agent.tool_pod_uid}
@app.post("/v1/execute")
def execute(request: ExecuteRequest): return agent.execute(request)
def main():
    import uvicorn; uvicorn.run(app, host="0.0.0.0", port=8090)
