from pydantic import BaseModel, Field, model_validator
from typing import List, Literal, Union, Any
from sqlalchemy import Column, String, Integer, DateTime, JSON, ForeignKey, Float
from sqlalchemy.ext.declarative import declarative_base
import datetime

# --- SQLAlchemy Models (Audit Trail) ---
Base = declarative_base()

class PolicyAudit(Base):
    __tablename__ = "policies"
    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(Integer, nullable=False)
    rules_json = Column(JSON, nullable=False) # The [{rule_id, threshold...}] list
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class EvaluationAudit(Base):
    __tablename__ = "evaluations"
    application_id = Column(String, primary_key=True) # application_id
    policy_version_id = Column(Integer, ForeignKey("policies.id"))
    decision = Column(String)
    reason = Column(String, nullable=True)
    final_foir = Column(Float)
    evaluated_at = Column(DateTime, default=datetime.datetime.utcnow)

# --- Pydantic Schemas (API Contracts) ---

class LoanRequest(BaseModel):
    amount: float = Field(..., gt=0)
    tenure_months: int = Field(..., gt=0)
    purpose: str

class ApplicantPayload(BaseModel):
    application_id: str
    age: int = Field(..., gt=18, lt=100)
    monthly_income: float = Field(..., gt=0)
    existing_emi_obligations: float = Field(default=0.0, ge=0)
    credit_score: int
    loan_request: LoanRequest
    
    # Derived Fields
    foir: float = 0.0
    loan_maturity_age: float = 0.0

    @model_validator(mode='after')
    def compute_derived_fields(self):
        self.loan_maturity_age = self.age + (self.loan_request.tenure_months / 12)
        
        # FOIR calculation: 1.5% flat monthly interest assumption for proposed EMI
        r = 0.015 
        p = self.loan_request.amount
        n = self.loan_request.tenure_months
        proposed_emi = (p * r * (1 + r)**n) / ((1 + r)**n - 1)
        
        self.foir = ((self.existing_emi_obligations + proposed_emi) / self.monthly_income) * 100
        return self

class RuleSchema(BaseModel):
    rule_id: str
    rule_text: str
    field: str
    operator: Literal[">", ">=", "<", "<=", "=="]
    threshold: Union[float, int]
    severity: Literal["HIGH", "MEDIUM", "LOW"]

class RuleResult(BaseModel):
    rule_id: str
    rule_text: str
    applicant_value: Any
    threshold: Any
    passed: bool

class DecisionResponse(BaseModel):
    application_id: str
    decision: Literal["APPROVED", "NEEDS_REVIEW", "REJECTED"]
    reason: str
    rules_evaluated: List[RuleResult]
    policy_version: int