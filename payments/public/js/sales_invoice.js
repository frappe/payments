// Copy the link the customer pays this invoice through (payments.utils.invoice_links).
frappe.ui.form.on("Sales Invoice", {
  refresh(frm) {
    if (
      frm.doc.docstatus !== 1 ||
      frm.doc.outstanding_amount <= 0 ||
      frm.doc.is_return
    )
      return;
    frm.add_custom_button(__("Copy Payment Link"), async () => {
      const link = await frappe.xcall(
        "payments.utils.invoice_links.get_invoice_payment_link",
        {
          invoice: frm.doc.name,
        }
      );
      if (!link) {
        frappe.msgprint(
          __(
            "This invoice has no payment link. Check Invoice Payment Links in Accounts Settings, the customer's setting, and that a Payment Gateway Account takes {0}.",
            [frm.doc.currency]
          )
        );
        return;
      }
      frappe.utils.copy_to_clipboard(link);
    });
  },
});
