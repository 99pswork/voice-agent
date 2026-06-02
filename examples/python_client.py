"""
Example: Create an agent, upload knowledge base, and place a call.

Run from your CRM / backend.
"""
import requests

BASE = "http://localhost:8000/api/v1"


def main():
    # 1. Create a knowledge base
    kb = requests.post(f"{BASE}/knowledge-base", json={
        "name": "product_catalog",
        "description": "ABC Corp product specs and pricing",
    }).json()
    print(f"Created KB: {kb['id']}")

    # 2. Upload documents to the KB
    with open("./catalog.pdf", "rb") as f:
        docs = requests.post(
            f"{BASE}/knowledge-base/{kb['id']}/upload",
            files={"files": ("catalog.pdf", f, "application/pdf")},
        ).json()
    print(f"Uploaded {len(docs)} documents")

    # 3. Create the voice agent
    agent = requests.post(f"{BASE}/agents", json={
        "name": "Sales - Premium Plan",
        "base_instructions": (
            "You are Riya, a friendly sales agent for ABC Corp. "
            "Greet the customer by name, ask if they have 2 minutes, "
            "and pitch our premium plan based on the knowledge base. "
            "Answer all pricing/feature questions accurately. "
            "If the customer is not interested, thank them politely and end the call. "
            "If they want to subscribe, collect their email and confirm."
        ),
        "voice": "nova",
        "language": "en-IN",
        "knowledge_base_ids": [kb["id"]],
        "initial_message": "Hi, am I speaking with {customer_name}? This is Riya from ABC Corp.",
        "transfer_number": "+918012345678",
        "webhook_url": "https://your-crm.com/api/voice-call-completed",
    }).json()
    print(f"Created agent: {agent['id']}")

    # 4. Trigger an outbound call (the PBX routes the number; no trunk/caller_id needed)
    call = requests.post(f"{BASE}/calls/outbound", json={
        "agent_id": agent["id"],
        "destination": "+919812345678",
        "variables": {"customer_name": "Rahul"},
        "metadata": {"crm_lead_id": "LEAD-9981"},
    }).json()
    print(f"Call placed: {call['call_id']}")

    # 5. Poll until completed
    import time
    while True:
        status = requests.get(f"{BASE}/calls/{call['call_id']}").json()
        print(f"Status: {status['status']}")
        if status["status"] in ("completed", "failed"):
            print(f"Transcript:\n{status.get('transcript')}")
            break
        time.sleep(3)


if __name__ == "__main__":
    main()
