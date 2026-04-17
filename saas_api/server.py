#!/usr/bin/env python3
"""ROMA SaaS API — HTTP API server with /run endpoint
Runs as: python3 -m saas_api.server
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, HTTPException, Header, Request
from pydantic import BaseModel, Field
from typing import Optional, Literal
import uvicorn, uuid, time
from saas_api.middleware import auth_middleware, log_request
from saas_api.routes.run import router as run_router
from saas_api.routes.jobs import router as jobs_router
from saas_api.routes.health import router as health_router

app = FastAPI(title="ROMA SaaS API", version="1.0.0")

app.middleware("http")(log_request)

app.include_router(health_router)
app.include_router(run_router, prefix="/run", tags=["run"])
app.include_router(jobs_router, prefix="/jobs", tags=["jobs"])

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
