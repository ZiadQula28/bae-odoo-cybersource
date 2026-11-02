{
    'name': "CyberSource Payment Gateway | CyberSource Payment Provider, Visa Acceptance, "
            "3D Secure, Saved Cards",
    'version': '19.0.1.0.0',
    'category': 'Accounting/Payment Providers',
    'sequence': 350,
    'summary': "CyberSource payment provider for Odoo eCommerce, portal and invoices "
               "(cybersource, cybersource payment, cybersource payment acquirer, cybersource "
               "integration, cybersource connector, cyber source, odoo cybersource, cybersource "
               "odoo, visa cybersource, visa payment gateway): card payments with CyberSource "
               "Microform (card data never touches Odoo, an alternative to the secure acceptance "
               "hosted checkout), cybersource 3d secure with payer authentication, authorize and "
               "capture or sale, void, cybersource refund and partial refund, saved cards with "
               "the cybersource token management service, signed webhooks, sandbox and "
               "production.",
    'description': " ",
    'author': 'Modvanta',
    'maintainer': 'Modvanta',
    'website': '',
    'support': 'modvantaapps@gmail.com',
    'license': 'OPL-1',
    'price': 30,
    'currency': 'USD',
    'depends': ['payment', 'account_payment'],
    'data': [
        'security/ir.model.access.csv',
        'security/mdv_cybersource_security.xml',

        'views/payment_cybersource_templates.xml',
        'views/payment_provider_views.xml',
        'views/payment_transaction_views.xml',
        'views/mdv_cybersource_log_views.xml',

        'data/payment_provider_data.xml',
        'data/ir_cron_data.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'mdv_payment_cybersource/static/src/js/cybersource_microform.js',
            'mdv_payment_cybersource/static/src/interactions/payment_form.js',
            'mdv_payment_cybersource/static/src/scss/cybersource.scss',
        ],
    },
    'images': ['static/description/banner.png'],
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'application': False,
}
