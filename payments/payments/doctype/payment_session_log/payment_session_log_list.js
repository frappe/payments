frappe.listview_settings["Payment Session Log"] = {
  hide_name_column: true,
  add_fields: ["status"],
  get_indicator: function (doc) {
    const indicators = {
      Paid: ["green"],
      Authorized: ["green"],
      Processing: ["yellow"],
      Created: ["blue"],
      Started: ["blue"],
      Initiated: ["orange"],
      Declined: ["red"],
      Error: ["red"],
      "Error - RefDoc": ["red"],
      Cancelled: ["red"],
    };
    const match = indicators[doc.status];
    if (match) {
      return [__(doc.status), match[0], "status,=," + doc.status];
    }
  },
};
