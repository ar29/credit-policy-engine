import pytest
from app.models.schemas import ApplicantPayload

def test_derived_fields_computation():
    payload = {
        "application_id": "TEST-01",
        "age": 30,
        "monthly_income": 100000.0,
        "existing_emi_obligations": 10000.0,
        "credit_score": 750,
        "loan_request": {
            "amount": 500000.0,
            "tenure_months": 24,
            "purpose": "business_expansion"
        }
    }
    
    applicant = ApplicantPayload(**payload)
    
    # Assert Maturity Age: 30 + (24/12) = 32
    assert applicant.loan_maturity_age == 32.0
    
    # Assert FOIR logic is successfully calculating a percentage > 0
    assert applicant.foir > 0
    assert applicant.foir > 10.0 # 10k base EMI on 100k income is 10%. With new EMI, must be higher.