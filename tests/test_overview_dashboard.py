"""Head Office Overview: composed sections, comparisons, scoping and live refresh."""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestOverviewDashboard(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        ref = cls.env.ref
        cls.branch_a = cls.env["stock.warehouse"].create({"name": "Overview Branch A", "code": "OVA", "company_id": cls.company.id})
        cls.branch_b = cls.env["stock.warehouse"].create({"name": "Overview Branch B", "code": "OVB", "company_id": cls.company.id})
        cls.head = cls._user("overview_head", [ref("strategic_management.group_strategic_head_office_manager"), ref("stock.group_stock_user")])
        cls.manager_a = cls._user("overview_manager_a", [ref("strategic_management.group_strategic_branch_manager")], cls.branch_a)
        cls.employee = cls._user("overview_employee", [ref("strategic_management.group_strategic_employee")], cls.branch_a)
        cls.today = fields.Date.context_today(cls.env.user)

    @classmethod
    def _user(cls, login, groups, warehouse=False):
        return cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": login.replace("_", " ").title(), "login": login, "company_id": cls.company.id,
            "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [cls.env.ref("base.group_user").id] + [group.id for group in groups])],
            "strategic_branch_warehouse_id": warehouse.id if warehouse else False,
        })

    def _extras(self, user, warehouse=False, days=10):
        return self.env["strategic.performance.dashboard"].with_user(user).get_overview_extras(
            fields.Date.to_string(self.today - timedelta(days=days - 1)), fields.Date.to_string(self.today),
            warehouse.id if warehouse else False,
        )

    def test_previous_period_has_the_same_length_and_ends_before_start(self):
        data = self._extras(self.head, days=10)
        comparison = data["comparison"]
        self.assertEqual(comparison["end"], fields.Date.to_string(self.today - timedelta(days=10)))
        self.assertEqual(comparison["start"], fields.Date.to_string(self.today - timedelta(days=19)))

    def test_sections_follow_role_and_branch_scope(self):
        head = self._extras(self.head)
        self.assertEqual(head["role"], "head_office")
        names = {row["name"] for row in head["branches"]["rows"]}
        self.assertTrue({self.branch_a.display_name, self.branch_b.display_name} <= names)

        manager = self._extras(self.manager_a, self.branch_b)
        self.assertEqual([row["name"] for row in manager["branches"]["rows"]], [self.branch_a.display_name])
        self.assertFalse([item for item in manager["attention"] if item["branch"] == self.branch_b.display_name])
        self.assertIs(manager["purchasing"], False, "Without Purchase rights the section is Not Available, not zero.")

        employee = self._extras(self.employee)
        self.assertEqual(employee["role"], "employee")
        for key in ("comparison", "inventory", "purchasing", "branches", "strategy"):
            self.assertIs(employee[key], False)
        self.assertFalse(employee["attention"] or employee["activities"])

        with self.assertRaises(AccessError):
            self.env["strategic.performance.dashboard"].with_user(self.head).get_overview_extras(False, False, 999999999)

    def test_attention_merges_sections_with_responsible_and_severity(self):
        self.branch_a.strategic_planned_headcount = 8
        data = self._extras(self.head, self.branch_a)
        item = next(item for item in data["attention"] if item["title"] == "Understaffed branch")
        self.assertEqual(item["source"], "People")
        self.assertEqual(item["branch"], self.branch_a.display_name)
        self.assertEqual(item["severity"], "critical")
        self.assertIn("Manager A", item["responsible"])
        self.assertEqual(data["people"]["summary"]["branches_needing_coverage"], 1)

    def test_objective_strategy_counts_are_not_evaluated_without_results(self):
        data = self._extras(self.head, self.branch_b)
        strategy = data["strategy"]
        self.assertEqual(sum(strategy["kpis"].values()), strategy["kpi_total"])
        self.assertEqual(sum(strategy["objectives"].values()), strategy["objective_total"])
        if not strategy["kpi_total"]:
            self.assertIs(strategy["kpi_average_attainment"], False)

    def test_posting_accounting_entries_queues_live_refresh(self):
        journal = self.env["account.journal"].search([("type", "=", "general"), ("company_id", "=", self.company.id)], limit=1)
        account = self.env["account.account"].search([("account_type", "=", "income")], limit=1)
        if not journal or not account:
            self.skipTest("No general journal or income account in this database.")
        move = self.env["account.move"].create({
            "journal_id": journal.id, "line_ids": [
                (0, 0, {"account_id": account.id, "credit": 10.0}),
                (0, 0, {"account_id": account.id, "debit": 10.0}),
            ],
        })
        self.env.cr.precommit.data.pop("strategic_management.data_changed", None)
        move.action_post()
        payload = self.env.cr.precommit.data.get("strategic_management.data_changed")
        self.assertIn("accounting", payload["areas"] if payload else [])

    def test_attention_followups_are_assigned_scoped_and_deduplicated(self):
        Dashboard = self.env["strategic.performance.dashboard"]
        self.branch_a.strategic_planned_headcount = 8
        item = next(item for item in self._extras(self.head, self.branch_a)["attention"] if item["title"] == "Understaffed branch")
        self.assertEqual((item["res_model"], item["res_id"]), ("stock.warehouse", self.branch_a.id))
        self.assertEqual(item["action_status"], "No action recorded")

        result = Dashboard.with_user(self.manager_a).create_attention_followup(item["res_model"], item["res_id"], item["title"], item["evidence"])
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["assigned_to"], [self.manager_a.name], "No owner on a branch: its manager is assigned.")
        again = Dashboard.with_user(self.manager_a).create_attention_followup(item["res_model"], item["res_id"], item["title"])
        self.assertEqual(again["created"], 0, "An open follow-up is not duplicated.")
        refreshed = next(row for row in self._extras(self.head, self.branch_a)["attention"] if row["title"] == "Understaffed branch")
        self.assertTrue(refreshed["action_status"].startswith("Action planned"))

        with self.assertRaises(AccessError):
            Dashboard.with_user(self.manager_a).create_attention_followup("stock.warehouse", self.branch_b.id, "Understaffed branch")
        with self.assertRaises(AccessError):
            Dashboard.with_user(self.employee).create_attention_followup("stock.warehouse", self.branch_a.id, "Understaffed branch")

    def test_objective_followup_goes_to_its_owner(self):
        plan = self.env["strategic.plan"].search([("company_id", "=", self.company.id)], limit=1)
        if not plan or plan.date_end <= self.today:
            self.skipTest("No current strategic plan in this database.")
        objective = self.env["strategic.objective"].with_user(self.head).create({
            "name": "Followup owner test", "plan_id": plan.id, "scope": "organization",
            "owner_id": self.manager_a.id, "deadline": fields.Date.to_string(plan.date_end), "perspective": "financial",
        })
        result = self.env["strategic.performance.dashboard"].with_user(self.head).create_attention_followup(
            "strategic.objective", objective.id, "Objective below target", "Score 40%")
        self.assertEqual(result["assigned_to"], [self.manager_a.name])
        activity = objective.sudo().activity_ids
        self.assertEqual(activity.user_id, self.manager_a)
        self.assertGreater(activity.date_deadline, self.today)
