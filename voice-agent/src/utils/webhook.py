"""Outbound webhook utility with HMAC signing and retries."""
import os
import json
import hmac
import hashlib
import logging
import asyncio
import aiohttp

logger = logging.getLogger(__name__)


async def fire_webhook(url: str, payload: dict, max_retries: int = 3):
    secret = os.getenv("WEBHOOK_SECRET", "")
    body = json.dumps(payload).encode()

    headers = {"Content-Type": "application/json"}
    if secret:
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={signature}"

    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=body, headers=headers, timeout=10) as r:
                    if r.status < 400:
                        logger.info(f"Webhook delivered to {url}")
                        return
                    logger.warning(f"Webhook {url} returned {r.status}")
        except Exception as e:
            logger.warning(f"Webhook attempt {attempt + 1} failed: {e}")

        await asyncio.sleep(2 ** attempt)
    logger.error(f"Webhook to {url} failed after {max_retries} attempts")
