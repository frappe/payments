frappe.ready(function () {
  // Focus the first button
  // document.getElementById("primary-button").focus();

  // Get all button elements
  const buttons = Array.from(document.getElementsByClassName("btn-pay"));

  // Get the error section
  // const errors = document.getElementById("errors");

  // Get the payment session log name
  const urlParams = new URLSearchParams(window.location.search);
  const pslName = urlParams.get("s");

  // Loop through each button and add the onclick event listener
  buttons.forEach((button) => {
    // Get the data-button attribute value
    const buttonData = button.getAttribute("data-button");

    button.addEventListener("click", () => {
      // Make the Frappe call
      frappe.call({
        method:
          "payments.payments.doctype.payment_session_log.payment_session_log.select_button",
        args: {
          pslName: pslName,
          buttonName: buttonData,
        },
        // No error_msg option here: frappe.call has no such option (grep the
        // frappe JS bundle), so it silently did nothing. Every refusal branch of
        // select_button returns None and puts its reason in message_log, which
        // frappe already renders as a msgprint for the payer.
        callback: (r) => {
          // r.message is undefined on every refusal branch, and this used to
          // dereference it — throwing a TypeError in the payer's browser instead
          // of showing them the reason.
          if (r.message && r.message.reload) {
            window.location.reload();
          }
        },
      });
    });
  });
});

$(document).on("payment-submitted", function (e) {
  $("div#button-section").hide();
});

$(document).on("payment-processed", function (e, r) {
  if (r.message.status_changed_to) {
    const status = r.message.status_changed_to;
    const color = r.message.indicator_color;
    const pill = $("#status");
    pill.text(status);
    pill.addClass(color);
    $("#status-wrapper").toggle(true);
    const indicator = $("#refdoc-indicator");
    indicator.removeClass(function (_, className) {
      const matches = className.match(/blue|red|yellow|gray/g) || [];
      return matches.join(" ");
    });
    indicator.addClass(color);
  }
  if (r.message.message) {
    $("#message").text(r.message.message).toggle(true);
    $("#message-wrapper").toggle(true);
  }
  if (r.message.action) {
    const cta = $("#action-processed");
    cta.text(r.message.action.label);
    const href = r.message.action.href || "";
    const isSafeHref = /^(\/|https?:\/\/)/.test(href);
    cta.attr("href", isSafeHref ? href : "/");
    cta.toggle(true);
    cta.focus();
  }
});
