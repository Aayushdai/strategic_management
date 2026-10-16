"""Acceptance tests for governed manual KPI submission and review."""

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestStrategicKpiManualMeasurement(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.GroupUser = cls.env.ref("base.group_user")
        cls.GroupHead = cls.env.ref("strategic_management.group_strategic_head_office_manager")
        cls.GroupBranch = cls.env.ref("strategic_management.group_strategic_branch_manager")
        cls.GroupEmployee = cls.env.ref("strategic_management.group_strategic_employee")
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        if not cls.warehouse:
            cls.warehouse = cls.env["stock.warehouse"].create({
                "name": "Manual Measurement Test Branch", "code": "MMT", "company_id": cls.company.id,
            })
        cls.head = cls._make_user("manual_measure_head", cls.GroupHead)
        cls.manager = cls._make_user("manual_measure_manager", cls.GroupBranch, cls.warehouse)
        cls.employee = cls._make_user("manual_measure_employee", cls.GroupEmployee, cls.warehouse)
        cls.Kpi = cls.env["strategic.kpi.definition"]
        cls.Target = cls.env["strategic.kpi.target"]
        cls.Measurement = cls.env["strategic.kpi.measurement"]
        cls.Evaluation = cls.env["strategic.kpi.evaluation"]

    @classmethod
    def _make_user(cls, login, role_group, warehouse=False):
        vals = {
            "name": login,
            "login": login,
            "email": "%s@example.com" % login,
            "company_id": cls.company.id,
            "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [cls.GroupUser.id, role_group.id])],
        }
        if warehouse:
            vals["strategic_branch_warehouse_id"] = warehouse.id
        return cls.env["res.users"].with_context(no_reset_password=True).create(vals)

    def _make_kpi(self, code):
        return self.Kpi.with_user(self.head).create({
            "name": "%s Manual KPI" % code,
            "code": code,
            "unit": "%",
            "direction": "higher",
            "source_type": "manual",
            "description": "Manual submission acceptance test.",
            "company_id": self.company.id,
        })

    def _make_target(self, code, scope="user"):
        kpi = self._make_kpi(code)
        vals = {
            "kpi_id": kpi.id,
            "date_start": "2026-01-01",
            "date_end": "2026-01-31",
            "scope": scope,
            "target_value": 100.0,
        }
        if scope == "user":
            vals["user_id"] = self.employee.id
        elif scope == "branch":
            vals["warehouse_id"] = self.warehouse.id
        else:
            raise AssertionError("This MVP test fixture only creates employee or branch targets.")
        target = self.Target.with_user(self.head).create(vals)
        target.with_user(self.head).action_approve()
        return kpi, target

    def _make_measurement(self, target, submitter=None, actual=80.0):
        submitter = submitter or (self.employee if target.scope == "user" else self.manager)
        return self.Measurement.with_user(submitter).create({
            "target_id": target.id,
            "actual_value": actual,
            "has_actual_value": True,
            "source_reference": "Test evidence: reconciled manual report.",
            "submitter_note": "Test submission.",
        })

    def test_measurement_model_is_registered_and_separate_from_evaluation(self):
        self.assertIn("strategic.kpi.measurement", self.env.registry.models)
        self.assertIn("measurement_id", self.Evaluation._fields)
        self.assertIn("strategic.kpi.measurement.revision", self.env.registry.models)
        self.assertIn("strategic.kpi.measurement.review", self.env.registry.models)

    def test_employee_submission_requires_branch_manager_approval_and_creates_one_evaluation(self):
        _kpi, target = self._make_target("MAN_EMP_APPROVE")
        measurement = self._make_measurement(target, actual=87.0)
        measurement.with_user(self.employee).action_submit()
        self.assertEqual(measurement.state, "submitted")
        self.assertEqual(len(measurement.revision_ids), 1)

        measurement.with_user(self.manager).action_approve()
        self.assertEqual(measurement.state, "approved")
        self.assertEqual(len(measurement.evaluation_ids), 1)
        evaluation = measurement.evaluation_ids
        self.assertEqual(evaluation.actual_value, 87.0)
        self.assertEqual(evaluation.measurement_id, measurement)
        self.assertEqual(evaluation.attainment_status, "almost_met")
        self.assertEqual(evaluation.source_model, "strategic.kpi.measurement")
        with self.assertRaises(ValidationError):
            measurement.with_user(self.manager).action_approve()

    def test_submitter_cannot_approve_own_measurement(self):
        _kpi, target = self._make_target("MAN_NO_SELF_APPROVE")
        measurement = self._make_measurement(target)
        measurement.with_user(self.employee).action_submit()
        with self.assertRaises(AccessError):
            measurement.with_user(self.employee).action_approve()

    def test_measurement_cannot_be_submitted_without_an_explicit_actual_value(self):
        _kpi, target = self._make_target("MAN_MISSING_ACTUAL")
        measurement = self.Measurement.with_user(self.employee).create({
            "target_id": target.id,
            "actual_value": 0.0,
            "has_actual_value": False,
            "source_reference": "Source is available, but no actual was entered.",
        })
        with self.assertRaises(ValidationError):
            measurement.with_user(self.employee).action_submit()

    def test_return_requires_reason_and_resubmission_preserves_each_submitted_version(self):
        _kpi, target = self._make_target("MAN_RETURN_RESUBMIT")
        measurement = self._make_measurement(target, actual=72.0)
        measurement.with_user(self.employee).action_submit()
        with self.assertRaises(ValidationError):
            measurement.with_user(self.manager).action_return_for_correction()

        measurement.with_user(self.manager).write({"review_note": "Attach the revised month-end source."})
        measurement.with_user(self.manager).action_return_for_correction()
        self.assertEqual(measurement.state, "returned")
        self.assertEqual(measurement.review_history_ids[-1].decision, "returned")

        measurement.with_user(self.employee).write({
            "actual_value": 84.0,
            "source_reference": "Corrected month-end source; revision 2.",
            "correction_note": "Reconciled and corrected the original total.",
        })
        measurement.with_user(self.employee).action_submit()
        self.assertEqual(measurement.state, "submitted")
        self.assertEqual(measurement.submission_version, 2)
        self.assertEqual(len(measurement.revision_ids), 2)
        self.assertEqual(measurement.revision_ids.sorted("submission_version")[0].actual_value, 72.0)
        self.assertEqual(measurement.revision_ids.sorted("submission_version")[-1].actual_value, 84.0)

    def test_rejection_requires_reason_and_is_retained_in_review_history(self):
        _kpi, target = self._make_target("MAN_REJECT")
        measurement = self._make_measurement(target)
        measurement.with_user(self.employee).action_submit()
        with self.assertRaises(ValidationError):
            measurement.with_user(self.manager).action_reject()
        measurement.with_user(self.manager).write({"review_note": "Evidence cannot be reconciled."})
        measurement.with_user(self.manager).action_reject()
        self.assertEqual(measurement.state, "rejected")
        self.assertEqual(measurement.review_history_ids[-1].decision, "rejected")
        self.assertEqual(measurement.review_history_ids[-1].reason, "Evidence cannot be reconciled.")
        self.assertFalse(measurement.evaluation_ids)

    def test_branch_measurement_is_reviewed_by_head_office_not_the_submitting_manager(self):
        _kpi, target = self._make_target("MAN_BRANCH_APPROVE", scope="branch")
        measurement = self._make_measurement(target, submitter=self.manager, actual=91.0)
        measurement.with_user(self.manager).action_submit()
        with self.assertRaises(AccessError):
            measurement.with_user(self.manager).action_approve()
        measurement.with_user(self.head).action_approve()
        self.assertEqual(measurement.state, "approved")
        self.assertEqual(measurement.evaluation_ids.scope, "branch")
        self.assertEqual(measurement.evaluation_ids.warehouse_id, self.warehouse)

    def test_head_office_manual_submission_is_deferred_and_rejected(self):
        kpi = self._make_kpi("MAN_HO_DEFERRED")
        target = self.Target.with_user(self.head).create({
            "kpi_id": kpi.id, "date_start": "2026-01-01", "date_end": "2026-01-31",
            "scope": "organization", "target_value": 100.0,
        })
        target.with_user(self.head).action_approve()
        with self.assertRaises(AccessError):
            self.Measurement.with_user(self.head).create({
                "target_id": target.id, "actual_value": 95.0, "has_actual_value": True,
                "source_reference": "Not permitted in MVP.",
            })

    def test_direct_manual_evaluation_by_head_office_cannot_bypass_review(self):
        _kpi, target = self._make_target("MAN_DIRECT_EVAL_BLOCK")
        with self.assertRaises(AccessError):
            self.Evaluation.with_user(self.head).create({
                "kpi_id": target.kpi_id.id, "target_id": target.id,
                "actual_value": 83.0, "has_actual_value": True,
            })

    def test_approved_measurement_correction_creates_replacement_and_supersedes_old_evaluation(self):
        _kpi, target = self._make_target("MAN_CORRECTION_CHAIN")
        original = self._make_measurement(target, actual=81.0)
        original.with_user(self.employee).action_submit()
        original.with_user(self.manager).action_approve()
        old_eval = original.evaluation_ids
        self.assertTrue(old_eval)

        correction = self.Measurement.with_user(self.employee).create({
            "target_id": target.id,
            "actual_value": 89.0,
            "has_actual_value": True,
            "source_reference": "Corrected source report, revision 2.",
            "correction_reason": "Original row was transcribed incorrectly.",
            "corrects_measurement_id": original.id,
        })
        correction.with_user(self.employee).action_submit()
        correction.with_user(self.manager).action_approve()
        self.assertEqual(correction.state, "approved")
        self.assertEqual(correction.corrects_measurement_id, original)
        self.assertEqual(correction.evaluation_ids.actual_value, 89.0)
        self.assertEqual(old_eval.superseded_by_id, correction.evaluation_ids)
        with self.assertRaises(AccessError):
            old_eval.with_user(self.head).write({"superseded_by_id": False})
