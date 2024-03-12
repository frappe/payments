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
           if (r.message.info=="200 OK"){
            load_success()

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
    success=`<div class="modal-dialog modal-confirm">
    <div class="modal-content">
        <div class="modal-header">
            <div class="icon-box">
                <i class="material-icons"></i>
            </div>				
            <h4 class="modal-title w-100">Awesome!</h4>	
        </div>
        <div class="modal-body">
            <p class="text-center">Your New balance is {{pred_balance}}</p></br>
            <div class="py-4 d-flex justify-content-center">
            <h6><a href="/all-products">Return to website</a></h6>
          </div>
        </div>
        <div class="modal-footer">
       
        </div>
    </div>
    </div>`;
    $('#main_section').empty();
    $( "#main_section" ).append(success);

}

function load_fail(){

}