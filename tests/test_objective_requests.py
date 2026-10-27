"""Objective Requests on the Overview: branch proposals, Head Office decisions and the review history."""

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestObjectiveRequests(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "OR Branch 1", "code": "ORB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "OR Branch 2", "code": "ORB2", "company_id": cls.company.id})

        def user(login, group, warehouse=False):
            values = {"name": login, "login": login, "email": "%s@example.com" % login,
                      "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                      "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id])]}
            if warehouse:
                values["strategic_branch_warehouse_id"] = warehouse.id
            return cls.env["res.users"].with_context(no_reset_password=True).create(values)

        cls.head = user("or_head", "strategic_management.group_strategic_head_office_manager")
        cls.manager1 = user("or_manager1", "strategic_management.group_strategic_branch_manager", cls.branch1)
        cls.manager2 = user("or_manager2", "strategic_management.group_strategic_branch_manager", cls.branch2)
        cls.employee = user("or_employee", "strategic_management.group_strategic_employee", cls.branch1)
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "OR Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31"})
        cls.company_goal = cls.env["strategic.objective"].with_user(cls.head).create({
            "name": "Grow profitably", "plan_id": cls.plan.id, "perspective": "financial", "scope": "organization",
            "deadline": "2099-12-31", "owner_id": cls.head.id})
        cls.Dashboard = cls.env["strategic.performance.dashboard"]

    def _proposal(self, manager=None, branch=None, name="Cut milk waste"):
        manager, branch = manager or self.manager1, branch or self.branch1
        objective = self.env["strategic.objective"].with_user(manager).create({
            "name": name, "description": "Less spoilage at the bar.", "plan_id": self.plan.id, "perspective": "internal_process",
            "scope": "branch", "warehouse_id": branch.id, "parent_objective_id": self.company_goal.id,
            "deadline": "2099-06-30", "owner_id": manager.id})
        objective.action_submit()
        return objective

    def _requests(self, user, warehouse=False):
        return self.Dashboard.with_user(user).get_objective_requests(warehouse)

    # ------------------------------------------------------------------ queue
    def test_submitted_branch_proposal_is_queued_with_its_details(self):
        objective = self._proposal()
        self.assertEqual(objective.state, "pending_approval")
        self.assertTrue(objective.submitted_at)
        self.assertEqual(objective.proposer_id, self.manager1)

        data = self._requests(self.head)
        self.assertTrue(data["can_decide"])
        row = next(row for row in data["pending"] if row["objective_id"] == objective.id)
        self.assertEqual(row["branch"], self.branch1.display_name)
        self.assertEqual(row["proposer"], self.manager1.name)
        self.assertEqual(row["parent_objective"], self.company_goal.display_name)
        self.assertEqual(row["status"], "Pending Review")
        self.assertEqual(row["description"], "Less spoilage at the bar.")
        self.assertEqual(row["proposed_target"], "Deadline 30 Jun 2099")

    def test_pending_proposal_is_not_an_active_goal(self):
        objective = self._proposal()
        goals = self.env["strategic.goal.setup"].with_user(self.head).get_goals()
        goal = next(goal for goal in goals if goal["objective_id"] == objective.id)
        self.assertEqual(goal["state"], "pending_approval")
        self.assertNotEqual(goal["state"], "active")

    def test_branch_filter_and_branch_manager_scope(self):
        own = self._proposal()
        other = self._proposal(self.manager2, self.branch2, "Faster service")
        head_ids = {row["objective_id"] for row in self._requests(self.head)["pending"]}
        self.assertTrue({own.id, other.id} <= head_ids)
        filtered = {row["objective_id"] for row in self._requests(self.head, self.branch2.id)["pending"]}
        self.assertIn(other.id, filtered)
        self.assertNotIn(own.id, filtered)

        data = self._requests(self.manager1)
        self.assertFalse(data["can_decide"])
        ids = {row["objective_id"] for row in data["pending"]}
        self.assertIn(own.id, ids)
        self.assertNotIn(other.id, ids, "a Branch Manager never sees another branch's requests")
        # Asking for another branch is pinned to the manager's own branch by the existing scope rules.
        ids = {row["objective_id"] for row in self._requests(self.manager1, self.branch2.id)["pending"]}
        self.assertNotIn(other.id, ids)

    def test_employee_has_no_queue(self):
        self._proposal()
        data = self._requests(self.employee)
        self.assertFalse(data["available"])
        self.assertEqual(data["pending"], [])

    # ------------------------------------------------------------------ approve
    def test_approve_activates_once_and_is_logged(self):
        objective = self._proposal()
        data = self.Dashboard.with_user(self.head).decide_objective_request(objective.id, "approve")
        self.assertEqual(objective.state, "active")
        self.assertEqual(objective.approved_by_id, self.head)
        self.assertEqual(objective.parent_objective_id, self.company_goal, "link to the organization objective kept")
        self.assertNotIn(objective.id, [row["objective_id"] for row in data["pending"]])
        row = next(row for row in data["history"] if row["objective_id"] == objective.id)
        self.assertEqual((row["status"], row["reviewer"], row["proposer"]), ("Approved", self.head.name, self.manager1.name))
        self.assertTrue(row["submitted_at"] and row["decided_at"])

        count = self.env["strategic.objective"].search_count([("name", "=", "Cut milk waste")])
        with self.assertRaises(ValidationError):
            self.Dashboard.with_user(self.head).decide_objective_request(objective.id, "approve")
        self.assertEqual(self.env["strategic.objective"].search_count([("name", "=", "Cut milk waste")]), count, "no duplicate objective")
        self.assertEqual(len(objective.review_ids), 1, "one decision recorded")

    def test_branch_manager_cannot_decide(self):
        objective = self._proposal()
        for decision in ("approve", "reject"):
            with self.assertRaises(AccessError):
                self.Dashboard.with_user(self.manager1).decide_objective_request(objective.id, decision, "No")
        with self.assertRaises(AccessError):
            objective.with_user(self.manager1).action_approve()
        self.assertEqual(objective.state, "pending_approval")
        self.assertFalse(objective.review_ids)

    # ------------------------------------------------------------------ reject
    def test_reject_requires_a_reason(self):
        objective = self._proposal()
        for reason in (False, "", "   "):
            with self.assertRaises(ValidationError):
                self.Dashboard.with_user(self.head).decide_objective_request(objective.id, "reject", reason)
        self.assertEqual(objective.state, "pending_approval")
        self.assertFalse(objective.review_ids)

    def test_reject_records_the_decision_and_tells_the_proposer(self):
        objective = self._proposal()
        data = self.Dashboard.with_user(self.head).decide_objective_request(objective.id, "reject", "Target is unrealistic for Q1.")
        self.assertEqual(objective.state, "draft", "the proposer can revise and resubmit")
        self.assertNotIn(objective.id, [row["objective_id"] for row in data["pending"]])
        row = next(row for row in data["history"] if row["objective_id"] == objective.id)
        self.assertEqual((row["status"], row["reason"], row["reviewer"]), ("Rejected", "Target is unrealistic for Q1.", self.head.name))
        message = objective.message_ids.filtered(lambda m: "Target is unrealistic for Q1." in (m.body or ""))
        self.assertTrue(message)
        self.assertIn(self.manager1.partner_id, message.partner_ids, "the proposer is notified with the reason")

        # The proposer sees the decision for their own branch; the other branch manager does not.
        self.assertIn(objective.id, [row["objective_id"] for row in self._requests(self.manager1)["history"]])
        self.assertNotIn(objective.id, [row["objective_id"] for row in self._requests(self.manager2)["history"]])
        with self.assertRaises(ValidationError):
            self.Dashboard.with_user(self.head).decide_objective_request(objective.id, "reject", "Again")

        # Resubmitting creates a new request; the first decision stays in the history.
        objective.with_user(self.manager1).action_submit()
        self.Dashboard.with_user(self.head).decide_objective_request(objective.id, "approve")
        decisions = [row["status"] for row in self._requests(self.head)["history"] if row["objective_id"] == objective.id]
        self.assertEqual(decisions, ["Approved", "Rejected"])

    def test_review_log_is_read_only_for_users(self):
        objective = self._proposal()
        self.Dashboard.with_user(self.head).decide_objective_request(objective.id, "reject", "Not now")
        review = objective.review_ids
        with self.assertRaises(AccessError):
            review.with_user(self.head).write({"reason": "changed"})
        with self.assertRaises(AccessError):
            review.with_user(self.head).unlink()
        with self.assertRaises(AccessError):
            self.env["strategic.objective.review"].with_user(self.manager1).create({
                "objective_name": "x", "company_id": self.company.id, "decision": "approved",
                "reviewer_id": self.manager1.id, "decided_at": "2026-01-01 00:00:00"})

    def test_goal_setup_returns_to_submit_step_after_rejection(self):
        Setup = self.env["strategic.goal.setup"]
        goal = Setup.with_user(self.manager1).create_goal({
            "name": "Branch sales push", "business_area": "sales_finance", "deadline": "2099-06-30", "owner_id": self.manager1.id,
            "scope": "branch", "warehouse_id": self.branch1.id, "parent_objective_id": self.company_goal.id})
        setup = Setup.browse(goal["setup_id"])
        # Simulate a goal whose target was linked and which was submitted through Business Goals.
        setup.sudo().write({"setup_state": "submitted"})
        setup.objective_id.with_user(self.manager1).action_submit()
        self.Dashboard.with_user(self.head).decide_objective_request(setup.objective_id.id, "reject", "Pick a measure first")
        self.assertEqual(setup.objective_id.state, "draft")
        self.assertEqual(setup.setup_state, "measured")
