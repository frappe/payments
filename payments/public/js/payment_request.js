frappe.ui.form.on("Payment Request", {
  refresh(frm) {
    const isInward = frm.doc.payment_request_type === "Inward";
    const isInitiated = frm.doc.status === "Initiated";
    const gatewayAccount = frm.doc.payment_gateway_account || "";
    const firstWord = gatewayAccount.split("-")[0].trim().toLowerCase();

    const isBankMuscat = firstWord === "bankmuscat";

    frappe.call({
      method: "payments.templates.pages.bankmuscat_checkout.set_payment_entry",
      args:{
        "doc_name":frm.doc.name
      },
      callback: function(r){
        frm.refresh_field("payment_entry");
      }
    })
                  
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

    if(frm.doc.status === "Paid" && !frm.doc.payment_entry && frm.doc.response_command){
      frappe.call({
        method:"payments.templates.pages.bankmuscat_checkout.check_roles",
        args:{},
        callback:function(r){
          if(r.message){
            frm.add_custom_button(__("Create Payment Entry"), function () {
              frappe.call({
                method: "erpnext.accounts.doctype.payment_request.payment_request.make_payment_entry",
                args: { docname: frm.doc.name },
                freeze: true,
                callback: function (r) {
                  if (!r.exc) {
                    var doc = frappe.model.sync(r.message);
                    frappe.set_route("Form", r.message.doctype, r.message.name);
                  }
                },
              });
            }).addClass("btn-primary");
          }
        }
      }) 
    }
  },
});
