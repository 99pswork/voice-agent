"""Bulk dialer - throttled outbound campaign engine."""
import asyncio
import logging
from uuid import uuid4
from datetime import datetime
from typing import List, Dict

logger = logging.getLogger(__name__)


class BulkDialer:
    def __init__(self, call_manager, rate_per_second: int = 5):
        self.call_manager = call_manager
        self.rate = rate_per_second

    async def start(self, agent_id: str, targets: List[Dict], db) -> str:
        job_id = f"job_{uuid4().hex[:12]}"
        await db.campaigns.insert_one({
            "id": job_id,
            "agent_id": agent_id,
            "total": len(targets),
            "completed": 0,
            "failed": 0,
            "status": "running",
            "started_at": datetime.utcnow(),
        })

        agent = await db.agents.find_one({"id": agent_id})
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")

        asyncio.create_task(self._run(job_id, agent, targets, db))
        return job_id

    async def _run(self, job_id: str, agent: Dict, targets: List[Dict], db):
        sem = asyncio.Semaphore(self.rate)
        delay = 1.0 / max(self.rate, 1)

        async def dial_one(target):
            async with sem:
                try:
                    call_id = f"call_{uuid4().hex[:12]}"
                    await self.call_manager.originate_call(
                        call_id=call_id,
                        destination=target["destination"],
                        caller_id=target.get("caller_id"),
                        trunk=target.get("trunk"),
                        agent_config=agent,
                        variables=target.get("variables", {}),
                    )
                    await db.campaigns.update_one(
                        {"id": job_id}, {"$inc": {"completed": 1}}
                    )
                except Exception as e:
                    logger.warning(f"Bulk dial failed for {target['destination']}: {e}")
                    await db.campaigns.update_one(
                        {"id": job_id}, {"$inc": {"failed": 1}}
                    )
                await asyncio.sleep(delay)

        await asyncio.gather(*(dial_one(t) for t in targets), return_exceptions=True)

        await db.campaigns.update_one(
            {"id": job_id},
            {"$set": {"status": "completed", "completed_at": datetime.utcnow()}},
        )
