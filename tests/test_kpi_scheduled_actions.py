from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.strategic_management.models import kpi_scheduler


@tagged("post_install", "-at_install")
class TestStrategicKpiScheduledActions(TransactionCase):
    """Scheduled source runs and review reminders are repeatable and scoped."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.GroupUser = cls.env.ref("base.group_user")
        cls.GroupHead = cls.env.ref(
            "strategic_management.group_strategic_head_office_manager"
        )
        cls.GroupBranch = cls.env.ref(
            "strategic_management.group_strategic_branch_manager"
        )
        cls.GroupEmployee = cls.env.ref(
            "strategic_management.group_strategic_employee"
        )
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        if not cls.warehouse:
            cls.warehouse = cls.env["stock.warehouse"].create(
                {
                    "name": "Scheduled KPI Test Branch",
                    "code": "SKT",
                    "company_id": cls.company.id,
                }
            )
        cls.head_user = cls._make_user(
            "scheduled_kpi_head", cls.GroupHead, warehouse=False
        )
        cls.branch_user = cls._make_user(
            "scheduled_kpi_branch", cls.GroupBranch, warehouse=cls.warehouse
        )
        cls.employee_user = cls._make_user(
            "scheduled_kpi_employee", cls.GroupEmployee, warehouse=cls.warehouse
        )
        cls.unit = cls.env.ref("uom.product_uom_unit")
        template = cls.env["product.template"].create(
            {
                "name": "Scheduled KPI Test Product",
                "uom_id": cls.unit.id,

            }
        )
        cls.product = template.product_variant_id
        cls.Kpi = cls.env["strategic.kpi.definition"]
        cls.Target = cls.env["strategic.kpi.target"]
        cls.Evaluation = cls.env["strategic.kpi.evaluation"]
        cls.Activity = cls.env["mail.activity"].sudo()

    @classmethod
    def _make_user(cls, login, role_group, warehouse=False):
        values = {
            "name": login,
            "login": login,
            "email": "%s@example.com" % login,
            "company_id": cls.company.id,
            "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [cls.GroupUser.id, role_group.id])],
        }
        if warehouse:
            values["strategic_branch_warehouse_id"] = warehouse.id
        return cls.env["res.users"].with_context(no_reset_password=True).create(values)

    def _make_kpi(self, code, calculation_type="pos_sold_quantity"):
        if calculation_type == "manual_value":
            return self.Kpi.create(
                {
                    "name": "%s Scheduled Test KPI" % code,
                    "code": code,
                    "unit": "%",
                    "direction": "higher",
                    "source_type": "manual",
                    "calculation_type": "manual_value",
                    "company_id": self.company.id,
                }
            )
        return self.Kpi.create(
            {
                "name": "%s Scheduled Test KPI" % code,
                "code": code,
                "unit": self.product.uom_id.name,
                "direction": "higher",
                "source_type": "pos",
                "calculation_type": calculation_type,
                "source_product_id": self.product.id,
                "company_id": self.company.id,
            }
        )

    def _make_target(self, kpi, scope="organization", **overrides):
        values = {
            "kpi_id": kpi.id,
            "date_start": "2000-01-01",
            "date_end": "2000-01-31",
            "scope": scope,
            "target_value": 10.0,
            "state": "approved",
        }
        if scope == "branch":
            values["warehouse_id"] = self.warehouse.id
        elif scope == "user":
            values["user_id"] = self.employee_user.id
        values.update(overrides)
        return self.Target.create(values)

    def _activity_search(self, record, summary, user):
        return self.Activity.search(
            [
                ("res_model_id", "=", self.env["ir.model"]._get_id(record._name)),
                ("res_id", "=", record.id),
                ("summary", "=", summary),
                ("user_id", "=", user.id),
            ]
        )

    def test_completed_source_target_creates_one_idempotent_evaluation(self):
        kpi = self._make_kpi("SCHED_VALID")
        target = self._make_target(kpi)

        def source_result(target_record, version):
            return {
                "source_key": "scheduled-valid-%s" % target_record.id,
                "status": "valid",
                "has_value": True,
                "value": 5.0,
                "source_model": "pos.order.line",
                "source_record_ref": "scheduled-test-source",
                "reason": "Controlled scheduler test source.",
            }

        with patch.dict(kpi_scheduler._SOURCE_COLLECTORS, {"pos": source_result}):
            self.Target._cron_process_scheduled_kpis()
            self.Target._cron_process_scheduled_kpis()

        evaluations = self.Evaluation.search(
            [("source_key", "=", "scheduled-valid-%s" % target.id)]
        )
        self.assertEqual(len(evaluations), 1)
        self.assertEqual(evaluations.target_id, target)
        self.assertEqual(evaluations.status, "valid")
        self.assertAlmostEqual(evaluations.actual_value, 5.0)

    def test_source_quality_issue_creates_one_deduplicated_review_activity(self):
        kpi = self._make_kpi("SCHED_QUALITY")
        target = self._make_target(kpi)

        def source_result(target_record, version):
            return {
                "source_key": "scheduled-quality-%s" % target_record.id,
                "status": "data_quality_issue",
                "has_value": False,
                "value": 0.0,
                "source_model": "pos.order.line",
                "source_record_ref": False,
                "reason": "Test source mapping is incomplete.",
            }

        with patch.dict(kpi_scheduler._SOURCE_COLLECTORS, {"pos": source_result}):
            self.Target._cron_process_scheduled_kpis()
            self.Target._cron_process_scheduled_kpis()

        evaluation = self.Evaluation.search(
            [("source_key", "=", "scheduled-quality-%s" % target.id)]
        )
        self.assertEqual(len(evaluation), 1)
        self.assertEqual(evaluation.status, "data_quality_issue")
        activities = self._activity_search(
            target, "Operational KPI exception requires review", self.head_user
        )
        self.assertEqual(len(activities), 1)
        self.assertIn("Test source mapping is incomplete", activities.note)

    def test_submitted_employee_measurement_notifies_branch_manager_once(self):
        kpi = self._make_kpi("SCHED_MANUAL_REVIEW", calculation_type="manual_value")
        target = self._make_target(kpi, scope="user")
        measurement = self.env["strategic.kpi.measurement"].with_user(
            self.employee_user
        ).create(
            {
                "target_id": target.id,
                "actual_value": 80.0,
                "has_actual_value": True,
                "source_reference": "Scheduled review reminder test evidence.",
                "submitter_note": "Submitted for scheduled reminder test.",
            }
        )
        measurement.with_user(self.employee_user).action_submit()

        self.Target._cron_process_scheduled_kpis()
        self.Target._cron_process_scheduled_kpis()

        activities = self._activity_search(
            measurement, "KPI measurement pending review", self.branch_user
        )
        self.assertEqual(len(activities), 1)
        self.assertIn("awaiting review", activities.note)
