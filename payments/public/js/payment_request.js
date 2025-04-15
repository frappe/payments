frappe.ui.form.on("Payment Request", {
  refresh(frm) {
    const isInward = frm.doc.payment_request_type === "Inward";
    const isInitiated = frm.doc.status === "Initiated";
    const gatewayAccount = frm.doc.payment_gateway_account || "";
    const firstWord = gatewayAccount.split("-")[0].trim().toLowerCase();

    const isBankMuscat = firstWord === "bankmuscat";

    if (
      isInward &&
      isInitiated &&
      isBankMuscat &&
      frm.doc.custom_payment_reference_no
    ) {
      frm.add_custom_button(__("Check Payment Status"), function () {
        frappe.call({
          method:
            "payments.templates.pages.bankmuscat_checkout.get_payment_status",
          freeze: true,
          freeze_message: __("Fetching payment status..."),
          args: {
            payment_request: frm.doc.name,
          },
          callback: function (r) {
            if (r.message) {
              window.location.reload();
            }
          },
        });
      });
    }
  },
});
