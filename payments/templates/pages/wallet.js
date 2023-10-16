$(document).ready(function() {

	var button = document.querySelector('#submit-button');
	var form = document.querySelector('#payment-form');
	var data = {{ frappe.form_dict | json }};
	var doctype = "{{ reference_doctype }}"
	var docname = "{{ reference_docname }}"

//main method	  


})


$("#proceed").click(function (event) {
    event.preventDefault();
    $("#register"). attr("disabled", true);
    args=validate_value();
    register_user(args);

})

function init_payment(){
    frappe.call({
        method: "payments.templates.pages.wallet.",
        freeze: true,
        headers: {
            "X-Requested-With": "XMLHttpRequest"
        },
        args: {
            "payload_nonce": payload.nonce,
            "data": JSON.stringify(data),
            "reference_doctype": doctype,
            "reference_docname": docname
        },
        callback: function(r) {
            if (r.message && r.message.status == "Completed") {
                load_success()
            } else if (r.message && r.message.status == "Error") {
                load_fail()
            }
        }
    })
}

function load_success(){

}

function load_fail(){

}