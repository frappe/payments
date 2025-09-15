$(document).ready(function() {


    var data = {{ frappe.form_dict | json }}; // Get data from backend

    if (!data || !data.order_id) {
        console.error("Error: Missing order_id");
        return;
    }

    frappe.call({
        method: "payments.templates.pages.bankmuscat_checkout.get_payment_url",
        freeze: true,
        headers: {
            "X-Requested-With": "XMLHttpRequest"
        },
        args: {
            data: {
                "order_id": data.order_id
            }
        },
        callback: function(r) {
            if (r && r.message && r.message.payment_url) {
				const tempDiv = document.createElement('div');
        		tempDiv.innerHTML = r.message.payment_url;

				const form = tempDiv.querySelector('form');
				if (form) {
					document.body.appendChild(form);
					form.submit();
				} else {
					console.error("Error: No form found in payment_url");
				}
            }
        },
        error: function(err) {
            console.error("API call failed:", err);
        }
    });
});
