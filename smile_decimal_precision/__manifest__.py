# (C) 2025 Smile (<http://www.smile.fr>)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

{
    "name": "Display Decimal Precision",
    "version": "19.0.0.0.0",
    "depends": ["base"],
    "author": "Smile",
    "license": "AGPL-3",
    "description": """
This module allows to distinguish computation digits
and display digits in decimal precision.
""",
    "website": "",
    "category": "Hidden/Dependency",
    "sequence": 32,
    "data": [
        "views/decimal_precision_view.xml",
        "views/res_currency_view.xml",
    ],
    "auto_install": True,
    "installable": True,
    "application": False,
}
