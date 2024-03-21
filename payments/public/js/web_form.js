frappe.ui.form.on("Web Form", {
    set_fields(frm) {
        let doc = frm.doc;

        let update_options = (options) => {
            [frm.fields_dict.web_form_fields.grid, frm.fields_dict.list_columns.grid].forEach(
                (obj) => {
                    obj.update_docfield_property("fieldname", "options", options);
                }
            );
        };

        if (!doc.doc_type) {
            update_options([]);
            frm.set_df_property("amount_field", "options", []);
            frm.set_df_property("payer_name_field", "options", []);
            frm.set_df_property("payer_email_field", "options", []);
            return;
        }

        update_options([`Fetching fields from ${doc.doc_type}...`]);

        get_fields_for_doctype(doc.doc_type).then((fields) => {
            let as_select_option = (df) => ({
                label: df.label,
                value: df.fieldname,
            });
            update_options(fields.map(as_select_option));

            // Amount Field Options
            let currency_fields = fields
                .filter((df) => ["Currency", "Float"].includes(df.fieldtype))
                .map(as_select_option);
            if (!currency_fields.length) {
                currency_fields = [
                    {
                        label: `No currency fields in ${doc.doc_type}`,
                        value: "",
                        disabled: true,
                    },
                ];
            }
            frm.set_df_property("amount_field", "options", currency_fields);

            // Payer Name Field Options
            let payer_name_fields = fields
                .filter((df) => ["Data", "Link"].includes(df.fieldtype))
                .map(as_select_option);
            if (!payer_name_fields.length) {
                payer_name_fields = [
                    {
                        label: `No data or link fields in ${doc.doc_type}`,
                        value: "",
                        disabled: true,
                    },
                ];
            }
            frm.set_df_property("payer_name_field", "options", payer_name_fields);

            // Payer Email Field Options
            let payer_email_fields = fields
                .filter((df) => ["Email"].includes(df.options))
                .map(as_select_option);
            if (!payer_email_fields.length) {
                payer_email_fields = [
                    {
                        label: `No email fields in ${doc.doc_type}`,
                        value: "",
                        disabled: true,
                    },
                ];
            }
            frm.set_df_property("payer_email_field", "options", payer_email_fields);
        });
    }
});
