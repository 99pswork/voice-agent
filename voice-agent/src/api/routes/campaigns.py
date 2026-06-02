"""Campaign tracking routes."""
from fastapi import APIRouter, HTTPException, Depends
from utils.db import get_db

router = APIRouter()


@router.get("/{job_id}")
async def get_campaign(job_id: str, db=Depends(get_db)):
    doc = await db.campaigns.find_one({"id": job_id})
    if not doc:
        raise HTTPException(404, "Campaign not found")
    return doc


@router.get("")
async def list_campaigns(db=Depends(get_db), limit: int = 50):
    cursor = db.campaigns.find({}).sort("started_at", -1).limit(limit)
    return [doc async for doc in cursor]
