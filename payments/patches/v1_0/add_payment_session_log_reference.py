# Copyright (c) 2026, Frappe and Contributors
# See LICENSE
"""Create the Payment Request -> Payment Session Log reference on existing sites.

make_custom_fields only runs at after_install, and the field used to sit inside
a block gated on the Web Form "payments_tab" already being absent — so on every
site that already had that tab, this field was silently never created.

The field is a correlation point for a reference document; nothing in the
installed ERPNext v16.30.0 reads it yet.
"""

from payments.utils.utils import make_payment_request_custom_fields


def execute():
	make_payment_request_custom_fields()
