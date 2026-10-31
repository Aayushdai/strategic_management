"""Branch Manager Phase 3: branch tasks assigned to employees, their review workflow and branch isolation."""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestBranchTasks(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "BT Branch 1", "code": "BTB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "BT Branch 2", "code": "BTB2", "company_id": cls.company.id})

        def user(login, group, warehouse=False):
            return cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "email": "%s@example.com" % login,
                "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id])],
                "strategic_branch_warehouse_id": warehouse.id if warehouse else False})

        cls.head = user("bt_head", "strategic_management.group_strategic_head_office_manager")
        cls.manager1 = user("bt_manager1", "strategic_management.group_strategic_branch_manager", cls.branch1)
        cls.manager2 = user("bt_manager2", "strategic_management.group_strategic_branch_manager", cls.branch2)
        cls.unassigned = user("bt_unassigned", "strategic_management.group_strategic_branch_manager")
        cls.barista = user("bt_barista", "strategic_management.group_strategic_employee", cls.branch1)
        cls.cook = user("bt_cook", "strategic_management.group_strategic_employee", cls.branch1)
        cls.other_barista = user("bt_other_barista", "strategic_management.group_strategic_employee", cls.branch2)
        cls.Task = cls.env["strategic.branch.task"]
        plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BT Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31"})
        cls.company_goal = cls.env["strategic.objective"].with_user(cls.head).create({
            "name": "BT Reduce waste", "plan_id": plan.id, "perspective": "internal_process", "scope": "organization",
            "deadline": "2099-12-31", "owner_id": cls.head.id})
        cls.company_goal.with_user(cls.head).action_activate()
        cls.other_goal = cls.env["strategic.objective"].with_user(cls.head).create({
            "name": "BT Branch 2 plan", "plan_id": plan.id, "perspective": "internal_process", "scope": "branch",
            "warehouse_id": cls.branch2.id, "parent_objective_id": cls.company_goal.id, "deadline": "2099-06-30", "owner_id": cls.head.id})
        cls.other_goal.with_user(cls.head).action_activate()

    def _create(self, manager=None, assignee=None, **values):
        manager = manager or self.manager1
        vals = {"name": "Check milk stock", "description": "Verify the quantity and record any discrepancy.",
                "assigned_user_id": (assignee or self.barista).id, "date_deadline": str(fields.Date.today() + timedelta(days=2))}
        vals.update(values)
        row = self.Task.with_user(manager).create_branch_task(vals)
        return self.Task.browse(row["id"])

    # ------------------------------------------------------------------ 1-3: create and see
    def test_manager_creates_a_task_that_both_views_show(self):
        task = self._create(priority="1", objective_id=self.company_goal.id)
        self.assertEqual((task.state, task.warehouse_id, task.assigned_by_id), ("assigned", self.branch1, self.manager1))
        self.assertEqual((task.priority, task.objective_id), ("1", self.company_goal))
        management = self.Task.with_user(self.manager1).get_task_management()
        self.assertIn(task.id, [row["id"] for row in management["tasks"]])
        self.assertEqual({a["name"] for a in management["assignees"]}, {"bt_barista", "bt_cook"}, "only this branch's staff")
        mine = self.Task.with_user(self.barista).get_my_tasks()
        self.assertEqual([row["id"] for row in mine["tasks"]], [task.id])
        self.assertNotIn(task.id, [row["id"] for row in self.Task.with_user(self.manager1).get_my_tasks()["tasks"]],
                         "a manager's employee assignments are not their own tasks")

    def test_required_fields_and_optional_objective(self):
        for missing in ({"name": ""}, {"description": " "}, {"date_deadline": ""}):
            with self.assertRaises(ValidationError):
                self._create(**missing)
        task = self._create()
        self.assertFalse(task.objective_id, "the objective is optional")

    # ------------------------------------------------------------------ 4-7: isolation
    def test_employee_sees_only_their_own_tasks(self):
        task = self._create()
        with self.assertRaises(AccessError):
            task.with_user(self.cook).read(["name"])
        self.assertFalse(self.Task.with_user(self.cook).get_my_tasks()["tasks"])
        with self.assertRaises(AccessError):
            task.with_user(self.cook).action_start()

    def test_manager_cannot_assign_or_manage_another_branch(self):
        with self.assertRaisesRegex(AccessError, "own branch"):
            self._create(assignee=self.other_barista)
        with self.assertRaises(AccessError):
            self._create(objective_id=self.other_goal.id)
        other = self._create(manager=self.manager2, assignee=self.other_barista)
        with self.assertRaises(AccessError):
            other.with_user(self.manager1).read(["name"])
        for action in ("action_cancel", "action_accept"):
            with self.assertRaises(AccessError):
                getattr(other.with_user(self.manager1), action)()
        with self.assertRaises(AccessError):
            other.with_user(self.manager1).write({"name": "Taken over"})
        self.assertNotIn(other.id, [row["id"] for row in self.Task.with_user(self.manager1).get_task_management()["tasks"]])

    def test_manager_without_branch_and_employees_cannot_create(self):
        with self.assertRaisesRegex(AccessError, "Assign this Branch Manager"):
            self._create(manager=self.unassigned)
        with self.assertRaisesRegex(AccessError, "Assign this Branch Manager"):
            self.Task.with_user(self.unassigned).get_task_management()
        with self.assertRaises(AccessError):
            self._create(manager=self.barista, assignee=self.cook)
        with self.assertRaises(AccessError):
            self.Task.with_user(self.barista).create({"name": "x", "description": "y", "assigned_user_id": self.cook.id,
                                                      "date_deadline": "2099-01-01"})

    # ------------------------------------------------------------------ 8-12: lifecycle
    def test_full_review_cycle(self):
        task = self._create()
        as_barista = task.with_user(self.barista)
        as_barista.action_start()
        self.assertEqual(task.state, "in_progress")
        with self.assertRaises(ValidationError):
            as_barista.action_submit("")
        as_barista.action_submit("Counted 12 L, system shows 14 L.")
        self.assertEqual((task.state, task.completion_note), ("submitted", "Counted 12 L, system shows 14 L."))
        with self.assertRaises(ValidationError):
            task.with_user(self.manager1).action_request_changes("")
        task.with_user(self.manager1).action_request_changes("Record the discrepancy in Inventory too.")
        self.assertEqual(task.state, "changes_requested")
        as_barista.action_start()
        as_barista.action_submit("Recorded the 2 L adjustment.")
        task.with_user(self.manager1).action_accept()
        self.assertEqual((task.state, task.reviewed_by_id), ("done", self.manager1))
        messages = task.message_ids.mapped("body")
        self.assertTrue(any("Submission accepted" in body for body in messages), "the history is kept on the task")

    def test_employee_cannot_approve_reassign_or_skip_steps(self):
        task = self._create()
        as_barista = task.with_user(self.barista)
        with self.assertRaises(ValidationError):
            as_barista.action_submit("Done", )
        as_barista.action_start()
        as_barista.action_submit("Done")
        for action in ("action_accept", "action_cancel"):
            with self.assertRaises(AccessError):
                getattr(as_barista, action)()
        with self.assertRaises(AccessError):
            as_barista.action_request_changes("x")
        for values in ({"state": "done"}, {"assigned_user_id": self.cook.id}, {"date_deadline": "2099-01-01"}, {"name": "x"}):
            with self.assertRaises((AccessError, ValidationError)):
                as_barista.write(values)
        with self.assertRaises(ValidationError):
            task.with_user(self.manager1).write({"state": "done"})
        with self.assertRaises(ValidationError):
            task.with_user(self.manager1).write({"assigned_user_id": self.cook.id})
        with self.assertRaises(AccessError):
            task.with_user(self.manager1).action_start()
        self.assertEqual(task.state, "submitted")

    def test_invalid_transitions_are_rejected(self):
        task = self._create()
        with self.assertRaises(ValidationError):
            task.with_user(self.manager1).action_accept()
        with self.assertRaises(ValidationError):
            task.with_user(self.manager1).action_request_changes("x")
        task.with_user(self.manager1).action_cancel()
        for call in (lambda: task.with_user(self.barista).action_start(), lambda: task.with_user(self.manager1).action_cancel()):
            with self.assertRaises(ValidationError):
                call()
        with self.assertRaises(UserError):
            task.with_user(self.head).unlink()

    # ------------------------------------------------------------------ 13-14: overdue and counts
    def test_overdue_and_open_counts(self):
        late = self._create(name="Late", date_deadline=str(fields.Date.today() - timedelta(days=1)))
        on_time = self._create(name="On time")
        submitted = self._create(name="Submitted late", date_deadline=str(fields.Date.today() - timedelta(days=1)))
        submitted.with_user(self.barista).action_start()
        submitted.with_user(self.barista).action_submit("Done")
        done = self._create(name="Done")
        done.with_user(self.barista).action_start()
        done.with_user(self.barista).action_submit("Done")
        done.with_user(self.manager1).action_accept()
        cancelled = self._create(name="Cancelled", date_deadline=str(fields.Date.today() - timedelta(days=3)))
        cancelled.with_user(self.manager1).action_cancel()
        self.assertTrue(late.is_overdue)
        self.assertEqual(late.state, "assigned", "overdue keeps its workflow status")
        self.assertFalse(on_time.is_overdue)
        self.assertFalse(submitted.is_overdue, "a submission waits on the reviewer, not the employee")
        self.assertFalse(cancelled.is_overdue)
        counts = self.Task.with_user(self.manager1).get_task_management()["counts"]
        self.assertEqual(counts, {"open": 3, "overdue": 1, "review": 1}, "completed and cancelled tasks are not open")
        summary = self.Task.with_user(self.manager1).get_branch_task_summary()
        self.assertEqual([row["id"] for row in summary["attention"]], [submitted.id, late.id], "review first, then overdue")

    # ------------------------------------------------------------------ 15-16: objectives and Head Office
    def test_task_completion_does_not_touch_the_objective(self):
        before = (self.company_goal.state, self.company_goal.score, self.company_goal.scoring_status)
        task = self._create(objective_id=self.company_goal.id)
        task.with_user(self.barista).action_start()
        task.with_user(self.barista).action_submit("Done")
        task.with_user(self.manager1).action_accept()
        self.company_goal.invalidate_recordset()
        self.assertEqual((self.company_goal.state, self.company_goal.score, self.company_goal.scoring_status), before)
        self.assertEqual(before[2], "not_evaluated", "still not evaluated: no Key Result measured it")
        goal = next(g for g in self.env["strategic.goal.setup"].with_user(self.manager1).get_goals() if g["objective_id"] == self.company_goal.id)
        self.assertFalse(goal["bar"], "no progress bar made from tasks")

    def test_head_office_assigns_a_task_to_a_branch_manager(self):
        task = self.Task.with_user(self.head).create({"name": "Inspect branch inventory", "description": "Full count before Friday.",
                                                      "assigned_user_id": self.manager1.id, "date_deadline": "2099-01-10"})
        self.assertEqual(task.warehouse_id, self.branch1)
        mine = self.Task.with_user(self.manager1).get_my_tasks()["tasks"]
        self.assertEqual([row["id"] for row in mine], [task.id], "shown under the manager's own tasks")
        self.assertNotIn(task.id, [row["id"] for row in self.Task.with_user(self.manager1).get_task_management()["tasks"]])
        task.with_user(self.manager1).action_start()
        task.with_user(self.manager1).action_submit("Counted everything.")
        with self.assertRaises(AccessError):
            task.with_user(self.manager1).action_accept()
        task.with_user(self.head).action_accept()
        self.assertEqual(task.state, "done")
        with self.assertRaises(AccessError):
            self.Task.with_user(self.manager1).create_branch_task({"name": "x", "description": "y", "assigned_user_id": self.head.id,
                                                                   "date_deadline": "2099-01-01"})
