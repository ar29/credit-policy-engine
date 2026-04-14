import logging
import operator
from typing import List
from app.models.schemas import ApplicantPayload, RuleSchema, DecisionResponse, RuleResult

OPERATORS = {
    ">": operator.gt, ">=": operator.ge,
    "<": operator.lt, "<=": operator.le,
    "==": operator.eq
}

class DeterministicRuleEngine:
    def evaluate(self, applicant: ApplicantPayload, rules: List[RuleSchema], policy_id: int) -> DecisionResponse:
        results = []
        failed_high = 0
        failed_medium = 0

        for rule in rules:
            try:
                applicant_value = getattr(applicant, rule.field)
                op_func = OPERATORS[rule.operator]
                # --- DYNAMIC THRESHOLD RESOLUTION ---
                if rule.field == "credit_score" and applicant.loan_request.amount > 250000:
                    # Apply the 2.5L condition
                    threshold_val = applicant.effective_cibil_threshold
                elif rule.field == "credit_eligibility_score":
                    # Use the NTC logic bridge
                    threshold_val = rule.threshold
                else:
                    # Standard static threshold
                    threshold_val = type(applicant_value)(rule.threshold)
                
                # Skip rule if it's conditional and the condition isn't met (e.g., small loan)
                if rule.field == "credit_score" and applicant.loan_request.amount <= 250000:
                    continue

                # Safety Check: If a rule has a threshold <= 100 but is targeting a currency field, 
                # it's likely a mapping error.
                if rule.field == "existing_emi_obligations" and rule.threshold <= 100:
                    logging.warning(f"Detected potential mapping error for {rule.rule_id}. Redirecting to foir.")
                    rule.field = "foir" # Auto-correct to the derived field

                passed = op_func(applicant_value, threshold_val)
            except (AttributeError, ValueError, KeyError):
                passed = False
                applicant_value = "EVALUATION_ERROR"

            results.append(RuleResult(
                rule_id=rule.rule_id,
                rule_text=rule.rule_text,
                applicant_value=applicant_value,
                threshold=threshold_val,
                passed=passed
            ))

            if not passed:
                if rule.severity == "HIGH": failed_high += 1
                if rule.severity == "MEDIUM": failed_medium += 1

        if failed_high > 0:
            decision, reason = "REJECTED", f"Failed {failed_high} HIGH severity rules."
        elif failed_medium > 0:
            decision, reason = "NEEDS_REVIEW", f"Failed {failed_medium} MEDIUM rules."
        else:
            decision, reason = "APPROVED", "All rules passed."

        return DecisionResponse(
            application_id=applicant.application_id,
            decision=decision,
            reason=reason,
            rules_evaluated=results,
            policy_version=policy_id
        )