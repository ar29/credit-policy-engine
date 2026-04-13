import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, mock_open
from app.main import app
from app.models.schemas import RuleSchema
from app.core.config import settings
from app.core.state import policy_state

# Initialize the synchronous TestClient for FastAPI
client = TestClient(app)


# Test Data: A mock compiled policy
MOCK_RULES = [
    RuleSchema(
        rule_id="R-01",
        rule_text="Credit score >= 700",
        field="credit_score",
        operator=">=",
        threshold=700,
        severity="HIGH"
    ),
    RuleSchema(
        rule_id="R-02",
        rule_text="FOIR <= 50",
        field="foir",
        operator="<=",
        threshold=50,
        severity="MEDIUM"
    ),
    RuleSchema(
        rule_id="R-03",
        rule_text="Age > 18",
        field="age",
        operator=">",
        threshold=18,
        severity="HIGH"
    )
]

def test_get_all_rules_success():
    """
    Test that /rules returns the full list when the cache is populated.
    """
    with patch("app.core.state.policy_state.get_rules", return_value=MOCK_RULES):
        response = client.get("/rules")
        assert response.status_code == 200
        assert len(response.json()) == 3
        assert response.json()[0]["rule_id"] == "R-01"

def test_get_specific_rule_success():
    """
    Test that /rules/{rule_id} returns the correct rule detail.
    """
    with patch("app.core.state.policy_state.get_rules", return_value=MOCK_RULES):
        # Case: Rule exists
        response = client.get("/rules/R-02")
        assert response.status_code == 200
        assert response.json()["field"] == "foir"
        assert response.json()["severity"] == "MEDIUM"

def test_get_specific_rule_not_found():
    """
    Test the 404 boundary when a non-existent rule_id is requested.
    """
    with patch("app.core.state.policy_state.get_rules", return_value=MOCK_RULES):
        # Case: Rule ID does not exist in the mock list
        response = client.get("/rules/R-99")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

def test_rules_endpoints_fail_when_cache_empty():
    """
    Test the 503 boundary when the system is fresh and no policy has been loaded.
    """
    with patch("app.core.state.policy_state.get_rules", return_value=[]):
        response = client.get("/rules")
        assert response.status_code == 503
        assert "Rules not loaded" in response.json()["detail"]

@patch("app.main.Client.connect", new_callable=AsyncMock)
@patch("builtins.open", new_callable=mock_open, read_data="Rule R-01: FOIR <= 50")
@pytest.mark.asyncio
async def test_policy_reload_triggers_temporal_workflow(mock_file, mock_temporal_connect):
    """
    Tests that the POST /policy/reload endpoint successfully reads the policy file
    and correctly delegates execution to the Temporal Server without blocking.
    """
    # 1. Setup the mocked Temporal client instance
    mock_temporal_client_instance = AsyncMock()
    mock_temporal_connect.return_value = mock_temporal_client_instance

    # 2. Trigger the API endpoint
    response = client.post("/policy/reload")

    # 3. Assert the HTTP response is immediate and correct
    assert response.status_code == 202
    assert response.json()["status"] == "Reload workflow triggered safely."

    # 4. Assert the Temporal Client connected to the configured host
    # <-- 2. Update this assertion to use the dynamic setting instead of a hardcoded string
    mock_temporal_connect.assert_called_once_with(settings.temporal_server_url) 

    # 5. Assert the workflow was dispatched with the correct parameters
    mock_temporal_client_instance.execute_workflow.assert_called_once_with(
        "ReloadPolicyWorkflow",
        "Rule R-01: FOIR <= 50", # Matches our mocked file data
        id="policy-reload-job",
        task_queue=settings.temporal_task_queue # <-- Make sure this matches config too
    )

def test_evaluate_fails_gracefully_when_no_rules_loaded():
    """
    Tests the deterministic engine boundary when state is empty.
    """
    # Simulate an empty cache
    with patch("app.core.state.policy_state.get_rules", return_value=[]):
        response = client.post("/evaluate", json={
            "application_id": "TEST-001",
            "age": 30,
            "monthly_income": 50000,
            "credit_score": 750,
            "existing_emi_obligations": 0,
            "loan_request": {"amount": 100000, "tenure_months": 12, "purpose": "capital"}
        })
        
        assert response.status_code == 503
        assert "Rules not loaded" in response.json()["detail"]


@pytest.mark.asyncio
async def test_evaluate_includes_policy_version():
    """Verify that the evaluation response includes the audit version ID."""
    
    with patch.object(policy_state, "get_rules", return_value=MOCK_RULES), \
         patch.object(policy_state, "get_current_policy_id", return_value=42), \
         patch("app.main.SessionLocal") as mock_db:
        
        response = client.post("/evaluate", json={
            "application_id": "APP-AUDIT-001",
            "age": 25,
            "monthly_income": 50000,
            "credit_score": 750,
            "loan_request": {"amount": 10000, "tenure_months": 12, "purpose": "test"}
        })
        
        assert response.status_code == 200
        assert response.json()["policy_version"] == 42

@pytest.mark.asyncio
@patch("app.main.Client.connect", new_callable=AsyncMock)
@patch("builtins.open", new_callable=mock_open, read_data="Mock Policy Content")
async def test_policy_reload_orchestration(mock_file, mock_temporal_connect):
    """
    Integration test for the /policy/reload endpoint.
    Verifies that the API correctly reads the configured file and 
    dispatches the workflow to the configured Temporal Task Queue.
    """
    mock_temporal_instance = AsyncMock()
    mock_temporal_connect.return_value = mock_temporal_instance

    response = client.post("/policy/reload")

    # Assertions
    assert response.status_code == 202
    
    # Verify Temporal Connection uses config
    mock_temporal_connect.assert_called_once_with(settings.temporal_server_url)
    
    # Verify Workflow Dispatch uses config
    mock_temporal_instance.execute_workflow.assert_called_once_with(
        "ReloadPolicyWorkflow",
        "Mock Policy Content",
        id="policy-reload-job",
        task_queue=settings.temporal_task_queue
    )

def test_get_rules_uninitialized():
    """Verify 533 error when system has not yet been initialized with a policy."""
    with patch("app.core.state.policy_state.get_rules", return_value=[]):
        response = client.get("/rules")
        assert response.status_code == 503
        assert "Rules not loaded" in response.json()["detail"]


def test_evaluate_endpoint_contract():
    """Verify the /evaluate contract and the automated derivation of fields (FOIR)."""
    # We mock the state so we don't need a real DB/Redis for this test
    with patch("app.main.policy_state.get_rules", return_value=MOCK_RULES), \
         patch("app.main.policy_state.get_current_policy_id", return_value=1), \
         patch("app.main.SessionLocal") as mock_db: # Mock DB session for audit trail
        
        payload = {
            "application_id": "APP-123",
            "age": 25,
            "monthly_income": 50000,
            "credit_score": 750,
            "loan_request": {"amount": 500000, "tenure_months": 24, "purpose": "Expansion"}
        }
        
        response = client.post("/evaluate", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert "decision" in data
        assert "policy_version" in data
        # Check that our derived field FOIR was calculated and returned in explainability
        assert any(r["rule_id"] == "R-03" and r["applicant_value"] == 25 for r in data["rules_evaluated"])

