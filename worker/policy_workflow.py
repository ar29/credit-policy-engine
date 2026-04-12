import asyncio
from datetime import timedelta
import httpx
import json
import redis
from temporalio import workflow, activity
from temporalio.client import Client
from temporalio.worker import Worker
from sqlalchemy import create_engine, desc
from sqlalchemy.orm import sessionmaker
from app.models.schemas import PolicyAudit, Base
from app.core.config import settings

# Setup Postgres Engine for the Worker
engine = create_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine)

@activity.defn
async def persist_policy_to_db(rules: list) -> int:
    """Saves the compiled ruleset to Postgres and returns the version ID."""
    db = SessionLocal()
    try:
        # Get latest version
        last_policy = db.query(PolicyAudit).order_by(desc(PolicyAudit.version)).first()
        new_version = (last_policy.version + 1) if last_policy else 1
        
        new_policy = PolicyAudit(version=new_version, rules_json=rules)
        db.add(new_policy)
        db.commit()
        db.refresh(new_policy)
        return new_policy.id
    finally:
        db.close()

@activity.defn
async def extract_rules_from_llm(policy_text: str) -> list:
    prompt = f"""
    You are a strict compliance bot. Extract rules to JSON matching schema:
    [{{ "rule_id": "str", "rule_text": "str", "field": "foir|credit_score|loan_maturity_age", "operator": ">|<|>=|<=", "threshold": float, "severity": "HIGH|MEDIUM" }}]
    Policy: {policy_text}
    Output ONLY valid JSON. No markdown wrappers.
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        from app.core.config import settings

        res = await client.post(settings.ollama_base_url, json={
            "model": settings.ollama_model,
            "prompt": prompt,
            "format": "json",
            "stream": False
        })
        # Strict parsing; if Ollama hallucinates, Temporal catches the error and retries.
        return json.loads(res.json()["response"])

@activity.defn
async def broadcast_new_rules(rules: list):
    r = redis.Redis(host='redis', port=6379)
    r.publish("policy_updates", json.dumps(rules))

@workflow.defn
class ReloadPolicyWorkflow:
    @workflow.run
    async def run(self, policy_text: str):
        # 1. Compile via LLM
        rules_json = await workflow.execute_activity(
            extract_rules_from_llm, policy_text, start_to_close_timeout=timedelta(minutes=3)
        )
        
        # 2. Anchor in Postgres (The Audit Trail)
        policy_id = await workflow.execute_activity(
            persist_policy_to_db, rules_json, start_to_close_timeout=timedelta(seconds=30)
        )
        
        # 3. Broadcast to Redis for Hot-Reload
        await workflow.execute_activity(
            broadcast_new_rules, 
            {"policy_id": policy_id, "rules": rules_json}, # Now includes the DB ID
            start_to_close_timeout=timedelta(seconds=10)
        )
        return f"Hot-Reload Complete. Active Policy ID: {policy_id}"

async def main():
    client = await Client.connect("temporal:7233")
    worker = Worker(
        client,
        task_queue="policy-queue",
        workflows=[ReloadPolicyWorkflow],
        activities=[extract_rules_from_llm, broadcast_new_rules],
    )
    print("Starting Temporal Worker...")
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())