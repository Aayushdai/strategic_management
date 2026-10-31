"""Branch Manager Phase 5: proposing a branch objective from Business Goals and Head Office's existing review."""

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestBranchProposals(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "BP Branch 1", "code": "BPB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "BP Branch 2", "code": "BPB2", "company_id": cls.company.id})

        def user(login, group, warehouse=False):
            return cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "email": "%s@example.com" % login,
                "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id])],
                "strategic_branch_warehouse_id": warehouse.id if warehouse else False})

        cls.head = user("bp_head", "strategic_management.group_strategic_head_office_manager")
        cls.manager1 = user("bp_manager1", "strategic_management.group_strategic_branch_manager", cls.branch1)
        cls.manager2 = user("bp_manager2", "strategic_management.group_strategic_branch_manager", cls.branch2)
        cls.unassigned = user("bp_unassigned", "strategic_management.group_strategic_branch_manager")
        cls.employee = user("bp_employee", "strategic_management.group_strategic_employee", cls.branch1)
        Objective = cls.env["strategic.objective"].with_user(cls.head)
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BP Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31"})
        cls.company_goal = Objective.create({"name": "BP Reduce waste", "plan_id": cls.plan.id, "perspective": "internal_process",
                                             "scope": "organization", "deadline": "2099-12-31", "owner_id": cls.head.id})
        cls.company_goal.action_activate()
        cls.closed_goal = Objective.create({"name": "BP Old goal", "plan_id": cls.plan.id, "perspective": "financial",
                                            "scope": "organization", "deadline": "2099-12-31", "owner_id": cls.head.id})
        # Fixture state only: a company objective that is no longer open for proposals.
        cls.closed_goal.sudo()._write_lifecycle({"state": "closed"})
        cls.other_branch_goal = cls.env["strategic.objective"].with_user(cls.manager2).create({
            "name": "BP Branch 2 plan", "plan_id": cls.plan.id, "perspective": "internal_process", "scope": "branch",
            "parent_objective_id": cls.company_goal.id, "deadline": "2099-06-30"})
        cls.Setup = cls.env["strategic.goal.setup"]
        cls.Dashboard = cls.env["strategic.performance.dashboard"]

    def _values(self, **values):
        vals = {"name": "Cut milk waste at the bar", "business_area": "inventory_waste", "deadline": "2099-06-30",
                "reason": "We pour away about 4 L a week.", "desired_result": "Under 1 L a week", "scope": "branch",
                "parent_objective_id": self.company_goal.id}
        vals.update(values)
        return vals

    def _propose(self, manager=None, **values):
        payload = self.Setup.with_user(manager or self.manager1).create_goal(self._values(**values))
        return self.env["strategic.objective"].browse(payload["objective_id"]), payload

    def _goal(self, user, objective):
        return next(goal for goal in self.Setup.with_user(user).get_goals() if goal["objective_id"] == objective.id)

    # ------------------------------------------------------------------ 1-4: open the form, create, branch and parent
    def test_form_options_offer_the_managers_branch_and_open_company_objectives(self):
        options = self.Setup.with_user(self.manager1).get_form_options()
        self.assertFalse(options["is_head_office"])
        self.assertEqual(options["branches"], [{"id": self.branch1.id, "name": self.branch1.display_name}])
        goals = {goal["id"] for goal in options["company_goals"]}
        self.assertIn(self.company_goal.id, goals)
        self.assertNotIn(self.closed_goal.id, goals)
        self.assertNotIn(self.other_branch_goal.id, goals, "branch objectives are never parents")

    def test_manager_creates_a_draft_proposal_for_their_own_branch(self):
        objective, payload = self._propose(warehouse_id=self.branch2.id)
        self.assertEqual(objective.warehouse_id, self.branch1, "the client's branch is ignored")
        self.assertEqual((objective.scope, objective.state, objective.parent_objective_id), ("branch", "draft", self.company_goal))
        self.assertEqual((objective.plan_id, objective.proposer_id), (self.plan, self.manager1))
        self.assertEqual(objective.description, "We pour away about 4 L a week.")
        self.assertEqual((payload["status"], payload["scope_kind"]), ("Needs setup", "branch"))
        self.assertTrue(payload["can_configure"], "the target and measure are set up through the existing goal setup")
        objective.with_user(self.manager1).action_submit()
        self.assertEqual(objective.state, "pending_approval")
        self.assertEqual(self._goal(self.manager1, objective)["status"], "Pending approval")

    # ------------------------------------------------------------------ 5-8: validation and isolation
    def test_required_values_and_ineligible_parents(self):
        for missing in ({"name": " "}, {"deadline": False}, {"business_area": "nope"}, {"parent_objective_id": False}):
            with self.assertRaises(UserError, msg=missing):
                self._propose(**missing)
        with self.assertRaises(UserError):
            self._propose(deadline="2100-01-01")
        with self.assertRaises(UserError):
            self._propose(parent_objective_id=self.other_branch_goal.id)
        with self.assertRaises(ValidationError):
            self._propose(parent_objective_id=self.closed_goal.id)
        # The same rule holds for a direct create through RPC, bypassing the form.
        with self.assertRaises(ValidationError):
            self.env["strategic.objective"].with_user(self.manager1).create({
                "name": "x", "plan_id": self.plan.id, "perspective": "financial", "scope": "branch",
                "parent_objective_id": self.closed_goal.id, "deadline": "2099-01-01"})

    def test_manager_cannot_propose_for_another_branch_or_the_company(self):
        objective = self.env["strategic.objective"].with_user(self.manager1).create({
            "name": "Sneaky", "plan_id": self.plan.id, "perspective": "financial", "scope": "branch",
            "warehouse_id": self.branch2.id, "parent_objective_id": self.company_goal.id, "deadline": "2099-01-01"})
        self.assertEqual(objective.warehouse_id, self.branch1, "the model also forces the manager's own branch")
        with self.assertRaises(AccessError):
            self._propose(scope="organization")
        with self.assertRaises(AccessError):
            self.other_branch_goal.with_user(self.manager1).action_submit()
        with self.assertRaises(AccessError):
            self.other_branch_goal.with_user(self.manager1).write({"name": "Mine now"})

    def test_manager_without_a_branch_and_employees_cannot_propose(self):
        with self.assertRaises(AccessError):
            self._propose(manager=self.unassigned)
        with self.assertRaises(AccessError):
            self._propose(manager=self.employee)
        with self.assertRaises(AccessError):
            self.env["strategic.objective"].with_user(self.employee).create({
                "name": "x", "plan_id": self.plan.id, "perspective": "financial", "scope": "branch",
                "parent_objective_id": self.company_goal.id, "deadline": "2099-01-01"})

    # ------------------------------------------------------------------ 9-13: Head Office review
    def test_head_office_reviews_approves_and_the_manager_cannot(self):
        objective, _payload = self._propose()
        self.assertEqual(self._goal(self.manager1, objective)["state"], "draft", "not approved before it is even submitted")
        objective.with_user(self.manager1).action_submit()
        row = next(row for row in self.Dashboard.with_user(self.head).get_objective_requests()["pending"] if row["objective_id"] == objective.id)
        self.assertEqual((row["name"], row["branch"], row["parent_objective"]), (objective.name, self.branch1.display_name, self.company_goal.display_name))
        self.assertEqual((row["description"], row["deadline"], row["status"]), ("We pour away about 4 L a week.", "2099-06-30", "Pending Review"))
        self.assertIn("baseline", row)
        for call in (lambda: objective.with_user(self.manager1).action_approve(),
                     lambda: self.Dashboard.with_user(self.manager1).decide_objective_request(objective.id, "approve"),
                     lambda: objective.goal_setup_ids.with_user(self.manager1).approve_goal(),
                     lambda: objective.with_user(self.manager1).write({"state": "active"})):
            with self.assertRaises((AccessError, ValidationError)):
                call()
        self.assertEqual(objective.state, "pending_approval", "nothing above approved it")
        self.Dashboard.with_user(self.head).decide_objective_request(objective.id, "approve")
        self.assertEqual((objective.state, objective.approved_by_id), ("active", self.head))
        goal = self._goal(self.manager1, objective)
        self.assertEqual(goal["review_feedback"]["decision"], "approved")
        self.assertEqual(objective.scoring_status, "not_evaluated", "approval does not make it achieved")
        self.assertFalse(self.env["strategic.branch.task"].search([("objective_id", "=", objective.id)]), "no tasks are created")

    def test_rejection_returns_the_proposal_for_changes_with_the_reason(self):
        objective, _payload = self._propose()
        objective.with_user(self.manager1).action_submit()
        self.Dashboard.with_user(self.head).decide_objective_request(objective.id, "reject", "Give the current weekly figure first.")
        self.assertEqual(objective.state, "draft")
        goal = self._goal(self.manager1, objective)
        self.assertEqual((goal["status"], goal["tone"]), ("Returned for changes", "warn"))
        self.assertEqual(goal["review_feedback"]["reason"], "Give the current weekly figure first.")
        self.assertEqual(goal["review_feedback"]["reviewer"], self.head.name)
        self.assertNotIn(objective.id, [g["objective_id"] for g in self.Setup.with_user(self.manager2).get_goals()], "other branches never see it")
        objective.with_user(self.manager1).write({"description": "We pour away 4.2 L a week (last 4 weeks)."})
        objective.with_user(self.manager1).action_submit()
        self.assertEqual(self._goal(self.manager1, objective)["status"], "Pending approval")
        with self.assertRaises(ValidationError):
            self.Dashboard.with_user(self.head).decide_objective_request(objective.id, "reject", " ")

    # ------------------------------------------------------------------ 14: evaluation unchanged
    def test_company_targets_and_evaluation_stay_protected(self):
        objective, _payload = self._propose()
        with self.assertRaises((AccessError, ValidationError)):
            self.company_goal.with_user(self.manager1).write({"deadline": "2099-01-01"})
        with self.assertRaises(AccessError):
            self.company_goal.with_user(self.manager1).write({"owner_id": self.manager1.id})
        with self.assertRaises(AccessError):
            objective.goal_setup_ids.with_user(self.manager1).prepare_target()
        self.assertFalse(objective.okr_objective_ids, "no Key Result until Head Office approves a target")
        self.assertEqual(self._goal(self.head, objective)["current"], "Not measured")
