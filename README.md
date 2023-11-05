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
    
## Supported Payment Gateways

- Razorpay
- Stripe
- Braintree
- Paypal
- PayTM
- Mpesa
- GoCardless

## Architecture

see [Architecture Document](./ARCHITECTURE.md)


## License
MIT ([license.txt](license.txt))
