from fastapi import APIRouter, Depends, Header, HTTPException, Query
router = APIRouter()
@router.get("/ping")
async def v1_ping():
    return {"ok": True}
