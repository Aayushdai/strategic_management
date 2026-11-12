"""Hoshin Kanri removal: delete its stored views before the upgrade loads the new code.

Odoo re-validates the Objectives list and form while loading, and removes obsolete views only at the end, so the
Hoshin extensions (which show the removed "hoshin_level" field) would stop the upgrade. Their models and tables are
removed by Odoo itself afterwards, because no code defines them any more.
"""

import logging

_logger = logging.getLogger(__name__)

HOSHIN_VIEWS = (
    "view_strategic_objective_list_hoshin", "view_strategic_objective_form_hoshin",
    "view_strategic_initiative_list", "view_strategic_initiative_form", "view_strategic_initiative_search",
    "view_strategic_xmatrix_alignment_list", "view_strategic_xmatrix_alignment_form", "view_strategic_xmatrix_alignment_search",
    "view_strategic_xmatrix_alignment_history_list", "view_strategic_xmatrix_alignment_history_form",
    "view_strategic_xmatrix_revision_wizard_form", "view_strategic_hoshin_countermeasure_form",
)


def migrate(cr, version):
    cr.execute(
        """DELETE FROM ir_ui_view WHERE id IN (
               SELECT res_id FROM ir_model_data WHERE module = 'strategic_management' AND model = 'ir.ui.view' AND name IN %s)""",
        (HOSHIN_VIEWS,),
    )
    removed = cr.rowcount
    cr.execute("DELETE FROM ir_model_data WHERE module = 'strategic_management' AND model = 'ir.ui.view' AND name IN %s", (HOSHIN_VIEWS,))
    _logger.info("Hoshin removal: deleted %s stored views.", removed)
