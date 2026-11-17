// Copyright (c) 2016, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Payment Button", {
  gateway_settings: function (frm) {
    const val = frm.get_field("gateway_settings").value;
    if (val) {
      frappe.call(
        "payments.controllers.frontend_defaults",
        { doctype: val },
        (r) => {
          if (r.message) {
            frm.set_value("gateway_css", r.message.gateway_css);
            frm.set_value("gateway_js", r.message.gateway_js);
            frm.set_value("gateway_wrapper", r.message.gateway_wrapper);
          }
        }
      );
    }
  },
});
