"""Branch Manager Phase 2: following the objectives that apply to the branch, Head Office instructions and blockers."""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestBranchObjectives(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "BO Branch 1", "code": "BOB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "BO Branch 2", "code": "BOB2", "company_id": cls.company.id})

        def user(login, group, warehouse=False):
            return cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "email": "%s@example.com" % login,
                "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id])],
                "strategic_branch_warehouse_id": warehouse.id if warehouse else False})

        cls.head = user("bo_head", "strategic_management.group_strategic_head_office_manager")
        cls.manager1 = user("bo_manager1", "strategic_management.group_strategic_branch_manager", cls.branch1)
        cls.manager2 = user("bo_manager2", "strategic_management.group_strategic_branch_manager", cls.branch2)
        cls.unassigned = user("bo_unassigned", "strategic_management.group_strategic_branch_manager")
        cls.employee = user("bo_employee", "strategic_management.group_strategic_employee", cls.branch1)
        Objective = cls.env["strategic.objective"]
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BO Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31"})
        cls.company_goal = Objective.with_user(cls.head).create({
            "name": "Reduce waste across the chain", "description": "Every café cuts spoilage.", "plan_id": cls.plan.id,
            "perspective": "internal_process", "scope": "organization", "deadline": "2099-12-31", "owner_id": cls.head.id})
        cls.company_goal.with_user(cls.head).action_activate()
        cls.own = cls._branch_goal(cls.manager1, cls.branch1, "Cut milk spoilage")
        cls.other = cls._branch_goal(cls.manager2, cls.branch2, "Faster service")
        cls.Dashboard = cls.env["strategic.performance.dashboard"]

    @classmethod
    def _branch_goal(cls, manager, branch, name, deadline="2099-06-30"):
        objective = cls.env["strategic.objective"].with_user(manager).create({
            "name": name, "description": "Branch plan.", "plan_id": cls.plan.id, "perspective": "internal_process",
            "scope": "branch", "warehouse_id": branch.id, "parent_objective_id": cls.company_goal.id,
            "deadline": deadline, "owner_id": manager.id})
        objective.with_user(manager).action_submit()
        objective.with_user(cls.head).action_approve()
        return objective

    def _goals(self, user):
        return {goal["objective_id"]: goal for goal in self.env["strategic.goal.setup"].with_user(user).get_goals()}

    # ------------------------------------------------------------------ 1-4: scope
    def test_manager_sees_own_branch_and_company_objectives_only(self):
        goals = self._goals(self.manager1)
        self.assertIn(self.own.id, goals)
        self.assertIn(self.company_goal.id, goals, "company-wide objectives follow the existing record rule")
        self.assertNotIn(self.other.id, goals, "another branch's objective is not exposed")
        own = goals[self.own.id]
        self.assertEqual((own["scope_kind"], own["warehouse_id"]), ("branch", self.branch1.id))
        self.assertEqual(goals[self.company_goal.id]["scope_kind"], "organization")
        summary = self.Dashboard.with_user(self.manager1).get_branch_objective_summary()
        self.assertEqual(summary["branch"]["id"], self.branch1.id)
        company_wide = self.env["strategic.objective"].search_count([("scope", "=", "organization"), ("state", "=", "active"),
                                                                     ("company_id", "=", self.company.id)])
        self.assertEqual((summary["counts"]["own_branch"], summary["counts"]["company_wide"]), (1, company_wide))
        self.assertNotIn(self.other.id, [item["objective_id"] for item in summary["attention"]])
        company = next((i for i in summary["attention"] if i["objective_id"] == self.company_goal.id), None)
        self.assertFalse(company and "No valid measurement yet" in company["reasons"],
                         "company-wide results are Head Office only, so their measurement is not flagged to the branch")

    def test_other_branch_objective_cannot_be_reached_directly(self):
        with self.assertRaises(AccessError):
            self.other.with_user(self.manager1).read(["name"])
        for call in (lambda: self.Dashboard.with_user(self.manager1).get_objective_followup(self.other.id),
                     lambda: self.Dashboard.with_user(self.manager1).report_objective_blocker(self.other.id, "x", "y", "z")):
            with self.assertRaises(AccessError):
                call()

    def test_missing_branch_gets_no_branch_objectives(self):
        goals = self._goals(self.unassigned)
        self.assertFalse([goal for goal in goals.values() if goal["scope_kind"] == "branch"], "no branch objectives at all")
        for call in (lambda: self.Dashboard.with_user(self.unassigned).get_branch_objective_summary(),
                     lambda: self.Dashboard.with_user(self.unassigned).get_objective_followup(self.company_goal.id),
                     lambda: self.Dashboard.with_user(self.unassigned).report_objective_blocker(self.company_goal.id, "x", "y", "z")):
            with self.assertRaisesRegex(AccessError, "Assign this Branch Manager"):
                call()

    # ------------------------------------------------------------------ 5: governance
    def test_manager_cannot_change_head_office_objectives_or_targets(self):
        for values in ({"name": "Mine now"}, {"deadline": "2099-01-01"}, {"description": "changed"}):
            with self.assertRaises((AccessError, ValidationError)):
                self.company_goal.with_user(self.manager1).write(values)
        with self.assertRaises((AccessError, ValidationError)):
            self.own.with_user(self.manager1).write({"deadline": "2099-01-01"})
        with self.assertRaises(ValidationError):
            self.own.with_user(self.manager1).write({"state": "completed"})
        with self.assertRaises(AccessError):
            self.own.with_user(self.manager1).action_complete()
        with self.assertRaises(AccessError):
            self.env["strategic.kpi.target"].with_user(self.manager1).create({"name": "x"})
        self.assertEqual((self.company_goal.name, self.own.state), ("Reduce waste across the chain", "active"))

    # ------------------------------------------------------------------ 6-9: details, evaluation, overdue
    def test_details_use_the_existing_evaluation_semantics(self):
        goal = self._goals(self.manager1)[self.own.id]
        self.assertEqual(goal["parent_objective"], self.company_goal.display_name)
        self.assertEqual(goal["deadline"], "2099-06-30")
        self.assertEqual(goal["status"], "Active · not evaluated yet", "no Key Result: not evaluated, never a percentage")
        self.assertFalse(goal["bar"])
        self.assertEqual(goal["current"], "Not measured")
        self.assertFalse(goal["baseline"], "no baseline is shown when Head Office recorded none")
        self.assertFalse(goal["overdue"])
        item = next(i for i in self.Dashboard.with_user(self.manager1).get_branch_objective_summary()["attention"]
                    if i["objective_id"] == self.own.id)
        self.assertIn("No valid measurement yet", item["reasons"])

    def test_overdue_is_flagged_but_not_failed(self):
        late = self._branch_goal(self.manager1, self.branch1, "Late plan", deadline=fields.Date.today() - timedelta(days=3))
        goal = self._goals(self.manager1)[late.id]
        self.assertTrue(goal["overdue"])
        self.assertNotEqual(goal["category"], "missed", "an overdue objective without a result has not failed")
        self.assertNotEqual(goal["tone"], "bad")
        item = next(i for i in self.Dashboard.with_user(self.manager1).get_branch_objective_summary()["attention"]
                    if i["objective_id"] == late.id)
        self.assertEqual(item["reasons"][0], "Deadline passed")
        # "Due soon" follows Settings (objective due-soon reminders, 3 days by default).
        soon = self._branch_goal(self.manager1, self.branch1, "Soon plan", deadline=fields.Date.today() + timedelta(days=2))
        item = next(i for i in self.Dashboard.with_user(self.manager1).get_branch_objective_summary()["attention"]
                    if i["objective_id"] == soon.id)
        self.assertTrue(item["reasons"][0].startswith("Due "))

    # ------------------------------------------------------------------ 10: unauthorised callers
    def test_employees_and_head_office_cannot_use_branch_manager_methods(self):
        for user in (self.employee, self.head):
            with self.assertRaises(AccessError):
                self.Dashboard.with_user(user).get_branch_objective_summary()
            with self.assertRaises(AccessError):
                self.Dashboard.with_user(user).report_objective_blocker(self.own.id, "x", "y", "z")
        with self.assertRaises(AccessError):
            self.Dashboard.with_user(self.employee).get_objective_followup(self.own.id)

    # ------------------------------------------------------------------ blockers and instructions
    def test_blocker_is_recorded_and_routed_without_changing_the_objective(self):
        with self.assertRaises(UserError):
            self.Dashboard.with_user(self.manager1).report_objective_blocker(self.own.id, "Supplier late", "", "Help")
        result = self.Dashboard.with_user(self.manager1).report_objective_blocker(
            self.own.id, "Supplier late", "Milk deliveries arrive after opening.", "Approve a second supplier.")
        self.assertGreaterEqual(result["activities"], 1, "Head Office receives a To-Do")
        message = self.env["mail.message"].browse(result["message_id"])
        self.assertEqual((message.author_id, message.model, message.res_id), (self.manager1.partner_id, "strategic.objective", self.own.id))
        blocker = result["followup"]["blockers"][0]
        self.assertEqual((blocker["problem"], blocker["status"], blocker["author"]), ("Supplier late", "Waiting for Head Office", self.manager1.name))
        self.assertIn("Approve a second supplier.", blocker["text"])
        self.assertEqual((self.own.state, str(self.own.deadline)), ("active", "2099-06-30"), "a blocker changes nothing else")
        activity = self.env["mail.activity"].search([("res_model", "=", "strategic.objective"), ("res_id", "=", self.own.id),
                                                     ("summary", "=", "Blocker: Supplier late")])
        self.assertTrue(activity, "one To-Do per Head Office user")
        self.assertTrue(all(user.has_group("strategic_management.group_strategic_head_office_manager") for user in activity.user_id))
        for todo in activity:
            todo.with_user(todo.user_id).action_feedback(feedback="Second supplier approved.")
        followup = self.Dashboard.with_user(self.manager1).get_objective_followup(self.own.id)
        self.assertEqual(followup["blockers"][0]["status"], "Closed by Head Office")
        # A company-wide objective can carry a branch's blocker too; a draft cannot.
        self.Dashboard.with_user(self.manager1).report_objective_blocker(self.company_goal.id, "Staff", "Two baristas left.", "Hiring help.")
        draft = self.env["strategic.objective"].with_user(self.manager1).create({
            "name": "Draft idea", "plan_id": self.plan.id, "perspective": "financial", "scope": "branch",
            "warehouse_id": self.branch1.id, "parent_objective_id": self.company_goal.id, "deadline": "2099-06-30", "owner_id": self.manager1.id})
        with self.assertRaises(UserError):
            self.Dashboard.with_user(self.manager1).report_objective_blocker(draft.id, "x", "y", "z")

    def test_head_office_instructions_reach_the_branch(self):
        self.company_goal.with_user(self.head).message_post(body="Focus on dairy first this quarter.", message_type="comment",
                                                            subtype_xmlid="mail.mt_comment")
        self.own.with_user(self.head).message_post(body="Use the new stock count sheet.", message_type="comment",
                                                   subtype_xmlid="mail.mt_note")
        followup = self.Dashboard.with_user(self.manager1).get_objective_followup(self.own.id)
        texts = [row["text"] for row in followup["instructions"]]
        self.assertIn("Use the new stock count sheet.", texts)
        self.assertIn("Focus on dairy first this quarter.", texts, "instructions on the company objective it supports")
        summary = self.Dashboard.with_user(self.manager1).get_branch_objective_summary()
        self.assertIn("Use the new stock count sheet.", [row["text"] for row in summary["instructions"]])
        self.Dashboard.with_user(self.manager1).report_objective_blocker(self.own.id, "Late", "Late milk.", "Help.")
        self.assertFalse([r for r in self.Dashboard.with_user(self.manager1).get_objective_followup(self.own.id)["instructions"]
                          if "Late milk" in r["text"]], "a manager's blocker is not an instruction")

    # ------------------------------------------------------------------ 11: Head Office unchanged
    def test_head_office_still_creates_edits_and_approves(self):
        goal = self.env["strategic.objective"].with_user(self.head).create({
            "name": "New company goal", "plan_id": self.plan.id, "perspective": "financial", "scope": "organization",
            "deadline": "2099-12-31", "owner_id": self.head.id})
        goal.with_user(self.head).write({"description": "Why it matters."})
        goal.with_user(self.head).action_activate()
        self.assertEqual(goal.state, "active")
        proposal = self.env["strategic.objective"].with_user(self.manager1).create({
            "name": "Proposal", "plan_id": self.plan.id, "perspective": "financial", "scope": "branch", "warehouse_id": self.branch1.id,
            "parent_objective_id": goal.id, "deadline": "2099-06-30", "owner_id": self.manager1.id})
        proposal.with_user(self.manager1).action_submit()
        with self.assertRaises(AccessError):
            proposal.with_user(self.manager1).action_approve()
        proposal.with_user(self.head).action_approve()
        self.assertEqual(proposal.state, "active")
        self.assertIn(proposal.id, self._goals(self.head))
