# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Installs the Stripe integration custom fields (Customer.stripe_customer_id,
# Subscription.stripe_subscription_id / stripe_customer_id,
# Payment Entry.stripe_payment_intent) on existing sites. make_custom_fields()
# is idempotent, so re-running is safe.

from payments.utils import make_custom_fields


def execute():
	make_custom_fields()
