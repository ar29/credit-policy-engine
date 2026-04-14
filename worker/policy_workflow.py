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
from openai import AsyncOpenAI


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
            "co_applicant_score",
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

    # 3.1 FAIL-SAFE GUARD: Prevent errors if the key didn't load
    api_key = settings.openai_api_key
    if not api_key or api_key == "None" or api_key.strip() == "":
        raise ValueError("OPENAI_API_KEY is missing!")

    # 3.2 Initialize the Async Client
    client = AsyncOpenAI(api_key=api_key)

    # 3.3 Execute Request Using the .parse() Method
    # The .parse() method automatically instructs the model to return JSON matching 
    # your Pydantic schema and validates the response before returning it.
    response = await client.beta.chat.completions.parse(
        model="gpt-4o", # Use gpt-4o or gpt-4o-mini for Structured Outputs
        messages=[
            {"role": "system", "content": "You are a strict compliance bot. Extract rules to JSON matching the schema."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.1, # Keep low for deterministic extraction
        response_format=PolicyExtraction # Pass the Pydantic class directly
    )
    
    # 3.4 Access the parsed and validated Pydantic object
    validated_data = response.choices[0].message.parsed
    
    return validated_data.rules

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
    print("Connecting to Temporal server...")
    client = None
    
    # Retry loop: Try 5 times, wait 3 seconds between attempts
    for attempt in range(5):
        try:
            client = await Client.connect("temporal:7233")
            print("Successfully connected to Temporal!")
            break
        except Exception as e:
            print(f"Temporal not ready yet. Retrying in 3 seconds... (Attempt {attempt + 1}/5)")
            await asyncio.sleep(3)
            
    if not client:
        raise RuntimeError("Failed to connect to Temporal server after 5 attempts.")
    print("Starting Temporal Worker...")
    worker = Worker(
        client,
        task_queue="policy-queue",
        workflows=[ReloadPolicyWorkflow],
        activities=[extract_rules_from_llm, broadcast_new_rules, persist_policy_to_db],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())