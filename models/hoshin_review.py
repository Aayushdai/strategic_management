"""Hoshin monthly review (the bowling chart): each Hoshin metric's actual against target, month by month.

The metrics are the KPIs the X-Matrix links to a breakthrough objective or an initiative (active alignments).
Each month shows the latest valid organization-level evaluation whose period ends in that month; nothing is
estimated, so a month without one is shown as no data. Head Office only.
"""

from datetime import date

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# Bowling-chart colours from the evaluation's own verdict.
_TONE = {"exceeded": "good", "achieved": "good", "almost_met": "warn", "not_achieved": "bad"}


class StrategicPerformanceDashboardHoshinReview(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def get_hoshin_review(self, year=False):
        if self._strategic_role() != "head_office":
            raise AccessError("The Hoshin review is for Head Office.")
        today = fields.Date.context_today(self)
        if year in (False, None, ""):
            year = today.year
        if not isinstance(year, int) or isinstance(year, bool) or not 2000 <= year <= 2100:
            raise ValidationError("Choose a valid year.")
        company = self.env.company
        alignments = self.env["strategic.xmatrix.alignment"].sudo().search([
            ("state", "=", "active"), ("kpi_id", "!=", False), ("kpi_id.company_id", "=", company.id)])
        kpis = alignments.kpi_id
        Evaluation = self.env["strategic.kpi.evaluation"].sudo()
        evaluations = Evaluation.search([
            ("kpi_id", "in", kpis.ids), ("company_id", "=", company.id), ("scope", "=", "organization"),
            ("superseded_by_id", "=", False), ("status", "=", "valid"),
            ("date_end", ">=", date(year, 1, 1)), ("date_end", "<=", date(year, 12, 31))], order="evaluated_at, id")
        latest = {}
        for evaluation in evaluations:  # ordered oldest first, so the last one per month wins
            latest[(evaluation.kpi_id.id, evaluation.date_end.month)] = evaluation
        last_month = today.month if year == today.year else 12 if year < today.year else 0
        rows = []
        for kpi in kpis.sorted("name"):
            links = alignments.filtered(lambda alignment: alignment.kpi_id == kpi)
            cells = []
            for month in range(1, 13):
                evaluation = latest.get((kpi.id, month))
                if not evaluation:
                    cells.append({"month": month, "tone": "future" if month > last_month else "none"})
                    continue
                cells.append({"month": month, "tone": _TONE.get(evaluation.attainment_status, "none"),
                              "actual": evaluation.actual_value if evaluation.has_actual_value else False,
                              "target": evaluation.target_value, "status": dict(evaluation._fields["attainment_status"].selection).get(evaluation.attainment_status)})
            # Consecutive off-target months up to the latest month that has a result (step 3 acts on this).
            streak = 0
            for cell in reversed([cell for cell in cells if cell["tone"] not in ("future", "none")]):
                if cell["tone"] != "bad":
                    break
                streak += 1
            annual = (links.annual_objective_id | links.initiative_id.alignment_ids.filtered(lambda a: a.state == "active").annual_objective_id)
            rows.append({
                "kpi_id": kpi.id, "name": kpi.name, "unit": kpi.unit or "",
                "breakthrough": links.breakthrough_objective_id.mapped("name"),
                "annual": annual.mapped("name"), "initiatives": links.initiative_id.mapped("name"),
                "owners": (links.owner_id | links.initiative_id.owner_id).mapped("name"),
                "cells": cells, "red_streak": streak,
            })
        breakthroughs = self.env["strategic.objective"].sudo().search_count([
            ("company_id", "=", company.id), ("hoshin_level", "=", "breakthrough"), ("state", "in", ("draft", "pending_approval", "active"))])
        return {"year": year, "months": list(_MONTHS), "rows": rows, "breakthrough_count": breakthroughs,
                "years": list(range(today.year - 2, today.year + 2))}
