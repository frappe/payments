$(document).ready(function() {

    const order_id = new URLSearchParams(window.location.search).get("order_id");

    console.log("ORDER ID (from client URL):", order_id);

    if (!order_id) {
        console.error("Error: Missing order_id");
        return;
    }

    console.log("id:",order_id)

    frappe.call({
        method: "payments.templates.pages.bankmuscat_checkout.get_payment_url",
        freeze: true,
        headers: {
            "X-Requested-With": "XMLHttpRequest"
        },
        args: {
            data: {
                "order_id": order_id
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
            if (r && r.message && r.message.msg && r.message.url) {
                const urlWithMsg = `${r.message.url}?msg=${encodeURIComponent(r.message.msg)}`;
                window.location.href = urlWithMsg;
            }

        },
        error: function(err) {
            console.error("API call failed:", err);
        }
    });
});
