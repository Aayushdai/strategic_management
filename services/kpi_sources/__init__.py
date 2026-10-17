"""Operational and accounting source adapters used by strategic KPI evaluations."""

from .accounting import collect_accounting_result
from .pos import collect_pos_result
from .purchase import collect_purchase_result
from .inventory import collect_inventory_result

from .supplier_delivery import collect_supplier_delivery_result
