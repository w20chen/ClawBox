from fastapi import Depends, FastAPI, HTTPException

from clawbox.common.auth import require_service_token
from clawbox.common.db import KBMetadataRow, SessionLocal, init_db
from clawbox.common.models import ExecutionIntent, Observation, ResourcePrediction
from .service import Scheduler

app = FastAPI(title="ClawBox Tenant Scheduler")
scheduler = Scheduler()

@app.on_event("startup")
def startup(): init_db()

@app.get("/healthz")
def health():
    try:
        from .kb import TenantKnowledgeBase
        TenantKnowledgeBase()
        return {"status": "ok", "clawtune": "available"}
    except Exception as exc:
        return {"status": "degraded", "clawtune": f"unavailable:{type(exc).__name__}"}

@app.post("/v1/executions/predict", response_model=ResourcePrediction, dependencies=[Depends(require_service_token)])
def predict(intent: ExecutionIntent): return scheduler.predict(intent)

@app.post("/v1/executions/run", dependencies=[Depends(require_service_token)])
def run(intent: ExecutionIntent): return scheduler.run(intent)

@app.post("/v1/executions/{execution_id}/observation", dependencies=[Depends(require_service_token)])
def observe(execution_id: str, observation: Observation):
    generation, updated = scheduler.observation(
        execution_id, observation, scheduler.stored_intent(execution_id)
    )
    return {"generation": generation, "kb_updated": updated}

@app.get("/v1/kb/generation", dependencies=[Depends(require_service_token)])
def generation(tenant_id: str):
    with SessionLocal() as db:
        row = db.get(KBMetadataRow, tenant_id)
        return {"tenant_id": tenant_id, "generation": row.generation if row else 0}

def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
