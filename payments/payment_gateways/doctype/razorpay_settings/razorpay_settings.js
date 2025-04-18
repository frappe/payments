// Copyright (c) 2016, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Razorpay Settings", {
  refresh: function (frm) {
    frm.add_custom_button(__("Clear"), function () {
      frm.call({
        doc: frm.doc,
        method: "clear",
        callback: function (r) {
          frm.refresh();
        },
      });
    });
  },
});
