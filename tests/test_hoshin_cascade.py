"""Hoshin cascade (Our Plan): a Branch Manager sees the company's annual objectives and only their own branch's part."""

from odoo.exceptions import AccessError
from odoo.tests.common import tagged

from .test_strategy import StrategyFixture


@tagged("post_install", "-at_install")
class TestHoshinCascade(StrategyFixture):
    def test_branch_manager_sees_own_branch_under_company_objectives(self):
        plan = self._mk_plan()
        self.env["strategic.plan"].sudo().search([("id", "!=", plan.id)]).write({"active": False})
        breakthrough = self._mk_org_obj(plan)
        breakthrough.write({"name": "Grow three branches", "hoshin_level": "breakthrough"})
        annual = self._mk_org_obj(plan)
        annual.write({"name": "Cut food waste to 5%", "hoshin_level": "annual"})
        uncovered = self._mk_org_obj(plan)
        uncovered.write({"name": "Improve customer satisfaction", "hoshin_level": "annual"})
        for objective, relation in ((annual, "primary"), (uncovered, "supporting")):  # one primary per breakthrough
            self.env["strategic.xmatrix.alignment"].with_user(self.head_user).create({
                "link_type": "B>A", "relationship_type": relation,
                "breakthrough_objective_id": breakthrough.id, "annual_objective_id": objective.id})
        for objective in annual | uncovered:
            objective.with_user(self.head_user).action_activate()
        mine = self._mk_branch_obj(plan, annual, self.wh1)
        mine.write({"name": "Record waste daily at branch A"})
        other = self._mk_branch_obj(plan, annual, self.wh2)
        Task = self.env["strategic.branch.task"].sudo()
        task_values = {"description": "Do it.", "date_deadline": "2026-12-01"}  # the branch comes from the assignee
        task = Task.create(dict(task_values, name="Weigh bins nightly", assigned_user_id=self.branch_user_a.id, objective_id=mine.id))
        Task.create(dict(task_values, name="Other branch task", assigned_user_id=self.branch_user_b.id, objective_id=other.id))

        data = self.env["strategic.performance.dashboard"].with_user(self.branch_user_a).get_branch_cascade(2026)
        self.assertEqual(data["plan"], plan.name)
        rows = {row["name"]: row for row in data["annuals"]}
        self.assertEqual(set(rows), {"Cut food waste to 5%", "Improve customer satisfaction"})
        row = rows["Cut food waste to 5%"]
        self.assertEqual(row["breakthrough"], ["Grow three branches"])
        self.assertEqual([objective["id"] for objective in row["branch_objectives"]], [mine.id], "never another branch's objective")
        self.assertEqual([t["id"] for t in row["branch_objectives"][0]["tasks"]], [task.id])
        self.assertEqual(data["uncovered"], 1)
        self.assertNotIn("score", str(data), "company results stay with Head Office")

        for user in (self.head_user, self.emp_user):
            with self.assertRaises(AccessError):
                self.env["strategic.performance.dashboard"].with_user(user).get_branch_cascade(2026)
        self.assertFalse(self.env["strategic.performance.dashboard"].with_user(self.branch_user_a).get_branch_cascade(2030)["annuals"])
