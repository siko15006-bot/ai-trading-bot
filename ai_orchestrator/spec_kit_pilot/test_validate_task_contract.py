import copy
import unittest

from validate_task_contract import validate


BASE = {
    "id": "sandbox-test",
    "objective": "Validate one bounded ai_orchestrator sandbox change",
    "classification": "CODE_CHANGE",
    "allowed_paths": ["ai_orchestrator/spec_kit_pilot/"],
    "forbidden_paths": [
        "windows_bots/",
        "oracle_bots/",
        "production",
        "live trading",
        "risk settings",
        "trade strategy thresholds",
    ],
    "acceptance_criteria": ["deterministic self-test passes"],
    "review_policy": "deterministic-first",
    "expected_output": "validation result",
    "metrics": {
        "codex_calls": 0,
        "claude_calls": 0,
        "retries": 0,
        "review_cycles": 0,
        "out_of_scope_diff": False,
        "scope_misunderstanding": False,
    },
}


class ContractValidationTests(unittest.TestCase):
    def test_valid_contract_passes(self):
        self.assertEqual(validate(copy.deepcopy(BASE)), [])

    def test_missing_objective_rejected(self):
        contract = copy.deepcopy(BASE)
        contract.pop("objective")
        self.assertTrue(any("objective" in error for error in validate(contract)))

    def test_path_outside_orchestrator_rejected(self):
        contract = copy.deepcopy(BASE)
        contract["allowed_paths"] = ["windows_bots/"]
        self.assertTrue(any("inside ai_orchestrator" in error for error in validate(contract)))

    def test_missing_production_boundary_rejected(self):
        contract = copy.deepcopy(BASE)
        contract["forbidden_paths"] = ["windows_bots/"]
        self.assertTrue(any("production boundaries" in error for error in validate(contract)))


if __name__ == "__main__":
    unittest.main()
