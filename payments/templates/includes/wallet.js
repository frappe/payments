$(document).ready(function() {

//main method	  


})

window.addEventListener('beforeunload', (event) => {
    cancel_payment();
});



var button = document.querySelector('#proceed');
var form = document.querySelector('#payment-form');
frappe.msgprint(frappe.form_dict);
var data = {{ frappe.form_dict | json }};
var doctype = "{{ reference_doctype }}";
var docname = "{{ reference_docname }}";


$("#proceed").click(function (event) {
    event.preventDefault();
    //$("#proceed"). attr("disabled", true);
    init_payment()
})

$("#cancel").click(function (event) {
    event.preventDefault();
    //$("#proceed"). attr("disabled", true);
    cancel_payment()
})
function init_payment(){
    frappe.call({
        method: "payments.templates.pages.wallet.make_payment",
        freeze: true,
        headers: {
            "X-Requested-With": "XMLHttpRequest"
        },
        args: {
            "data": JSON.stringify(data),
            "reference_doctype": doctype,
            "reference_docname": docname
        },
        callback: function(r) {
           frappe.msgprint(r.message.info)
           if (r.message.info=="Transaction successful"){

           }else {
            frappe.msgprint(r.message.info)

           }
        }
    })
}
function cancel_payment(){
    frappe.call({
        method: "payments.templates.pages.wallet.cancel_payment",
        freeze: true,
        headers: {
            "X-Requested-With": "XMLHttpRequest"
        },
        args: {
            "data": JSON.stringify(data)
        },
        callback: function(r) {
            if (r.message && r.message == "Success") {
                frappe.show_alert({
                    message:__('Payment Cancelled'),
                    indicator:'green'
                }, 5)
                $("#proceed"). attr("disabled", true);
                $("#cancel"). attr("disabled", true);
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