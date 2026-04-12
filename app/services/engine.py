import operator
from typing import List
from app.models.schemas import ApplicantPayload, RuleSchema, DecisionResponse, RuleResult

OPERATORS = {
    ">": operator.gt, ">=": operator.ge,
    "<": operator.lt, "<=": operator.le,
    "==": operator.eq
}

class DeterministicRuleEngine:
    def evaluate(self, applicant: ApplicantPayload, rules: List[RuleSchema]) -> DecisionResponse:
        results = []
        failed_high = 0
        failed_medium = 0

        for rule in rules:
            try:
                applicant_value = getattr(applicant, rule.field)
                op_func = OPERATORS[rule.operator]
                threshold_val = type(applicant_value)(rule.threshold)
                passed = op_func(applicant_value, threshold_val)
            except (AttributeError, ValueError, KeyError):
                passed = False
                applicant_value = "EVALUATION_ERROR"

            results.append(RuleResult(
                rule_id=rule.rule_id,
                rule_text=rule.rule_text,
                applicant_value=applicant_value,
                threshold=rule.threshold,
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
            rules_evaluated=results
        )