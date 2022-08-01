# Payments

A payments app for frappe.

## Installation
1. Install [bench & frappe](https://frappeframework.com/docs/v14/user/en/installation).

2. Once setup is complete, add the payments app to your bench by running
    ```
    $ bench get-app payments
    ```
3. Install the payments app on the required site by running
    ```
    $ bench --site <sitename> install-app payments
    ```

## App Structure & Details
App has 2 modules - Payments and Payment Gateways.

Payment Module contains the Payment Gateway DocType which creates links for the payment gateways and Payment Gateways Module contain all the Payment Gateway (Razorpay, Stripe, Braintree, Paypal, PayTM) DocTypes.

App adds custom fields to Web Form for facilitating payments upon installation and removes them upon uninstallation.

All general utils are stored in [utils](payments/utils) directory. The utils are written in [utils.py](payments/utils/utils.py) and then imported into the [`__init__.py`](payments/utils/__init__.py) file for easier importing/namespacing.

[overrides](payments/overrides) directory has all the overrides for overriding standard frappe code. Currently it overrides WebForm DocType controller as well as a WebForm whitelisted method.

[templates](payments/templates) directory has all the payment gateways' custom checkout pages.

#

## License
MIT ([license.txt](license.txt))
