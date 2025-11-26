# (C) 2025 Smile (<http://www.smile.fr>)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import models


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'


    def get_currencies(self):
        get_currencies_super = getattr(super(), "get_currencies", None)
        if get_currencies_super:
            res = get_currencies_super()
        else:
            res = {}
            for currency in self.env['res.currency'].search([]):
                res[currency.id] = {
                    'symbol': currency.symbol,
                    'position': currency.position,
                    'digits': [0, currency.display_decimal_places],
                }

        currency_ids = list(res.keys())
        for currency in self.env['res.currency'].browse(currency_ids):
            res[currency.id]['digits'][1] = currency.display_decimal_places
        return res
