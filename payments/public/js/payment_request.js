frappe.ui.form.on("Payment Request", {
    refresh(frm) {
        if (
            frm.doc.docstatus === 1 &&
            frm.doc.status !== "Paid" &&
            frm.doc.payment_gateway &&
            frm.doc.payment_gateway.startsWith("MoMo-")
        ) {
            frm.add_custom_button(__("Open MoMo Checkout"), function() {
                frappe.call({
                    method: "payments.payment_gateways.doctype.momo_settings.momo_settings.generate_payment_url",
                    args: {
                        payment_request_name: frm.doc.name
                    },
                    callback(r) {
                        if (r.message) {
                            window.open(r.message, "_blank");
                        }
                    }
                });
            }, __("MoMo"));
        }
    }
});
