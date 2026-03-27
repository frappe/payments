from . import __version__ as app_version

app_name = "payments"
app_title = "Payments"
app_publisher = "Frappe Technologies"
app_description = "Payments app for frappe"
app_email = "hello@frappe.io"
app_license = "MIT"

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/pay/css/pay.css"
# app_include_js = "/assets/pay/js/pay.js"

# include js, css files in header of web template
# web_include_css = "/assets/pay/css/pay.css"
# web_include_js = "/assets/pay/js/pay.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "pay/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {"Payment Request": "public/js/payment_request.js"}

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
#     "Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
#     "methods": "pay.utils.jinja_methods",
#     "filters": "pay.utils.jinja_filters"
# }

# Installation
# ------------

before_install = "payments.utils.before_install"
after_install = "payments.utils.make_custom_fields"

# Uninstallation
# ------------

before_uninstall = "payments.utils.delete_custom_fields"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config
# notification_config = "pay.notifications.get_notification_config"

# Permissions
# -----------
# permission_query_conditions = {
#     "Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
#     "Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------

extend_doctype_class = {"Web Form": "payments.overrides.payment_webform.PaymentWebForm"}

# Document Events
# ---------------

doc_events = {
    "Sales Invoice": {
        "on_submit": "payments.payment_gateways.doctype.momo_settings.momo_settings.on_sales_invoice_submit",
    }
}

# Scheduled Tasks
# ---------------

scheduler_events = {
    "all": [
        "payments.payment_gateways.doctype.razorpay_settings.razorpay_settings.capture_payment",
    ],
    "hourly": [
        "payments.payment_gateways.doctype.momo_settings.momo_settings.poll_pending_transactions",
    ],
}

# Testing
# -------

before_tests = "erpnext.setup.utils.before_tests"  # To setup company and accounts

# Overriding Methods
# ------------------------------

override_whitelisted_methods = {
    "frappe.website.doctype.web_form.web_form.accept": "payments.overrides.payment_webform.accept"
}

# User Data Protection
# --------------------

# user_data_fields = [
#     {
#         "doctype": "{doctype_1}",
#         "filter_by": "{filter_by}",
#         "redact_fields": ["{field_1}", "{field_2}"],
#         "partial": 1,
#     }
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
#     "pay.auth.validate"
# ]
