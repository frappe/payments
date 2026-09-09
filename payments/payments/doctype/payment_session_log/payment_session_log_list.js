frappe.listview_settings["Payment Session Log"] = {
  hide_name_column: true,
  add_fields: ["status", "reconciliation"],
  get_indicator: function (doc) {
    // A captured payment whose bookkeeping failed needs operator attention even
    // though the gateway side succeeded, so it must not read as a plain "Paid".
    // Clicking through filters to the unreconciled worklist.
    if (doc.reconciliation === "Failed") {
      return [
        __("{0} · unreconciled", [__(doc.status)]),
        "red",
        "reconciliation,=,Failed",
      ];
    }
    const indicators = {
      Paid: ["green"],
      Authorized: ["green"],
      Processing: ["yellow"],
      Created: ["blue"],
      Started: ["blue"],
      Initiated: ["orange"],
      "Data Capture": ["orange"],
      Declined: ["red"],
      Error: ["red"],
      Cancelled: ["red"],
      // The gateway answered and we could not act on its answer, so money may be
      // held. Needs an operator, and is never purged by retention.
      Unresolved: ["red"],
    };
    const match = indicators[doc.status];
    if (match) {
      return [__(doc.status), match[0], "status,=," + doc.status];
    }
  },
};
