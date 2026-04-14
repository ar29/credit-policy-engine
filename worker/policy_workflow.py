import asyncio
from datetime import timedelta
from temporalio import workflow, activity
from temporalio.client import Client
from temporalio.worker import Worker, UnsandboxedWorkflowRunner
from sqlalchemy import create_engine, desc
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.models.schemas import PolicyAudit, Base
from app.core.database import SessionLocal # NEW IMPORT


@activity.defn
async def persist_policy_to_db(rules: list) -> int:
    """Saves the compiled ruleset to Postgres and returns the version ID."""
    # Setup Postgres Engine for the Worker
    
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
    import httpx  # Import INSIDE the activity
    import json
    from app.core.config import settings

    import os
    from typing import List, Literal, Union
    from pydantic import BaseModel, Field

    # ---------------------------------------------------------
    # 1. Define the Pydantic Schema for the LLM Output
    # ---------------------------------------------------------

    class RuleSchema(BaseModel):
        rule_id: str
        rule_text: str
        # Enforcing ALL possible fields from your ApplicantPayload
        field: Literal[
            "age", 
            "monthly_income", 
            "existing_emi_obligations", 
            "credit_score", 
            "co_applicant_score", 
            "industry_type", 
            "effective_cibil_threshold", 
            "credit_eligibility_score", 
            "is_industry_allowed", 
            "foir", 
            "loan_maturity_age",
            "amount",     # Derived mapping for loan_request.amount
            "tenure_months"    # Derived mapping for loan_request.tenure_months
        ]
        operator: Literal[">", ">=", "<", "<=", "=="]
        threshold: float # Float covers both ints and decimals in JSON
        severity: Literal["HIGH", "MEDIUM"]

    class PolicyExtraction(BaseModel):
        # Wrapping the array in an object guarantees better JSON compliance 
        # across different open-source model providers
        rules: List[RuleSchema]

    # ---------------------------------------------------------
    # 2. Define the Prompts and Policy Text
    # ---------------------------------------------------------

    prompt = f"""
    You are a strict compliance bot. Extract rules to JSON.
    Output ONLY valid JSON matching the schema. No markdown wrappers.

    Special Mapping Instruction: If the policy mentions credit score requirements for 'New-to-Credit' or 'No History' 
    applicants involving a co-applicant, map the 'field' to credit_eligibility_score. 
    Treat the required co-applicant score as the 'threshold'.

    Constraint: When the policy defines a base credit score (e.g., 700) 
    but provides an exception for "New-to-Credit" (NTC) applicants via a co-applicant, 
    DO NOT generate a separate rule for credit_score. 
    Instead, generate a SINGLE rule using credit_eligibility_score. 
    This ensures the exception logic is handled within the data model rather than creating conflicting rules.
    
    Policy: {policy_text}
    """

    # ---------------------------------------------------------
    # 3. Execute the API Call
    # ---------------------------------------------------------
    async with httpx.AsyncClient(timeout=120.0) as client:
        from app.core.config import settings

        res = await client.post(settings.ollama_base_url, json={
            "model": settings.ollama_model,
            "prompt": prompt,
            "format": PolicyExtraction.model_json_schema(),
            "stream": False,
            "options": {
                    "temperature": 0.1 # Low temperature for extraction tasks
                }
        })
        # ---------------------------------------------------------
        # 4. Parse and Validate the Output
        # ---------------------------------------------------------
        res_json = json.loads(res.json()["response"])

        return res_json["rules"]

@activity.defn
async def broadcast_new_rules(rules: list, version_id: int):
    import json
    import redis
    from app.core.config import settings
    
    # Using the centralized config for host/port
    r = redis.Redis(
        host=settings.redis_host, 
        port=settings.redis_port, 
        db=settings.redis_db
    )
    
    # We create a structured envelope so the API knows exactly what it's receiving
    payload = {
        "version": version_id,
        "rules": rules
    }
    
    # Publish the stringified JSON
    r.publish("policy_updates", json.dumps(payload))

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
            args=[rules_json, policy_id],
            start_to_close_timeout=timedelta(seconds=10)
        )
        return f"Hot-Reload Complete. Active Policy ID: {policy_id}"

async def main():
    client = await Client.connect("temporal:7233")
    worker = Worker(
        client,
        task_queue="policy-queue",
        workflows=[ReloadPolicyWorkflow],
        activities=[extract_rules_from_llm, broadcast_new_rules, persist_policy_to_db],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    print("Starting Temporal Worker...")
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())