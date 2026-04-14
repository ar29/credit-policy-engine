import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, mock_open, MagicMock
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

# MSME Mock Rules as they would be compiled by the LLM
MSME_MOCK_RULES = [
    RuleSchema(
        rule_id="R-01",
        rule_text="NTC Eligibility: Co-applicant score > 720 if applicant has no history",
        field="credit_eligibility_score",
        operator=">=",
        threshold=720,
        severity="HIGH"
    ),
    RuleSchema(
        rule_id="R-02",
        rule_text="FOIR must be <= 50%",
        field="foir",
        operator="<=",
        threshold=50.0,
        severity="HIGH"
    ),
    RuleSchema(
        rule_id="R-03",
        rule_text="High Value CIBIL: > 750 for loans exceeding 10L",
        field="credit_score",
        operator=">=",
        threshold=750, # The threshold for the specific rule
        severity="HIGH"
    )
]

BASE_MSME_PAYLOAD = {
    "application_id": "APP-123",
    "age": 25,
    "monthly_income": 50000,
    "credit_score": 750,
    "annual_turnover": 1500000,      # Added for MSME Schema
    "business_vintage_months": 24,   # Added for MSME Schema
    "loan_request": {"amount": 500000, "tenure_months": 24, "purpose": "Expansion"}
}

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


@pytest.mark.asyncio
@patch("app.main.Client.connect", new_callable=AsyncMock)
@patch("builtins.open", new_callable=mock_open, read_data="Rule R-01: FOIR <= 50")
async def test_policy_reload_triggers_temporal_workflow(mock_file, mock_temporal_connect):
    """
    Tests that the POST /policy/reload endpoint successfully reads the policy file
    and correctly starts the Temporal workflow using the handle-based start_workflow logic.
    """
    # 1. Setup the mocked Temporal client instance
    mock_temporal_client_instance = AsyncMock()
    mock_temporal_connect.return_value = mock_temporal_client_instance

    # 2. Setup a mock handle to prevent the FastAPI RecursionError
    # We use MagicMock for the handle because its attributes (id, run_id) are simple data
    mock_handle = MagicMock()
    mock_handle.id = "policy-reload-job"
    mock_handle.run_id = "mock-run-12345"
    
    # 3. Ensure start_workflow returns our handle instead of another AsyncMock
    mock_temporal_client_instance.start_workflow.return_value = mock_handle

    # 4. Trigger the API endpoint
    response = client.post("/policy/reload")

    # 5. Assertions
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "Reload workflow triggered safely."
    assert data["workflow_id"] == "policy-reload-job"
    assert data["run_id"] == "mock-run-12345"

    # 6. Verify internal calls
    # Ensure it read the correct file (using the path from settings)
    mock_file.assert_called_once()
    
    # Ensure start_workflow was called with correct deterministic parameters
    mock_temporal_client_instance.start_workflow.assert_called_once_with(
        "ReloadPolicyWorkflow",
        "Rule R-01: FOIR <= 50",
        id="policy-reload-job",
        task_queue="policy-queue"
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
    Verifies that the API correctly reads the file and dispatches 
    the workflow using the new 'start_workflow' non-blocking logic.
    """
    # 1. Setup the mocked Temporal client
    mock_temporal_instance = AsyncMock()
    mock_temporal_connect.return_value = mock_temporal_instance
    
    # 2. Create a concrete handle mock to avoid RecursionError
    # We use a MagicMock for the handle because it's a data object, not a coroutine
    mock_handle = MagicMock()
    mock_handle.id = "policy-reload-job"
    mock_handle.run_id = "mock-run-uuid-123"
    
    # 3. Ensure start_workflow returns this handle
    mock_temporal_instance.start_workflow.return_value = mock_handle

    # 4. Trigger the API
    response = client.post("/policy/reload")

    # 5. Assertions
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "Reload workflow triggered safely."
    assert data["workflow_id"] == "policy-reload-job"
    assert data["run_id"] == "mock-run-uuid-123"
    
    # Verify the internal call was correct
    mock_temporal_instance.start_workflow.assert_called_once_with(
        "ReloadPolicyWorkflow",
        "Mock Policy Content",
        id="policy-reload-job",
        task_queue="policy-queue"
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
        
        response = client.post("/evaluate", json=BASE_MSME_PAYLOAD) # Use full payload
        
        assert response.status_code == 200
        data = response.json()
        assert "decision" in data
        assert "policy_version" in data
        # Check that our derived field FOIR was calculated and returned in explainability
        assert any(r["rule_id"] == "R-03" and r["applicant_value"] == 25 for r in data["rules_evaluated"])


@pytest.mark.asyncio
async def test_evaluate_ntc_co_applicant_logic():
    """Verify that an NTC applicant (score 0) passes using their co-applicant's score."""
    with patch("app.main.policy_state.get_rules", return_value=MSME_MOCK_RULES), \
         patch("app.main.policy_state.get_current_policy_id", return_value=1), \
         patch("app.main.SessionLocal") as mock_db:
        
        # Applicant has 0 score, but co-applicant has 750
        payload = {
            "application_id": "MSME-NTC-001",
            "age": 30,
            "monthly_income": 100000,
            "credit_score": 0, 
            "co_applicant_score": 750,
            "annual_turnover": 2000000,
            "business_vintage_months": 36,
            "loan_request": {"amount": 500000, "tenure_months": 24, "purpose": "Working Capital"}
        }
        
        response = client.post("/evaluate", json=payload)
        assert response.status_code == 200
        data = response.json()
        
        # Find the R-01 result
        ntc_rule = next(r for r in data["rules_evaluated"] if r["rule_id"] == "R-01")
        assert ntc_rule["applicant_value"] == 750
        assert ntc_rule["passed"] is True


@pytest.mark.asyncio
async def test_evaluate_tiered_cibil_failure():
    """Verify high-value loan fails if score is < 750, even if it is > 700."""
    with patch("app.main.policy_state.get_rules", return_value=MSME_MOCK_RULES), \
         patch("app.main.policy_state.get_current_policy_id", return_value=1), \
         patch("app.main.SessionLocal") as mock_db:
        
        payload = {
            "application_id": "MSME-HV-001",
            "age": 40,
            "monthly_income": 200000,
            "credit_score": 720, # Passes standard (700) but fails High-Value (750)
            "annual_turnover": 5000000,
            "business_vintage_months": 48,
            "loan_request": {"amount": 1500000, "tenure_months": 36, "purpose": "Machinery"}
        }
        
        response = client.post("/evaluate", json=payload)
        data = response.json()
        
        # The decision should be REJECTED because credit_score (720) < threshold (750)
        assert data["decision"] == "REJECTED"
        assert any(r["rule_id"] == "R-03" and r["passed"] is False for r in data["rules_evaluated"])

def test_evaluate_fails_without_policy():
    """Ensure 503 is returned if no policy is loaded."""
    with patch("app.main.policy_state.get_rules", return_value=[]), \
         patch("app.main.policy_state.get_current_policy_id", return_value=None):
        
        response = client.post("/evaluate", json={"application_id": "FAIL", "age": 25, "monthly_income": 10, "credit_score": 700, "annual_turnover": 10, "business_vintage_months": 1, "loan_request": {"amount": 10, "tenure_months": 1, "purpose": "test"}})
        assert response.status_code == 503

