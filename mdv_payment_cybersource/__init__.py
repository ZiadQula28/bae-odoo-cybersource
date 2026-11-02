from . import controllers
from . import models

from odoo.addons.payment import setup_provider, reset_payment_provider

from . import const


def post_init_hook(env):
    setup_provider(env, const.PROVIDER_CODE)


def uninstall_hook(env):
    reset_payment_provider(env, const.PROVIDER_CODE)
