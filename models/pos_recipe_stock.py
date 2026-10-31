"""Phase 9E: lot-tracked recipe ingredients in POS sales, and POS stock failures that stay visible.

A POS sale gives lot numbers only for the product it sells. An ingredient of a kit (Milk in a Latte) gets none, so Odoo
cannot validate the picking, and pos_stock swallows that error. Here such ingredients take real lots from the branch's own
stock, soonest expiry first. A shortfall is never consumed: its quantity stays without a lot, so validation keeps refusing,
and the failure is recorded on the picking for an authorized retry. The order and its payment are not affected.
"""

import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

_SUMMARY = "POS stock not processed"
_TRACKED = ("lot", "serial")


class StockMove(models.Model):
    _inherit = "stock.move"

    def _add_mls_related_to_order(self, related_order_lines, are_qties_done=True):
        result = super()._add_mls_related_to_order(related_order_lines, are_qties_done=are_qties_done)
        sold = related_order_lines.product_id
        self.filtered(lambda move: move.product_id not in sold)._strategic_allocate_recipe_lots()
        return result

    def _strategic_valid_lot_quants(self):
        """This move's product in valid lots at its own source location: available, unexpired, soonest expiry first."""
        self.ensure_one()
        quants = self.env["stock.quant"].sudo().search([
            ("product_id", "=", self.product_id.id), ("location_id", "child_of", self.location_id.id),
            ("lot_id", "!=", False), ("quantity", ">", 0), ("location_id.usage", "=", "internal")])
        now = fields.Datetime.now()
        has_expiry = "expiration_date" in self.env["stock.lot"]._fields

        def expiry(quant):
            return quant.lot_id.expiration_date if has_expiry else False

        uom = self.product_id.uom_id
        valid = quants.filtered(lambda quant: not (expiry(quant) and expiry(quant) <= now)
                                and quant.quantity > quant.reserved_quantity and not uom.is_zero(quant.quantity - quant.reserved_quantity))
        # A lot without an expiry date goes last: dated stock must not be left to expire behind it.
        return valid.sorted(lambda quant: (not expiry(quant), expiry(quant) or now, quant.in_date or now, quant.id))

    def _strategic_allocate_recipe_lots(self, keep_chosen=False):
        """Set real lots on lot-tracked ingredients; Odoo's own reservation (oldest first, expired included) is replaced.
        With keep_chosen, lines already holding a lot stay: a retry must respect what an inventory user entered.
        Only outgoing stock; returns keep Odoo's flow."""
        for move in self.sudo().filtered(lambda m: m.product_id.tracking in _TRACKED and m.location_id.usage == "internal"
                                         and m.state not in ("done", "cancel")):
            uom = move.product_id.uom_id
            needed = move.uom_id._compute_quantity(move.product_uom_qty, uom, rounding_method="HALF-UP")
            move.move_line_ids.filtered(lambda line: not (keep_chosen and line.lot_id)).unlink()
            needed -= sum(line.uom_id._compute_quantity(line.quantity, uom) for line in move.move_line_ids)
            values = []
            for quant in move._strategic_valid_lot_quants():
                if needed <= 0 or uom.is_zero(needed):
                    break
                quantity = min(needed, quant.quantity - quant.reserved_quantity)
                needed -= quantity
                values.append({"move_id": move.id, "picking_id": move.picking_id.id, "product_id": move.product_id.id, "uom_id": uom.id,
                               "location_id": quant.location_id.id, "location_dest_id": move.location_dest_id.id, "lot_id": quant.lot_id.id,
                               "quantity": quantity, "picked": True, "company_id": move.company_id.id})
            if needed > 0 and not uom.is_zero(needed):
                # Not consumed: without a lot, Odoo's validation refuses this line and nothing leaves stock.
                values.append({"move_id": move.id, "picking_id": move.picking_id.id, "product_id": move.product_id.id, "uom_id": uom.id,
                               "location_id": move.location_id.id, "location_dest_id": move.location_dest_id.id,
                               "quantity": needed, "picked": True, "company_id": move.company_id.id})
            self.env["stock.move.line"].sudo().create(values)
            move.picked = True


class StockPicking(models.Model):
    _inherit = "stock.picking"

    strategic_pos_stock_error = fields.Text(
        string="POS Stock Error", readonly=True, copy=False,
        help="Why this POS sale's stock could not be processed. The order and its payment are not affected.")

    @api.model
    def _create_picking_from_pos_order_lines(self, location_dest_id, lines, picking_type, partner=False):
        pickings = super()._create_picking_from_pos_order_lines(location_dest_id, lines, picking_type, partner=partner)
        # pos_stock links the picking to its order only after this returns, so the order is named from the sold lines.
        pickings.filtered(lambda picking: picking.state not in ("done", "cancel"))._strategic_record_pos_stock_failure(lines.sudo().order_id)
        return pickings

    def _create_move_from_pos_order_lines(self, lines):
        # Exploding a kit copies stock moves, and with them fields a POS User cannot read (scrap reasons), so no POS-only
        # cashier could sell a kit. Only POS calls this private step, after the order itself passed the cashier's rights.
        return super(StockPicking, self.sudo())._create_move_from_pos_order_lines(lines)

    def _strategic_try_done(self):
        """Validate; the error text on failure, False on success. A savepoint leaves no partial stock change behind."""
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self._action_done()
        except (UserError, ValidationError) as error:
            return str(error.args[0]) if error.args else str(error)
        return False

    def _strategic_shortfall_text(self):
        """The missing quantities per ingredient, which is what an inventory user has to act on."""
        texts = []
        for line in self.sudo().move_line_ids.filtered(lambda ml: ml.product_id.tracking in _TRACKED and not ml.lot_id):
            texts.append("%s: %s %s has no valid lot at %s." % (line.product_id.display_name, line.quantity, line.uom_id.name, line.location_id.display_name))
        return " ".join(texts)

    def _strategic_record_pos_stock_failure(self, orders):
        """pos_stock discards the validation error: read it again and keep it where it can be acted on."""
        for picking in self.sudo():
            error = picking._strategic_try_done()
            if not error:
                continue
            text = " ".join(part for part in ("Order %s." % ", ".join(orders.mapped("name")), picking._strategic_shortfall_text(), error) if part)
            picking.strategic_pos_stock_error = text
            _logger.error("POS stock not processed for %s: %s", picking.name, text)
            picking.message_post(body="POS stock not processed: %s" % text, subtype_xmlid="mail.mt_note")
            branch = picking.picking_type_id.warehouse_id
            self.env["strategic.kpi.target"].sudo()._create_deduplicated_activity(
                picking, "%s: %s" % (_SUMMARY, picking.name), text, warehouse=branch, scope="branch", company=picking.company_id)

    def _strategic_can_retry_pos_stock(self):
        user = self.env.user
        return user.has_group("stock.group_stock_user") or user.has_group("strategic_management.group_strategic_branch_manager")

    def action_strategic_retry_pos_stock(self):
        """Re-allocate lots and validate a POS picking that recorded a failure. Safe to repeat: no order, payment or second move is created."""
        if not self._strategic_can_retry_pos_stock():
            raise AccessError("POS stock is retried by Inventory users, Branch Managers and Head Office.")
        self.check_access("write")
        for picking in self:
            if picking.state == "done":
                continue
            # Only failures recorded by this mechanism: older stuck pickings need a stock count first.
            if not picking.strategic_pos_stock_error:
                raise UserError("%s has no recorded POS stock failure to retry." % picking.name)
            picking = picking.sudo()
            picking.move_ids._strategic_allocate_recipe_lots(keep_chosen=True)
            error = picking._strategic_try_done()
            if error:
                # Not raised: that would roll back the allocation and this up-to-date reason.
                picking.strategic_pos_stock_error = " ".join(part for part in (picking._strategic_shortfall_text(), error) if part)
                return {"type": "ir.actions.client", "tag": "display_notification",
                        "params": {"title": _SUMMARY, "message": picking.strategic_pos_stock_error, "type": "danger", "sticky": True}}
        return True

    def _action_done(self):
        result = super()._action_done()
        # Solved by a retry or by an inventory user entering the lot: the failure is over.
        solved = self.sudo().filtered(lambda picking: picking.state == "done" and picking.strategic_pos_stock_error)
        if solved:
            solved.strategic_pos_stock_error = False
            solved.activity_ids.filtered(lambda activity: (activity.summary or "").startswith(_SUMMARY)).action_feedback(feedback="Stock processed.")
        return result
