"""Hoshin monthly review (bowling chart): X-Matrix metrics, actual against target per month, red streaks."""

from odoo.exceptions import AccessError
from odoo.tests.common import tagged

from .test_strategy import StrategyFixture


@tagged("post_install", "-at_install")
class TestHoshinReview(StrategyFixture):
    def _month_result(self, kpi, start, end, actual):
        target = self.env["strategic.kpi.target"].with_user(self.head_user).create({
            "kpi_id": kpi.id, "date_start": start, "date_end": end, "scope": "organization", "target_value": 100.0, "state": "approved"})
        return self.env["strategic.kpi.evaluation"].with_user(self.head_user).create({"target_id": target.id, "actual_value": actual, "has_actual_value": True})

    def test_bowling_chart_rows_months_and_red_streak(self):
        plan = self._mk_plan()
        breakthrough = self._mk_org_obj(plan)
        breakthrough.write({"name": "HR Breakthrough", "hoshin_level": "breakthrough"})
        kpi = self._mk_kpi("HR-REVIEW-METRIC")
        year_target = self.env["strategic.kpi.target"].with_user(self.head_user).create({
            "kpi_id": kpi.id, "date_start": "2026-01-01", "date_end": "2026-12-31", "scope": "organization", "target_value": 100.0, "state": "approved"})
        self.env["strategic.xmatrix.alignment"].with_user(self.head_user).create({
            "link_type": "B>M", "relationship_type": "primary", "breakthrough_objective_id": breakthrough.id,
            "kpi_id": kpi.id, "target_id": year_target.id, "effective_date": "2026-01-01"})
        self._month_result(kpi, "2026-01-01", "2026-01-31", 120.0)
        self._month_result(kpi, "2026-02-01", "2026-02-28", 60.0)
        self._month_result(kpi, "2026-03-01", "2026-03-31", 50.0)

        data = self.env["strategic.performance.dashboard"].with_user(self.head_user).get_hoshin_review(2026)
        row = next(r for r in data["rows"] if r["kpi_id"] == kpi.id)
        self.assertEqual(row["breakthrough"], ["HR Breakthrough"])
        jan, feb, mar, apr = row["cells"][:4]
        self.assertEqual((jan["tone"], jan["actual"], jan["target"]), ("good", 120.0, 100.0))
        self.assertEqual((feb["tone"], mar["tone"]), ("bad", "bad"))
        self.assertNotIn("target", apr, "no result in April: shown as no data, never as zero")
        self.assertEqual(row["red_streak"], 2)

        for user in (self.branch_user_a, self.emp_user):
            with self.assertRaises(AccessError):
                self.env["strategic.performance.dashboard"].with_user(user).get_hoshin_review(2026)
