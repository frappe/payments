from .enums import PaymentMethod, TestMode

_TEST_CREDENTIALS = {
    PaymentMethod.ECOCASH: {
        TestMode.SUCCESS: "0771111111",
        TestMode.DELAYED_SUCCESS: "0772222222",
        TestMode.USER_CANCELLED: "0773333333",
        TestMode.INSUFFICIENT_FUNDS: "0774444444",
    },
    PaymentMethod.VMC: {
        TestMode.SUCCESS: "{11111111-1111-1111-1111-111111111111}",
        TestMode.DELAYED_SUCCESS: "{22222222-2222-2222-2222-222222222222}",
        TestMode.USER_CANCELLED: "{33333333-3333-3333-3333-333333333333}",
        TestMode.INSUFFICIENT_FUNDS: "{44444444-4444-4444-4444-444444444444}",
    },
    PaymentMethod.ZIMSWITCH: {
        TestMode.SUCCESS: "11111111111111111111111111111111",
        TestMode.DELAYED_SUCCESS:  "22222222222222222222222222222222",
        TestMode.USER_CANCELLED: "33333333333333333333333333333333",
        TestMode.INSUFFICIENT_FUNDS:"44444444444444444444444444444444"
    }
}

# Synonyms
_TEST_CREDENTIALS[PaymentMethod.INNBUCKS] = _TEST_CREDENTIALS[PaymentMethod.ECOCASH]
_TEST_CREDENTIALS[PaymentMethod.OMARI] = _TEST_CREDENTIALS[PaymentMethod.ECOCASH]
_TEST_CREDENTIALS[PaymentMethod.ONEMONEY] = _TEST_CREDENTIALS[PaymentMethod.ECOCASH]

def resolve_test_credential(method: PaymentMethod, mode: TestMode, original_input: str) -> str:
    if mode == TestMode.NONE:
        return original_input
    
    creds = _TEST_CREDENTIALS.get(method, {})
    return creds.get(mode, original_input)