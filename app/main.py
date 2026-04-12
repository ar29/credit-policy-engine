from fastapi import FastAPI, HTTPException, Path
import asyncio
from typing import List
from temporalio.client import Client
from app.core.state import policy_state
from app.core.config import settings
from app.models.schemas import ApplicantPayload, DecisionResponse, RuleSchema
from app.services.engine import DeterministicRuleEngine
from sqlalchemy.orm import Session
from app.models.schemas import EvaluationAudit, Base, PolicyAudit
from worker.policy_workflow import SessionLocal

app = FastAPI(title="Prayaan Credit Engine")
engine = DeterministicRuleEngine()

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(policy_state.listen_for_invalidations())

@app.post("/evaluate", response_model=DecisionResponse)
async def evaluate(payload: ApplicantPayload):
    # 1. Get current rules and the active policy_id from our thread-safe state
    active_rules = policy_state.get_rules()
    active_id = policy_state.get_current_policy_id() 

    if not active_rules or active_id:
        raise HTTPException(status_code=533, detail="Rules not loaded. Policy not initialized. Call /policy/reload first.")

    # 2. Deterministic Evaluation (PII never leaves the pod)
    result = engine.evaluate(payload, active_rules)

    # 3. Log Audit Trail to Postgres
    # In production, this would be a background task to keep API latency low
    db = SessionLocal() 
    audit_entry = EvaluationAudit(
        application_id=payload.application_id,
        policy_version_id=active_id,
        decision=result.decision,
        reason=result.reason
    )
    db.merge(audit_entry) # Use merge to handle retries/re-evaluations
    db.commit()
    db.close()

    # Add version to response for transparency
    response = result.model_dump()
    response["policy_version"] = active_id
    return response

@app.get("/rules", response_model=List[RuleSchema])
async def get_all_rules():
    """
    Returns the complete list of parsed rules currently active in the engine.
    Fetches directly from the thread-safe O(1) memory cache.
    """
    rules = policy_state.get_rules()
    if not rules:
        raise HTTPException(status_code=503, detail="Rules not loaded. Call /policy/reload first.")
    return rules

@app.get("/rules/{rule_id}", response_model=RuleSchema)
async def get_rule_by_id(rule_id: str = Path(..., description="The ID of the rule to fetch (e.g., R-01)")):
    """
    Returns the details of a specific rule.
    """
    rules = policy_state.get_rules()
    if not rules:
        raise HTTPException(status_code=503, detail="Rules not loaded. Call /policy/reload first.")
    
    # Simple linear search. If policy grows to 10k+ rules, we would index this in a dict.
    for rule in rules:
        if rule.rule_id == rule_id:
            return rule
            
    raise HTTPException(status_code=404, detail=f"Rule with ID '{rule_id}' not found in active policy.")

@app.post("/policy/reload", status_code=202)
async def trigger_reload():
    from app.core.config import settings

    with open(settings.policy_file_path, "r") as f:
        text = f.read()

    try:
        client = await Client.connect(settings.temporal_server_url)
        await client.execute_workflow(
            "ReloadPolicyWorkflow",
            text,
            id="policy-reload-job",
            task_queue="policy-queue"
        )
        return {"status": "Reload workflow triggered safely."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))