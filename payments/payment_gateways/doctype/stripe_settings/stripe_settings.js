// Copyright (c) 2017, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Stripe Settings", {
  refresh(frm) {
    const key = frm.doc.publishable_key || "";
    if (key.startsWith("pk_test_")) {
      frm.page.set_indicator(__("Test mode"), "orange");
      frm.dashboard.set_headline_alert(
        __(
          "Test mode: payments use Stripe's test cards and no money is charged."
        ),
        "orange"
      );
    } else if (key.startsWith("pk_live_")) {
      frm.page.set_indicator(__("Live"), "green");
    }
    if (frm.is_new()) return;
    const url = frappe.urllib.get_full_url(
      "/api/method/payments.payment_gateways.doctype.stripe_settings.stripe_settings.webhook?gateway=" +
        encodeURIComponent(frm.doc.name)
    );
    frm.set_df_property(
      "webhook_secret",
      "description",
      __(
        "The signing secret of a Stripe webhook sending payment_intent.succeeded, refund.created, refund.updated, refund.failed, charge.dispute.created and charge.dispute.closed to {0}. Payments are then recorded even when the payer doesn't return to the site, and Accounts Managers are told when a customer disputes a payment or a payment is refunded in Stripe, or a refund fails.",
        [`<code>${frappe.utils.escape_html(url)}</code>`]
      )
    );
  },
});
