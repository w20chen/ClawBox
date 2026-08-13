from fastapi import FastAPI
from .topology import status, topology
app=FastAPI(title="ClawBox Host Node Agent")
@app.get("/healthz")
def health():return {"status":"ok"}
@app.get("/v1/topology")
def get_topology():return topology()
@app.get("/v1/status")
def get_status():return status()
def main():
    import uvicorn;uvicorn.run(app,host="0.0.0.0",port=8083)
