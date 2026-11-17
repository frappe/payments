// Copyright (c) 2016, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("PayPal Settings", {
  refresh(frm) {
    if (frm.doc.client_id || frm.doc.api_username) {
      if (frm.doc.paypal_sandbox) {
        frm.page.set_indicator(__("Sandbox"), "orange");
        frm.dashboard.set_headline_alert(
          __(
            "Sandbox: payments come from PayPal sandbox accounts and no money moves."
          ),
          "orange"
        );
      } else {
        frm.page.set_indicator(__("Live"), "green");
      }
    }
    if (!frm.doc.client_id && frm.doc.api_username) {
      frm.dashboard.set_headline_alert(
        __(
          "Payments use PayPal's classic API. Add your PayPal app's Client ID and Client Secret to use its REST API."
        ),
        "yellow"
      );
    }
    const url = frappe.urllib.get_full_url(
      "/api/method/payments.payment_gateways.doctype.paypal_settings.paypal_settings.webhook"
    );
    frm.set_df_property(
      "webhook_id",
      "description",
      __(
        "The ID PayPal shows for a webhook you add to your app for {0}, with the events {1}. Payments are then recorded even when the payer doesn't come back to the site, and Accounts Managers are told about refunds made in PayPal, reversals and disputes.",
        [
          `<code>${frappe.utils.escape_html(url)}</code>`,
          [
            "CHECKOUT.ORDER.APPROVED",
            "PAYMENT.CAPTURE.COMPLETED",
            "PAYMENT.CAPTURE.DENIED",
            "PAYMENT.CAPTURE.REFUNDED",
            "PAYMENT.CAPTURE.REVERSED",
            "CUSTOMER.DISPUTE.CREATED",
            "CUSTOMER.DISPUTE.RESOLVED",
          ].join(", "),
        ]
      )
    );
  },
});
