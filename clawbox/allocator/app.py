from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from clawbox.common.auth import require_service_token
from clawbox.common.db import init_db
from clawbox.common.models import ReleaseLease, RenewLease, ResourceLease, ResourceRequest

from .service import Allocator

allocator = Allocator()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="ClawBox Global Allocator", lifespan=lifespan)


@app.get("/healthz")
def health():
    return {"status": "ok", "epoch": allocator.epoch}


@app.get("/v1/capacity", dependencies=[Depends(require_service_token)])
def capacity():
    return allocator.capacity()


@app.post("/v1/leases", response_model=ResourceLease, dependencies=[Depends(require_service_token)])
def create(request: ResourceRequest):
    return allocator.create(request)


@app.post("/v1/leases/{lease_id}/renew", response_model=ResourceLease, dependencies=[Depends(require_service_token)])
def renew(lease_id: str, request: RenewLease):
    return allocator.renew(lease_id, request)


@app.delete("/v1/leases/{lease_id}", response_model=ResourceLease, dependencies=[Depends(require_service_token)])
def release(lease_id: str, request: ReleaseLease):
    return allocator.release(lease_id, request)


def main():
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8081)

