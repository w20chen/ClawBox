from fastapi import Depends, FastAPI
from clawbox.common.auth import require_service_token
from clawbox.common.db import init_db
from clawbox.common.models import ExecuteRequest, ToolInstance, ToolSpec
from .service import Controller
app = FastAPI(title="ClawBox Tool Controller"); controller = Controller()
@app.on_event("startup")
def startup(): init_db()
@app.get("/healthz")
def health(): return {"status": "ok"}
@app.post("/v1/tool-pods/acquire", response_model=ToolInstance, dependencies=[Depends(require_service_token)])
def acquire(spec: ToolSpec): return controller.acquire(spec)
@app.post("/v1/tool-pods/{uid}/execute", dependencies=[Depends(require_service_token)])
def execute(uid: str, request: ExecuteRequest): return controller.execute(uid, request)
@app.post("/v1/tool-pods/{uid}/release", response_model=ToolInstance, dependencies=[Depends(require_service_token)])
def release(uid: str): return controller.release(uid)
@app.delete("/v1/tool-pods/{uid}", response_model=ToolInstance, dependencies=[Depends(require_service_token)])
def destroy(uid: str): return controller.release(uid, True)
def main():
    import uvicorn; uvicorn.run(app, host="0.0.0.0", port=8082)
