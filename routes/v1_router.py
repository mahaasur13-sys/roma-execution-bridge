from fastapi import APIRouter

router = APIRouter()


@router.get("/ping")
async def v1_ping():
    return {"ok": True}
