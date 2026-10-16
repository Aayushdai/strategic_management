"""Backfill the first version record for KPI definitions created pre-versioning.

This migration snapshots the legacy KPI contract as version 1, effective from
1970-01-01 so existing historical reporting periods can resolve it. The reason
text explicitly records that this is a technical migration snapshot, not a
retrospective business approval of the old formula.
"""

from odoo import SUPERUSER_ID, api, fields


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Kpi = env["strategic.kpi.definition"].sudo().with_context(active_test=False)
    Version = env["strategic.kpi.definition.version"].sudo().with_context(
        _create_initial_kpi_version=True
    )

    legacy_kpis = Kpi.search([])
    for kpi in legacy_kpis:
        # Be idempotent: never add another initial version if one already exists.
        if Version.search_count([("kpi_id", "=", kpi.id)]):
            continue

        Version.create({
            "kpi_id": kpi.id,
            "version_number": 1,
            "unit": kpi.unit,
            "direction": kpi.direction,
            "source_type": kpi.source_type,
            "description": kpi.description,
            "effective_from": "1970-01-01",
            "change_reason": (
                "Initial KPI contract migrated from a definition created before "
                "versioning was introduced. This is a technical history snapshot, "
                "not a retrospective business approval of a historical formula."
            ),
            "state": "approved",
            "approved_by_id": SUPERUSER_ID,
            "approved_at": fields.Datetime.now(),
        })
