"""Stop treating organization and branch KPIs as employees' personal results.

No KPI was ever marked individual, so every employee target predates the ownership rule. Targets are archived, never
deleted, with a note on each; evaluations and measurements stay as history and simply no longer count as employee
results. A KPI that really has one individual owner can be marked and given new targets by Head Office.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)
NOTE = "Archived by the KPI ownership correction: %s is a business-level KPI, not an individual employee result. Its past evaluations are kept for audit."


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    targets = env["strategic.kpi.target"].with_context(active_test=False).search([
        ("scope", "=", "user"), ("kpi_id.individual_outcome", "=", False), ("state", "!=", "archived")])
    for target in targets:
        target.state = "archived"
        target.message_post(body=NOTE % target.kpi_id.display_name, subtype_xmlid="mail.mt_note")
        _logger.info("KPI ownership correction archived target %s (%s, %s).", target.id, target.kpi_id.code, target.user_id.login)
