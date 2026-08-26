# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import frappe
from frappe.model.document import Document


class StripeWebhookLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		error: DF.LongText | None
		event_type: DF.Data | None
		payload: DF.Code | None
		reference_doctype: DF.Data | None
		reference_name: DF.Data | None
		status: DF.Literal["Received", "Processed", "Pending", "Ignored", "Failed"]
		stripe_event_id: DF.Data
		stripe_object_id: DF.Data | None
		stripe_settings: DF.Link | None
	# end: auto-generated types

	pass
