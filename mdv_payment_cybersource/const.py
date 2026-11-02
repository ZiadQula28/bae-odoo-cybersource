"""Constants of the CyberSource REST API (developer.cybersource.com/api-reference-assets)."""

from odoo.tools import LazyTranslate

_lt = LazyTranslate(__name__)

PROVIDER_CODE = 'mdv_cybersource'

# Run environments of the REST API.
HOSTS = {
    'test': 'apitest.cybersource.com',
    'prod': 'api.cybersource.com',
}

# The Microform client library comes from these origins only (the capture context names it).
MICROFORM_ORIGINS = ('https://testflex.cybersource.com', 'https://flex.cybersource.com')

# Payment method codes of Odoo -> network names of Microform (`allowedCardNetworks`).
NETWORKS = {
    'visa': 'VISA',
    'mastercard': 'MASTERCARD',
    'amex': 'AMEX',
    'discover': 'DISCOVER',
    'diners': 'DINERSCLUB',
    'jcb': 'JCB',
    'maestro': 'MAESTRO',
    'unionpay': 'CUP',
    'cartes_bancaires': 'CARTESBANCAIRES',
    'elo': 'ELO',
}
DEFAULT_NETWORKS = ['VISA', 'MASTERCARD', 'AMEX', 'DISCOVER', 'DINERSCLUB', 'JCB']

# `paymentInformation.card.type` codes -> Odoo payment method codes.
CARD_TYPES = {
    '001': 'visa',
    '002': 'mastercard',
    '003': 'amex',
    '004': 'discover',
    '005': 'diners',
    '007': 'jcb',
    '024': 'maestro',
    '036': 'cartes_bancaires',
    '042': 'maestro',
    '054': 'elo',
    '062': 'unionpay',
}
CARD_TYPE_LABELS = {
    '001': 'Visa', '002': 'Mastercard', '003': 'American Express', '004': 'Discover',
    '005': 'Diners Club', '007': 'JCB', '024': 'Maestro', '036': 'Cartes Bancaires',
    '042': 'Maestro', '054': 'Elo', '062': 'UnionPay',
}
# Microform `detectedCardTypes` / network name -> card type code (needed by Payer Auth setup).
NETWORK_TO_TYPE = {
    'VISA': '001', 'MASTERCARD': '002', 'AMEX': '003', 'DISCOVER': '004', 'DINERSCLUB': '005',
    'JCB': '007', 'MAESTRO': '042', 'CARTESBANCAIRES': '036', 'ELO': '054', 'CUP': '062',
}

DEFAULT_PAYMENT_METHOD_CODES = {'card', 'visa', 'mastercard', 'amex', 'discover'}

# `status` of the payment response (POST /pts/v2/payments).
STATUS_AUTHORIZED = {'AUTHORIZED'}
STATUS_REVIEW = {'AUTHORIZED_PENDING_REVIEW', 'PENDING_REVIEW'}
STATUS_DECLINED = {'DECLINED', 'INVALID_REQUEST', 'AUTHORIZED_RISK_DECLINED', 'PARTIAL_AUTHORIZED',
                   'SERVER_ERROR'}
STATUS_CHALLENGE = 'PENDING_AUTHENTICATION'
# `status` of follow-on responses: captures and refunds are queued for the settlement batch.
STATUS_FOLLOW_ON_OK = {'PENDING', 'TRANSMITTED', 'SETTLED', 'COMPLETED'}
STATUS_CANCELED = {'REVERSED', 'VOIDED'}

# `errorInformation.reason` -> message for the customer.
DECLINE_MESSAGES = {
    'INSUFFICIENT_FUND': _lt("The card has insufficient funds."),
    'EXPIRED_CARD': _lt("The card has expired."),
    'INVALID_CVN': _lt("The security code (CVV) is not correct."),
    'CV_FAILED': _lt("The security code (CVV) is not correct."),
    'AVS_FAILED': _lt("The billing address does not match the address of the card."),
    'STOLEN_LOST_CARD': _lt("The card was declined by the bank."),
    'CARD_REFUSED': _lt("The card was declined by the bank."),
    'PROCESSOR_DECLINED': _lt("The card was declined by the bank."),
    'GENERAL_DECLINE': _lt("The card was declined by the bank."),
    'INVALID_ACCOUNT': _lt("The card number is not valid."),
    'UNAUTHORIZED_CARD': _lt("This card cannot be used for this purchase."),
    'CREDIT_LIMIT_REACHED': _lt("The credit limit of the card is reached."),
    'CONSUMER_AUTHENTICATION_FAILED': _lt("The 3-D Secure verification with your bank failed."),
    'CONSUMER_AUTHENTICATION_REQUIRED': _lt("The bank asks for a 3-D Secure verification."),
    'DECISION_PROFILE_REJECT': _lt("The payment was refused by the fraud screening."),
    'SCORE_EXCEEDS_THRESHOLD': _lt("The payment was refused by the fraud screening."),
    'DUPLICATE_REQUEST': _lt("This payment was already sent."),
    'MISSING_FIELD': _lt("Some billing details are missing."),
    'INVALID_DATA': _lt("Some payment details are not valid."),
}

# Webhook products whose events update the transactions.
WEBHOOK_PRODUCTS = ('payments', 'decisionManager', 'fraudManagementEssentials')
DEFAULT_WEBHOOK_EVENTS = {
    'payments': [
        'payments.payments.accept', 'payments.payments.reject', 'payments.payments.review',
        'payments.captures.accept', 'payments.captures.reject',
        'payments.refunds.accept', 'payments.refunds.reject',
        'payments.reversals.accept', 'payments.reversals.reject',
        'payments.voids.accept', 'payments.voids.reject',
    ],
}
# A notification signed more than this many seconds before it arrives is refused.
WEBHOOK_TOLERANCE = 5 * 60

# Field names written to the API log with their value hidden.
SECRET_KEYS = {
    'transientTokenJwt', 'number', 'securityCode', 'accessToken', 'cavv', 'xid', 'key',
    'captureContext', 'jwt', 'JWT', 'token',
}
