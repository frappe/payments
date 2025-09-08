# Pesapal Payment Gateway Integration

This document provides comprehensive information about the Pesapal v3 payment gateway integration for ERPNext.

## Overview

Pesapal is a leading payment gateway in East Africa that supports multiple payment methods including mobile money (M-Pesa, MTN Mobile Money, Airtel Money), credit/debit cards, and bank transfers. This integration provides seamless payment processing for both Point of Sale (POS) and webshop functionality in ERPNext.

## Features

- **Multiple Payment Methods**: Supports cards, mobile money, and bank transfers
- **Multi-Currency Support**: KES, UGX, TZS, RWF, ZMW, USD, EUR, GBP
- **Real-time Payment Processing**: Instant payment notifications via IPN
- **Sandbox Testing**: Full sandbox environment for testing
- **POS Integration**: Seamless integration with ERPNext Point of Sale
- **Webshop Integration**: E-commerce payment processing
- **Comprehensive Logging**: Detailed logging and error handling
- **Payment Status Tracking**: Real-time payment status updates

## Supported Currencies

- KES (Kenyan Shilling)
- UGX (Ugandan Shilling)
- TZS (Tanzanian Shilling)
- RWF (Rwandan Franc)
- ZMW (Zambian Kwacha)
- USD (US Dollar)
- EUR (Euro)
- GBP (British Pound)

## Setup Instructions

### 1. Pesapal Account Setup

1. **Create Pesapal Account**:
   - Visit [Pesapal](https://www.pesapal.com) and create a business account
   - Complete the verification process
   - Obtain your Consumer Key and Consumer Secret

2. **Test Credentials**:
   - For testing, download test credentials from [Pesapal Developer Portal](https://developer.pesapal.com)

### 2. ERPNext Configuration

1. **Install the Payments App** (if not already installed):
   ```bash
   bench get-app payments
   bench install-app payments
   ```

2. **Configure Pesapal Settings**:
   - Go to **Setup > Integrations > Pesapal Settings**
   - Enter your Consumer Key and Consumer Secret
   - Enable "Is Sandbox" for testing
   - Set IPN Notification Type (POST recommended)
   - Save the settings

3. **Register IPN URL**:
   - Click "Register IPN URL" button in Pesapal Settings
   - This will automatically register your IPN endpoint with Pesapal

4. **Test Connection**:
   - Click "Test Connection" to verify your credentials

### 3. Payment Gateway Account Setup

1. **Create Payment Gateway Account**:
   - Go to **Accounts > Payment Gateway Account**
   - Create new Payment Gateway Account
   - Set Payment Gateway as "Pesapal"
   - Configure other settings as needed

## Usage

### Point of Sale (POS)

1. **Configure POS Payment Method**:
   - Go to **Retail > POS Payment Method**
   - Create new POS Payment Method
   - Set Type as "Card" and select Pesapal gateway

2. **Process Payment**:
   - In POS interface, select Pesapal payment method
   - Enter payment amount
   - Customer will be redirected to Pesapal payment page
   - Payment status will be updated automatically

### Webshop/E-commerce

1. **Enable in Website Settings**:
   - Go to **Website > Website Settings**
   - Add Pesapal to enabled payment gateways

2. **Customer Payment Flow**:
   - Customer selects Pesapal at checkout
   - Redirected to Pesapal payment page
   - After payment, redirected back to success/failure page
   - Order status updated automatically

### Sales Invoice Payment

1. **Create Payment Request**:
   - From Sales Invoice, click "Create Payment Request"
   - Select Pesapal as payment gateway
   - Send payment link to customer

2. **Customer Payment**:
   - Customer clicks payment link
   - Completes payment on Pesapal
   - Payment Entry created automatically

## API Integration

### Basic Usage

```python
from payments.utils import get_payment_gateway_controller

# Get Pesapal controller
controller = get_payment_gateway_controller("Pesapal")

# Validate currency
controller().validate_transaction_currency("KES")

# Create payment request
payment_details = {
    "amount": 1000,
    "title": "Payment for Invoice INV-001",
    "description": "Payment via Pesapal",
    "reference_doctype": "Sales Invoice",
    "reference_docname": "INV-001",
    "payer_email": "customer@example.com",
    "payer_name": "John Doe",
    "order_id": "INV-001",
    "currency": "KES",
    "payment_gateway": "Pesapal",
}

# Get payment URL
payment_url = controller().get_payment_url(**payment_details)
```

### JavaScript Integration

```javascript
// Create Pesapal payment
let pesapal = new frappe.integration_service.pesapal_gateway();

pesapal.process({
    amount: 1000,
    currency: "KES",
    payer_email: "customer@example.com",
    payer_name: "John Doe",
    reference_doctype: "Sales Invoice",
    reference_docname: "INV-001"
}, 
function(response) {
    // Success callback
    console.log("Payment successful", response);
},
function(error) {
    // Error callback
    console.log("Payment failed", error);
});
```

## Payment Flow

1. **Payment Initiation**:
   - Customer initiates payment
   - ERPNext creates Integration Request
   - Pesapal order request submitted
   - Customer redirected to Pesapal

2. **Payment Processing**:
   - Customer completes payment on Pesapal
   - Pesapal processes payment
   - Payment status determined

3. **Payment Completion**:
   - Pesapal sends IPN notification
   - ERPNext processes callback
   - Payment status updated
   - Reference document updated
   - Payment Entry created (if applicable)

## Webhook/IPN Handling

The integration automatically handles Instant Payment Notifications (IPN) from Pesapal:

- **IPN Endpoint**: `/api/method/payments.payment_gateways.doctype.pesapal_settings.pesapal_settings.handle_ipn`
- **Callback Endpoint**: `/api/method/payments.payment_gateways.doctype.pesapal_settings.pesapal_settings.handle_callback`
- **Automatic Registration**: IPN URL is automatically registered with Pesapal
- **Duplicate Prevention**: Duplicate IPN requests are handled gracefully
- **Error Handling**: Comprehensive error logging and handling

## Payment Status Mapping

| Pesapal Status | ERPNext Status | Description |
|---------------|----------------|-------------|
| COMPLETED     | Completed      | Payment successful |
| FAILED        | Failed         | Payment failed |
| REVERSED      | Cancelled      | Payment reversed/refunded |
| PENDING       | Queued         | Payment pending |
| INVALID       | Failed         | Invalid payment |

## Error Handling

The integration includes comprehensive error handling:

- **API Errors**: Detailed logging of API communication errors
- **Validation Errors**: Input validation and error messages
- **Network Errors**: Retry mechanisms and timeout handling
- **IPN Errors**: Robust IPN processing with error recovery
- **Integration Logs**: All requests logged for debugging

## Testing

### Unit Tests

Run the test suite:

```bash
bench run-tests --app payments --module payments.payment_gateways.doctype.pesapal_settings.test_pesapal_settings
```

### Manual Testing

1. **Sandbox Testing**:
   - Enable sandbox mode in Pesapal Settings
   - Use test credentials
   - Process test payments

2. **Test Scenarios**:
   - Successful payment
   - Failed payment
   - Cancelled payment
   - Network interruption
   - IPN handling

## Troubleshooting

### Common Issues

1. **Invalid Credentials**:
   - Verify Consumer Key and Consumer Secret
   - Check sandbox vs production settings
   - Use "Test Connection" feature

2. **IPN Not Working**:
   - Verify IPN URL is registered
   - Check firewall settings
   - Review IPN logs in Integration Request

3. **Payment Not Updating**:
   - Check Integration Request logs
   - Verify webhook endpoints are accessible
   - Review error logs

### Debug Mode

Enable debug logging:

```python
# In site_config.json
{
    "developer_mode": 1,
    "log_level": "DEBUG"
}
```

### Log Files

Check these log files for debugging:
- `logs/web.log` - Web request logs
- `logs/worker.log` - Background job logs
- `logs/error.log` - Error logs

## Security Considerations

- **Credentials**: Store Consumer Key and Secret securely
- **HTTPS**: Always use HTTPS for production
- **IPN Validation**: IPN requests are validated
- **Error Logging**: Sensitive data is not logged
- **Access Control**: Proper permission checks

## Support

For support and issues:

1. **Documentation**: Check this README and code comments
2. **Logs**: Review Integration Request logs
3. **Pesapal Support**: Contact Pesapal for API-related issues
4. **ERPNext Community**: Post in ERPNext community forums

## License

This integration is licensed under the MIT License. See LICENSE file for details.

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Submit a pull request

## Changelog

### Version 1.0.0
- Initial Pesapal v3 integration
- Support for POS and webshop payments
- Comprehensive error handling and logging
- Full test suite
- Documentation
