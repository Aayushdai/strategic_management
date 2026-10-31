"""Branch Manager Phase 4: Employee Management, a read-only view of each branch employee's task workload."""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestEmployeeManagement(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        # Fresh branches: their staff and tasks are only this fixture's, whatever else the database holds.
        cls.branch1 = Warehouse.create({"name": "EM Branch 1", "code": "EMB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "EM Branch 2", "code": "EMB2", "company_id": cls.company.id})

        def user(login, groups, warehouse=False):
            return cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "email": "%s@example.com" % login,
                "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                "group_ids": [(6, 0, [ref("base.group_user").id] + [ref(group).id for group in groups])],
                "strategic_branch_warehouse_id": warehouse.id if warehouse else False})

        employee, manager = ["strategic_management.group_strategic_employee"], ["strategic_management.group_strategic_branch_manager"]
        cls.head = user("em_head", ["strategic_management.group_strategic_head_office_manager"])
        cls.manager1 = user("em_manager1", manager, cls.branch1)
        cls.co_manager = user("em_co_manager", manager, cls.branch1)
        cls.manager2 = user("em_manager2", manager, cls.branch2)
        cls.unassigned = user("em_unassigned", manager)
        cls.barista = user("em_barista", employee, cls.branch1)
        cls.cook = user("em_cook", employee, cls.branch1)
        cls.no_access = user("em_no_access", [], cls.branch1)
        cls.other_barista = user("em_other_barista", employee, cls.branch2)
        cls.Task = cls.env["strategic.branch.task"]

    def _create(self, name, assignee=None, manager=None, days=2):
        row = self.Task.with_user(manager or self.manager1).create_branch_task({
            "name": name, "description": "Do it.", "assigned_user_id": (assignee or self.barista).id,
            "date_deadline": str(fields.Date.today() + timedelta(days=days))})
        return self.Task.browse(row["id"])

    def _workload(self):
        """One of each lifecycle state for the barista, plus one task for the other branch."""
        tasks = {
            "assigned": self._create("Assigned"),
            "overdue": self._create("Overdue", days=-2),
            "in_progress": self._create("In progress late", days=-1),
            "submitted": self._create("Submitted late", days=-1),
            "changes": self._create("Changes requested"),
            "done": self._create("Done"),
            "cancelled": self._create("Cancelled", days=-5),
        }
        tasks["in_progress"].with_user(self.barista).action_start()
        for key in ("submitted", "changes", "done"):
            tasks[key].with_user(self.barista).action_start()
            tasks[key].with_user(self.barista).action_submit("Finished.")
        tasks["changes"].with_user(self.manager1).action_request_changes("Add the count sheet.")
        tasks["done"].with_user(self.manager1).action_accept()
        tasks["cancelled"].with_user(self.manager1).action_cancel()
        tasks["other_branch"] = self._create("Other branch", assignee=self.other_barista, manager=self.manager2)
        return tasks

    def _rows(self, manager=None):
        return {row["id"]: row for row in self.Task.with_user(manager or self.manager1).get_employee_management()["employees"]}

    # ------------------------------------------------------------------ 1-2: access and who is listed
    def test_manager_sees_only_the_staff_of_their_branch(self):
        data = self.Task.with_user(self.manager1).get_employee_management()
        self.assertEqual(data["branch"]["id"], self.branch1.id)
        rows = {row["id"]: row for row in data["employees"]}
        self.assertEqual(set(rows), {self.barista.id, self.cook.id, self.no_access.id},
                         "not the manager, the co-manager, Head Office or another branch's staff")
        self.assertTrue(rows[self.barista.id]["can_receive_tasks"])
        self.assertFalse(rows[self.barista.id]["note"])
        self.assertFalse(rows[self.no_access.id]["can_receive_tasks"], "listed although the task form cannot assign them")
        self.assertIn("no Strategic Management employee access", rows[self.no_access.id]["note"])
        self.assertEqual([row["name"] for row in data["employees"]], sorted(row["name"] for row in data["employees"]))
        self.assertEqual(set(self._rows(self.manager2)), {self.other_barista.id})

    # ------------------------------------------------------------------ 3-7: counts
    def test_counts_follow_task_management_definitions(self):
        tasks = self._workload()
        row = self._rows()[self.barista.id]
        # Open: assigned, overdue, in progress, submitted, changes requested. Overdue: the two late ones still with the employee.
        self.assertEqual(row["counts"], {"open": 5, "overdue": 2, "review": 1},
                         "done and cancelled are not open; a late submission awaits review, not the employee")
        mine = self.Task.search([("assigned_user_id", "=", self.barista.id), ("warehouse_id", "=", self.branch1.id)])
        self.assertEqual(row["counts"]["open"], len(mine.filtered(lambda t: t.state in ("assigned", "in_progress", "changes_requested", "submitted"))))
        self.assertEqual(row["counts"]["overdue"], len(mine.filtered("is_overdue")))
        self.assertEqual({t for t in mine if t.is_overdue}, {tasks["overdue"], tasks["in_progress"]})
        self.assertEqual(row["counts"]["review"], len(mine.filtered(lambda t: t.state == "submitted")))
        self.assertEqual(self._rows()[self.cook.id]["counts"], {"open": 0, "overdue": 0, "review": 0}, "no tasks, all zero")

    def test_totals_match_task_management(self):
        self._workload()
        self._create("Cook task", assignee=self.cook, days=-1)
        # Head Office's task for the manager is the manager's own work, so neither page counts it.
        self.Task.with_user(self.head).create({"name": "For the manager", "description": "x", "assigned_user_id": self.manager1.id,
                                               "date_deadline": str(fields.Date.today() - timedelta(days=1))})
        rows = self._rows().values()
        totals = {key: sum(row["counts"][key] for row in rows) for key in ("open", "overdue", "review")}
        self.assertEqual(totals, self.Task.with_user(self.manager1).get_task_management()["counts"])

    # ------------------------------------------------------------------ 8-9: detail view and its links
    def test_employee_detail_shows_current_work_and_history(self):
        tasks = self._workload()
        detail = self.Task.with_user(self.manager1).get_employee_tasks(self.barista.id)
        self.assertEqual(detail["employee"]["name"], "em_barista")
        self.assertEqual(detail["employee"]["counts"], self._rows()[self.barista.id]["counts"], "same counts as the list")
        current = {row["id"]: row for row in detail["current"]}
        self.assertEqual(set(current), {tasks[key].id for key in ("assigned", "overdue", "in_progress", "submitted", "changes")})
        self.assertEqual({row["id"] for row in detail["history"]}, {tasks["done"].id, tasks["cancelled"].id})
        self.assertEqual(detail["history_total"], 2)
        self.assertEqual({row["status"] for row in detail["history"]}, {"Completed", "Cancelled"})
        self.assertEqual(current[tasks["submitted"].id]["status"], "Submitted for Review")
        self.assertTrue(current[tasks["submitted"].id]["can_review"])
        self.assertFalse(current[tasks["submitted"].id]["is_overdue"])
        self.assertTrue(current[tasks["overdue"].id]["is_overdue"])
        # Every task links to a record Task Management itself holds, so the existing task dialog opens it.
        managed = {row["id"]: row for row in self.Task.with_user(self.manager1).get_task_management()["tasks"]}
        for row in detail["current"] + detail["history"]:
            self.assertEqual(managed[row["id"]], row)
        self.assertNotIn(tasks["other_branch"].id, current)
        empty = self.Task.with_user(self.manager1).get_employee_tasks(self.cook.id)
        self.assertEqual((empty["current"], empty["history"], empty["employee"]["counts"]), ([], [], {"open": 0, "overdue": 0, "review": 0}))

    def test_someone_who_left_the_branch_keeps_their_branch_tasks_visible(self):
        task = self._create("Before the move", days=-1)
        self.barista.sudo().strategic_branch_warehouse_id = self.branch2
        row = self._rows()[self.barista.id]
        self.assertEqual((row["note"], row["can_receive_tasks"]), ("No longer in this branch", False))
        self.assertEqual(row["counts"], {"open": 1, "overdue": 1, "review": 0})
        self.assertEqual([r["id"] for r in self.Task.with_user(self.manager1).get_employee_tasks(self.barista.id)["current"]], [task.id])
        self.assertIn(self.barista.id, self._rows(self.manager2), "now listed in the new branch")
        self.assertEqual(self._rows(self.manager2)[self.barista.id]["counts"]["open"], 0, "branch 1's task is not branch 2's work")

    # ------------------------------------------------------------------ 10-12: isolation
    def test_other_branches_and_unknown_people_are_refused(self):
        tasks = self._workload()
        for person in (self.other_barista.id, self.manager2.id, self.head.id, self.co_manager.id, self.manager1.id, 0, "1", None):
            with self.assertRaises(AccessError, msg=person):
                self.Task.with_user(self.manager1).get_employee_tasks(person)
        with self.assertRaises(AccessError):
            self.Task.with_user(self.manager2).get_employee_tasks(self.barista.id)
        with self.assertRaises(AccessError):
            tasks["other_branch"].with_user(self.manager1).read(["name"])
        with self.assertRaises(AccessError):
            tasks["assigned"].with_user(self.manager2).read(["name"])

    def test_manager_without_a_branch_gets_nothing(self):
        with self.assertRaises(AccessError):
            self.Task.with_user(self.unassigned).get_employee_management()
        with self.assertRaises(AccessError):
            self.Task.with_user(self.unassigned).get_employee_tasks(self.barista.id)

    # ------------------------------------------------------------------ 13-15: other roles unchanged
    def test_employees_keep_my_tasks_and_cannot_use_manager_functions(self):
        task = self._create("Mine")
        with self.assertRaises(AccessError):
            self.Task.with_user(self.barista).get_employee_management()
        with self.assertRaises(AccessError):
            self.Task.with_user(self.barista).get_employee_tasks(self.barista.id)
        self.assertEqual([row["id"] for row in self.Task.with_user(self.barista).get_my_tasks()["tasks"]], [task.id])
        task.with_user(self.barista).action_start()
        self.assertEqual(task.state, "in_progress")

    def test_head_office_permissions_unchanged(self):
        self.assertIn(self.cook.id, [row["id"] for row in self.Task.with_user(self.head).get_employee_management()["employees"]], "Head Office sees every branch")
        task = self.Task.with_user(self.head).create({"name": "HO task", "description": "x", "assigned_user_id": self.cook.id,
                                                      "date_deadline": "2099-01-10"})
        self.assertEqual(task.warehouse_id, self.branch1)
        self.assertIn(task.id, [row["id"] for row in self.Task.with_user(self.manager1).get_employee_tasks(self.cook.id)["current"]])
        people = self.env["strategic.performance.dashboard"].with_user(self.head).get_people_overview_data()
        self.assertEqual(people["role"], "head_office")
