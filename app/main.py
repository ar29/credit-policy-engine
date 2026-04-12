from fastapi import FastAPI, HTTPException
import asyncio
from temporalio.client import Client
from app.core.state import policy_state
from app.models.schemas import ApplicantPayload, DecisionResponse
from app.services.engine import DeterministicRuleEngine

app = FastAPI(title="Prayaan Credit Engine")
engine = DeterministicRuleEngine()

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(policy_state.listen_for_invalidations())

@app.post("/evaluate", response_model=DecisionResponse)
async def evaluate(payload: ApplicantPayload):
    rules = policy_state.get_rules()
    if not rules:
        raise HTTPException(status_code=503, detail="Rules not loaded. Call /policy/reload first.")
    return engine.evaluate(payload, rules)

@app.post("/policy/reload", status_code=202)
async def trigger_reload():
    with open("data/policy.txt", "r") as f:
        text = f.read()
    
    try:
        client = await Client.connect("temporal:7233")
        await client.execute_workflow(
            "ReloadPolicyWorkflow",
            text,
            id="policy-reload-job",
            task_queue="policy-queue"
        )
        return {"status": "Reload workflow triggered safely."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))