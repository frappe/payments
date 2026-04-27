// Copyright (c) 2016, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Payment Gateway", {
  onload: function (frm) {
    frm.fields_dict.payment_gateway_account.grid.get_field("payment_account").get_query = (frm, cdt, cdn) => {
      row = locals[cdt][cdn];
      return {
        filters: {
          company: row.company,
        }
      };
    }
  },
});
