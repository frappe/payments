// Adds a "Refund via Stripe" action to submitted Payment Entries with a Stripe intent.

frappe.ui.form.on("Payment Entry", {
  refresh(frm) {
    if (frm.doc.docstatus === 1 && frm.doc.stripe_payment_intent) {
      frm.add_custom_button(__("Refund via Stripe"), () => {
        const d = new frappe.ui.Dialog({
          title: __("Refund via Stripe"),
          fields: [
            {
              fieldname: "amount",
              fieldtype: "Currency",
              label: __("Amount (leave blank for full refund)"),
            },
          ],
          primary_action_label: __("Refund"),
          primary_action(values) {
            d.hide();
            frappe.call({
              method:
                "payments.payment_gateways.doctype.stripe_settings.stripe_settings.refund_payment_entry",
              args: {
                payment_entry: frm.doc.name,
                amount: values.amount || null,
              },
              freeze: true,
              freeze_message: __("Requesting refund from Stripe..."),
              callback(r) {
                if (r.message) {
                  frappe.msgprint(
                    __(
                      "Stripe refund {0} is {1}. The ledger will update from the webhook.",
                      [r.message.refund, r.message.status]
                    )
                  );
                }
              },
            });
          },
        });
        d.show();
      });
    }
  },
});
