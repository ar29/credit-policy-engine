from app.services.engine import DeterministicRuleEngine
from app.models.schemas import ApplicantPayload, RuleSchema

def test_engine_rejects_high_severity():
    engine = DeterministicRuleEngine()
    
    applicant = ApplicantPayload(**{
        "application_id": "TEST-02",
        "age": 34,
        "monthly_income": 50000,
        "existing_emi_obligations": 0,
        "credit_score": 650,
        "loan_request": {"amount": 300000, "tenure_months": 36, "purpose": "capital"}
    })
    
    rules = [
        RuleSchema(
            rule_id="R-01",
            rule_text="Credit score must be >= 700",
            field="credit_score",
            operator=">=",
            threshold=700,
            severity="HIGH"
        )
    ]
    
    response = engine.evaluate(applicant, rules)
    assert response.decision == "REJECTED"
    assert response.rules_evaluated[0].passed is False

def test_engine_needs_review_medium_severity():
    engine = DeterministicRuleEngine()
    
    # Applicant passes CIBIL (750) but fails FOIR
    applicant = ApplicantPayload(**{
        "application_id": "TEST-03",
        "age": 34,
        "monthly_income": 50000,
        "existing_emi_obligations": 30000, # Massive existing debt
        "credit_score": 750,
        "loan_request": {"amount": 100000, "tenure_months": 12, "purpose": "capital"}
    })
    
    rules = [
        RuleSchema(
            rule_id="R-01",
            rule_text="Credit score must be >= 700",
            field="credit_score",
            operator=">=",
            threshold=700,
            severity="HIGH"
        ),
        RuleSchema(
            rule_id="R-02",
            rule_text="FOIR must be <= 50%",
            field="foir",
            operator="<=",
            threshold=50.0,
            severity="MEDIUM"
        )
    ]
    
    response = engine.evaluate(applicant, rules)
    assert response.decision == "NEEDS_REVIEW"