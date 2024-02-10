+$(document).ready(function() {
	var form = document.querySelector('#payment-form');
	var data = {{ frappe.form_dict | json }};
	var doctype = "{{ reference_doctype }}"
	var docname = "{{ reference_docname }}"
	document.getElementById("submit").innerHTML = "{{_("Loading...")}}";
	document.getElementById("status").value = "{{_("Loading...")}}";
	frappe.call({
			method: "payments.templates.pages.mollie_checkout.make_payment",
			freeze: true,
			headers: {
				"X-Requested-With": "XMLHttpRequest"
			},
			args: {
				"data": JSON.stringify(data),
				"reference_doctype": doctype,
				"reference_docname": docname,
			},
			callback: function(r){
				payment = r.message
				document.getElementById("status").value = payment.status;
				if (payment.paymentUrl == "Unavailable") {
					document.getElementById("submit").innerHTML = "{{_("Ready")}}";
				}
				else {
					document.getElementById("submit").innerHTML = "{{_('Pay')}} {{amount}}";
				}
			}
		})

	form.addEventListener('submit', e => {
		e.preventDefault();
		if (payment.paymentUrl == "Unavailable") {
			window.location.href = payment.redirect_to
		}
		else {
			window.location.href = payment.paymentUrl
		}
	})
})
