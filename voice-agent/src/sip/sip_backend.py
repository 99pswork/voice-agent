"""
SIPBackend - the telephony backend (direct SIP client via PJSIP/pjsua2).

main.py stores this on app.state.telephony and the API layer reaches the call
manager via `app.state.telephony.call_manager`. It owns the registered SIP
account lifecycle (connect / disconnect) and exposes the SIPCallManager.
"""
import os
import asyncio
import logging

from sip.pjsip_client import PJSIPClient
from sip.sip_call_manager import SIPCallManager

logger = logging.getLogger(__name__)


class SIPBackend:
    def __init__(self):
        self.client: PJSIPClient | None = None
        self.call_manager: SIPCallManager | None = None

    async def connect(self):
        self.client = PJSIPClient(
            domain=os.getenv("SIP_DOMAIN", "127.0.0.1:5060"),
            username=os.getenv("SIP_USERNAME", ""),
            password=os.getenv("SIP_PASSWORD", ""),
            transport=os.getenv("SIP_TRANSPORT", "udp"),
            local_port=int(os.getenv("SIP_LOCAL_PORT", "5060")),
        )
        # Capture the manager (and the asyncio loop) on THIS thread.
        self.call_manager = SIPCallManager(self.client)

        # start() must run on a single owning thread and that thread must stay
        # registered with pjsua2. Run it synchronously here (fast); the library's
        # own worker thread then pumps SIP events. Only the registration WAIT is
        # offloaded so we don't block the event loop for up to 15s.
        self.client.start()

        registered = await asyncio.to_thread(
            self.client.wait_until_registered, 15
        )
        if registered:
            logger.info("SIP backend registered and ready")
        else:
            logger.warning(
                "SIP backend started but registration not confirmed yet; "
                "calls will retry registration on first dial"
            )

    async def disconnect(self):
        if self.client:
            await asyncio.to_thread(self.client.shutdown)
