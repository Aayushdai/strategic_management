"""Hoshin monthly review (the bowling chart): each Hoshin metric's actual against target, month by month.

The metrics are the KPIs the X-Matrix links to a breakthrough objective or an initiative (active alignments).
Each month shows the latest valid organization-level evaluation whose period ends in that month; nothing is
estimated, so a month without one is shown as no data. Head Office only. Metrics off target two months in a row
get a countermeasure (models/hoshin_countermeasure.py).
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
        rows = self._hoshin_rows(company, year, today)
        Countermeasure = self.env["strategic.hoshin.countermeasure"].sudo()
        for row in rows:
            latest = Countermeasure.search([("kpi_id", "=", row["kpi_id"]), ("company_id", "=", company.id)], order="id desc", limit=1)
            row["countermeasure"] = latest._row() if latest else False
        breakthroughs = self.env["strategic.objective"].sudo().search_count([
            ("company_id", "=", company.id), ("hoshin_level", "=", "breakthrough"), ("state", "in", ("draft", "pending_approval", "active"))])
        return {"year": year, "months": list(_MONTHS), "rows": rows, "breakthrough_count": breakthroughs,
                "years": list(range(today.year - 2, today.year + 2)), "xmatrix": self._hoshin_xmatrix(company, year, rows)}

    @api.model
    def _hoshin_xmatrix(self, company, year, rows):
        """The X-Matrix of the plan covering the year: five item lists and the corner links between them.

        Links are keyed like the X-Matrix builder: "IA:<initiative>:<annual>", "BA", "IM", "BM", "IO";
        1 is a primary link (dot), 2 a supporting one (ring). Read only: links are edited in the X-Matrix workspace.
        """
        plan = self.env["strategic.plan"].sudo().search([
            ("company_id", "=", company.id), ("date_start", "<=", date(year, 12, 31)), ("date_end", ">=", date(year, 1, 1))],
            order="date_start desc, id desc", limit=1)
        if not plan:
            return False
        Objective = self.env["strategic.objective"].sudo()
        live = ("draft", "pending_approval", "active", "completed")
        objectives = {level: Objective.search([("plan_id", "=", plan.id), ("scope", "=", "organization"),
                                               ("hoshin_level", "=", level), ("state", "in", live)], order="deadline, name")
                      for level in ("breakthrough", "annual")}
        initiatives = self.env["strategic.initiative"].sudo().search(
            [("plan_id", "=", plan.id), ("state", "in", ("draft", "active", "completed"))], order="date_start, name")
        alignments = self.env["strategic.xmatrix.alignment"].sudo().search([
            ("state", "=", "active"), "|", "|", ("breakthrough_objective_id.plan_id", "=", plan.id),
            ("annual_objective_id.plan_id", "=", plan.id), ("initiative_id.plan_id", "=", plan.id)])
        metric_links = alignments.filtered(lambda alignment: alignment.link_type in ("I>M", "B>M"))
        kpis = metric_links.kpi_id.sorted("name")
        owners = alignments.filtered(lambda alignment: alignment.link_type == "I>O").owner_id | initiatives.owner_id
        tone = {row["kpi_id"]: next((cell["tone"] for cell in reversed(row["cells"]) if "target" in cell), "none") for row in rows}
        records = {"B": objectives["breakthrough"], "A": objectives["annual"], "I": initiatives, "M": kpis, "O": owners.sorted("name")}
        index = {code: {record.id: i for i, record in enumerate(recs)} for code, recs in records.items()}
        ends = {"B>A": ("BA", "B", "breakthrough_objective_id", "A", "annual_objective_id"),
                "I>A": ("IA", "I", "initiative_id", "A", "annual_objective_id"),
                "I>M": ("IM", "I", "initiative_id", "M", "kpi_id"),
                "B>M": ("BM", "B", "breakthrough_objective_id", "M", "kpi_id"),
                "I>O": ("IO", "I", "initiative_id", "O", "owner_id")}
        links = {}
        for alignment in alignments:
            corner, row_code, row_field, col_code, col_field = ends[alignment.link_type]
            r, c = index[row_code].get(alignment[row_field].id), index[col_code].get(alignment[col_field].id)
            if r is not None and c is not None:
                key = f"{corner}:{r}:{c}"
                links[key] = 1 if alignment.relationship_type == "primary" or links.get(key) == 1 else 2

        def metric_label(kpi):
            target = metric_links.filtered(lambda alignment: alignment.kpi_id == kpi)[:1].target_id
            return f"{kpi.name} (target {target.target_value:g}{(' ' + kpi.unit) if kpi.unit else ''})" if target else kpi.name

        items = {code: [{"id": record.id, "name": metric_label(record) if code == "M" else record.name,
                         "tone": tone.get(record.id, "none") if code == "M" else False} for record in recs]
                 for code, recs in records.items()}
        used, owned = set(), set()
        for key in links:
            corner, r, c = key.split(":")
            if corner == "IO":  # an owner alone does not connect an initiative to the strategy
                used.add("O" + c)
                owned.add(int(r))
            else:
                used.update({corner[0] + r, corner[1] + c})
        names = {"B": "Breakthrough objectives", "A": "Annual objectives", "I": "Improvement initiatives", "M": "Metrics", "O": "Owners"}
        findings = []
        for code, entries in items.items():
            if len(entries) > 5:
                findings.append(f"{names[code]}: {len(entries)} items. Three to five keeps it a strategy, not a to-do list.")
            for i, entry in enumerate(entries):
                entry["linked"] = code + str(i) in used and (code != "I" or i in owned)
                if code + str(i) not in used:
                    findings.append(f"{code}{i + 1} “{entry['name']}” is not connected to anything.")
                elif code == "I" and i not in owned:
                    findings.append(f"I{i + 1} “{entry['name']}” has no owner.")
        return {"plan": plan.name, "items": items, "links": links, "findings": findings}

    @api.model
    def _hoshin_rows(self, company, year, today):
        """The bowling-chart rows of one company and year. No access check: callers check, or run as the scheduler."""
        alignments = self.env["strategic.xmatrix.alignment"].sudo().search([
            ("state", "=", "active"), ("kpi_id", "!=", False), ("kpi_id.company_id", "=", company.id)])
        kpis = alignments.kpi_id
        evaluations = self.env["strategic.kpi.evaluation"].sudo().search([
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
            # Consecutive off-target months up to the latest month that has a result.
            streak = 0
            for cell in reversed([cell for cell in cells if "target" in cell]):
                if cell["tone"] != "bad":
                    break
                streak += 1
            initiative_links = links.initiative_id.alignment_ids.filtered(lambda alignment: alignment.state == "active")
            owners = links.owner_id | links.initiative_id.owner_id
            rows.append({
                "kpi_id": kpi.id, "name": kpi.name, "unit": kpi.unit or "",
                "breakthrough": links.breakthrough_objective_id.mapped("name"),
                "annual": (links.annual_objective_id | initiative_links.annual_objective_id).mapped("name"),
                "initiatives": links.initiative_id.mapped("name"),
                "owners": owners.mapped("name"), "owner_ids": owners.ids,
                "cells": cells, "red_streak": streak,
            })
        return rows
