# -*- coding: utf-8 -*-
##############################################################################
#
#    MoviTrack
#    Copyright (C) 2020-TODAY MoviTrack.
#    You can modify it under the terms of the GNU LESSER
#    GENERAL PUBLIC LICENSE (LGPL v3), Version 3.
#
#    It is forbidden to publish, distribute, sublicense, or sell copies
#    of the Software or modified copies of the Software.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU LESSER GENERAL PUBLIC LICENSE (LGPL v3) for more details.
#
#    You should have received a copy of the GNU LESSER GENERAL PUBLIC LICENSE
#    GENERAL PUBLIC LICENSE (LGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

import json

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class InvoiceServiceTypeDetail(models.Model):
    _name = 'invoice.service.type.detail'
    _description = "Invoice Service Type Detail"

    name = fields.Char()
    code = fields.Char(size=2)
    parent_code = fields.Char()

    _sql_constraints = [
        ('code_unique', 'unique(code)', _('Code must be unique')),
    ]


class AccountInvoice(models.Model):
    _inherit = 'account.move'

    def _get_invoice_payment_widget(self):
        j = json.loads(self.invoice_payments_widget)
        return j['content'] if j else []

    def _compute_invoice_payment_date(self):
        for inv in self:
            if inv.state == 'posted':
                dates = [
                    payment['date'] for payment in inv._get_reconciled_info_JSON_values()
                ]
                if dates:
                    max_date = max(dates)
                    date_invoice = inv.invoice_date
                    inv.payment_date = max_date if max_date >= date_invoice \
                        else date_invoice

    # TODO Adaptar funcionalidad de busqueda de linea de impuestos
    @api.model
    @api.constrains('l10n_latam_tax_ids')
    def _check_isr_tax(self):
        """Restrict one ISR tax per invoice"""
        for inv in self:
            line = [
                tax_line.tax_id.purchase_tax_type
                for tax_line in inv.l10n_latam_tax_ids
                if tax_line.tax_id.purchase_tax_type in ['isr', 'ritbis']
            ]
            if len(line) != len(set(line)):
                raise ValidationError(_('An invoice cannot have multiple'
                                        'withholding taxes.'))

    def _convert_to_local_currency(self, amount):
        sign = -1 if self.move_type in ['in_refund', 'out_refund'] else 1
        amount = self.currency_id._convert(
            amount, self.company_id.currency_id, self.company_id, self.date
        )
        return amount * sign

    # TODO Adaptar funcionalidad de busqueda de linea de impuestos
    def _get_tax_line_ids(self):
        return self.l10n_latam_tax_ids

    @api.model
    @api.depends('l10n_latam_tax_ids', 'line_ids.tax_ids', 'state')
    def _compute_taxes_fields(self):
        """Compute invoice common taxes fields"""
        for inv in self:

            tax_line_ids = inv._get_tax_line_ids()

            if inv.state != 'draft':
                # Monto Impuesto Selectivo al Consumo
                inv.selective_tax = inv._convert_to_local_currency(
                    sum(
                        tax_line_ids.filtered(
                            lambda tax: tax.tax_line_id.tax_group_id.name == 'ISC')
                        .mapped('debit')))

                # Monto Otros Impuestos/Tasas
                inv.other_taxes = inv._convert_to_local_currency(
                    sum(
                        tax_line_ids.filtered(
                            lambda tax: tax.tax_line_id.tax_group_id.name ==
                            "Otros Impuestos").mapped('debit')))

                # Monto Propina Legal
                inv.legal_tip = inv._convert_to_local_currency(
                    sum(
                        tax_line_ids.filtered(
                            lambda tax: tax.tax_line_id.tax_group_id.name ==
                            'Propina').mapped('debit')))

                # ITBIS sujeto a proporcionalidad
                inv.proportionality_tax = inv._convert_to_local_currency(
                    sum(
                        tax_line_ids.filtered(
                            lambda tax: tax.account_id.account_fiscal_type in
                            ['A29', 'A30']).mapped('debit')))

                # ITBIS llevado al Costo
                inv.cost_itbis = inv._convert_to_local_currency(
                    sum(
                        tax_line_ids.filtered(
                            lambda tax: tax.account_id.account_fiscal_type ==
                            'A51').mapped('debit')))

                if inv.move_type == 'out_invoice' and any([
                    inv.third_withheld_itbis,
                    inv.third_income_withholding
                        ]):
                    # Fecha Pago
                    inv._compute_invoice_payment_date()

                if inv.move_type == 'in_invoice' and any([
                    inv.withholded_itbis,
                    inv.income_withholding
                        ]):
                    # Fecha Pago
                    inv._compute_invoice_payment_date()

    @api.model
    @api.depends('invoice_line_ids', 'invoice_line_ids.product_id', 'state')
    def _compute_amount_fields(self):
        """Compute Purchase amount by product type"""
        for inv in self:
            if inv.move_type in [
                'in_invoice', 'in_refund'
                    ] and inv.state != 'draft':
                service_amount = 0
                good_amount = 0

                for line in inv.invoice_line_ids:

                    # Monto calculado en bienes
                    if line.product_id.type in ['product', 'consu']:
                        good_amount += line.price_subtotal

                    # Si la linea no tiene un producto
                    elif not line.product_id:
                        service_amount += line.price_subtotal
                        continue

                    # Monto calculado en servicio
                    else:
                        service_amount += line.price_subtotal

                inv.service_total_amount = inv._convert_to_local_currency(
                    service_amount)
                inv.good_total_amount = inv._convert_to_local_currency(
                    good_amount)

    # TODO Adaptar funcionalidad de busqueda de linea de impuestos
    @api.model
    @api.depends('l10n_latam_tax_ids', 'state', 'move_type')
    def _compute_isr_withholding_type(self):
        """Compute ISR Withholding Type
        Keyword / Values:
        01 -- Alquileres
        02 -- Honorarios por Servicios
        03 -- Otras Rentas
        04 -- Rentas Presuntas
        05 -- Intereses Pagados a Personas Jurídicas
        06 -- Intereses Pagados a Personas Físicas
        07 -- Retención por Proveedores del Estado
        08 -- Juegos Telefónicos
        """
        for inv in self.filtered(
                lambda i: i.move_type == "in_invoice" and i.state == "paid"):

            tax_l_id = inv.l10n_latam_tax_ids.filtered(
                lambda t: t.tax_line_id.purchase_tax_type == "isr") # TODO Revisar esta parte de tax_group_id hacer una relacion con el campo isr_retention_tipe y purchase_tax_type a account.accoun
            if tax_l_id:  # invoice tax lines use case
                inv.isr_withholding_type = tax_l_id[0].tax_line_id.purchase_tax_type
            else:  # in payment/journal entry use case
                aml_ids = self.env["account.move"].browse(
                    p["move_id"] for p in inv._get_invoice_payment_widget()
                ).mapped("line_ids").filtered(
                    lambda aml: aml.account_id.isr_retention_type)
                if aml_ids:
                    inv.isr_withholding_type = aml_ids[0].account_id.isr_retention_type

    def _get_payment_string(self):
        """Compute Vendor Bills payment method string

        Keyword / Values:
        cash        -- Efectivo
        bank        -- Cheques / Transferencias / Depósitos
        card        -- Tarjeta Crédito / Débito
        credit      -- Compra a Crédito
        swap        -- Permuta
        credit_note -- Notas de Crédito
        mixed       -- Mixto
        """
        payments = []
        p_string = ""

        for payment in self._get_invoice_payment_widget():
            payment_id = self.env['account.payment'].browse(
                payment.get('account_payment_id'))
            move_id = False
            if payment_id:
                if payment_id.journal_id.type in ['cash', 'bank']:
                    p_string = payment_id.journal_id.payment_form

            if not payment_id:
                move_id = self.env['account.move'].browse(
                    payment.get('move_id'))
                if move_id:
                    p_string = 'swap'

            # If invoice is paid, but the payment doesn't come from
            # a journal, assume it is a credit note
            payment = p_string if payment_id or move_id else 'credit_note'
            payments.append(payment)

        methods = {p for p in payments}
        if len(methods) == 1:
            return list(methods)[0]
        elif len(methods) > 1:
            return 'mixed'

    @api.model
    @api.depends('state')
    def _compute_in_invoice_payment_form(self):
        for inv in self:
            if inv.state == 'paid':
                payment_dict = {'cash': '01', 'bank': '02', 'card': '03',
                                'credit': '04', 'swap': '05',
                                'credit_note': '06', 'mixed': '07'}
                inv.payment_form = payment_dict.get(inv._get_payment_string())
            else:
                inv.payment_form = '04'

    # TODO Adaptar funcionalidad de busqueda de linea de impuestos
    @api.model
    @api.depends('l10n_latam_tax_ids', 'l10n_latam_tax_ids.credit', 'l10n_latam_tax_ids.debit', 'state')
    def _compute_invoiced_itbis(self):
        """Compute invoice invoiced_itbis taking into account the currency"""
        for inv in self:
            if inv.state != 'draft':
                amount = 0
                # itbis_taxes = ['ITBIS', 'ITBIS 18%']
                for tax in inv._get_tax_line_ids():
                    # print(tax.tax_line_id.purchase_tax_type)
                    # print(itbis_taxes)
                    # if tax.tax_line_id.purchase_tax_type in itbis_taxes and \
                    #         tax.tax_line_id.purchase_tax_type != 'ritbis':
                    if inv.move_type in ['out_invoice', 'out_refund']:
                        amount += tax.credit
                    elif inv.move_type in ['in_invoice', 'in_refund']:
                        amount += tax.debit
                    inv.invoiced_itbis = inv._convert_to_local_currency(amount)

    def _get_payment_move_iterator(self, payment, inv_type, witheld_type):
        payment_id = self.env['account.payment'].browse(
            payment.get('account_payment_id'))
        if payment_id:
            if inv_type == 'out_invoice':
                return [
                    move_line.debit
                    for move_line in payment_id.move_line_ids
                    if move_line.account_id.account_fiscal_type in witheld_type
                ]
            else:
                return [
                    move_line.credit
                    for move_line in payment_id.move_line_ids
                    if move_line.account_id.account_fiscal_type in witheld_type
                ]
        else:
            move_id = self.env['account.move'].browse(payment.get('move_id'))
            if move_id:
                if inv_type == 'out_invoice':
                    return [
                        move_line.debit
                        for move_line in move_id.line_ids
                        if move_line.account_id.account_fiscal_type in
                        witheld_type
                    ]
                else:
                    return [
                        move_line.credit
                        for move_line in move_id.line_ids
                        if move_line.account_id.account_fiscal_type in
                        witheld_type
                    ]

    # TODO Adaptar funcionalidad de busqueda de linea de impuestos

    # @api.model
    @api.depends('state', 'move_type', 'payment_state', 'payment_id')
    def _compute_withheld_taxes(self):
        for inv in self:
            if inv.state == 'posted':
                inv.third_withheld_itbis = 0
                inv.third_income_withholding = 0
                withholding_amounts_dict = {"A34": 0, "A36": 0, "ISR": 0, "A38": 0}

                if inv.move_type == 'in_invoice':
                    tax_line_ids = inv._get_tax_line_ids()

                    # Monto ITBIS Retenido por impuesto
                    inv.withholded_itbis = abs(
                        inv._convert_to_local_currency(sum(tax_line_ids.filtered(
                        lambda tax: tax.tax_line_id.purchase_tax_type == 'ritbis' # TODO revisar matcheo campo tax_group_id con el anterior de nfc_manager purchase_tax_type
                        ).mapped('debit'))))

                    # Monto Retención Renta por impuesto
                    inv.income_withholding = abs(
                        inv._convert_to_local_currency(sum(tax_line_ids.filtered(
                        lambda tax: tax.tax_line_id.purchase_tax_type == 'isr' # TODO revisar matcheo campo tax_group_id con el anterior de nfc_manager purchase_tax_type
                        ).mapped('debit'))))

                move_ids = [p["move_id"] for p in inv._get_invoice_payment_widget()]
                aml_ids = self.env["account.move"].browse(move_ids).mapped(
                    "line_ids").filtered(lambda aml: aml.account_id.account_fiscal_type)
                if aml_ids:
                    for aml in aml_ids:
                        fiscal_type = aml.account_id.account_fiscal_type
                        print(fiscal_type)
                        if fiscal_type in withholding_amounts_dict:
                            withholding_amounts_dict[fiscal_type] += aml.debit \
                                if inv.move_type == "out_invoice" else aml.credit

                    withheld_itbis = sum(v for k, v in withholding_amounts_dict.items()
                                         if k in ("A34", "A36"))
                    withheld_isr = sum(v for k, v in withholding_amounts_dict.items()
                                       if k in ("ISR", "A38"))

                    if inv.move_type == 'out_invoice':
                        inv.third_withheld_itbis = withheld_itbis
                        inv.third_income_withholding = withheld_isr

                    elif inv.move_type == 'in_invoice':
                        inv.withholded_itbis = withheld_itbis
                        inv.income_withholding = withheld_isr
    # TODO revisar funcion
    @api.model
    @api.depends('invoiced_itbis', 'cost_itbis', 'state')
    def _compute_advance_itbis(self):
        for inv in self:
            if inv.state != 'draft':
                inv.advance_itbis = inv.invoiced_itbis - inv.cost_itbis
    # TODO Evaluar funcionalidad
    @api.model
    @api.depends('l10n_latam_document_type_id') # TODO Evaluar campo Purchase_type que no existe aqui, si en el nfc anterior para tomar funcionalidad
    def _compute_is_exterior(self):
        for inv in self:
            inv.is_exterior = True if inv.l10n_latam_document_type_id.l10n_do_ncf_type == \
                'exterior' else False

    @api.onchange('service_type')
    def onchange_service_type(self):
        self.service_type_detail = False
        return {
            'domain': {
                'service_type_detail': [
                    ('parent_code', '=', self.service_type)
                    ]
            }
        }

    @api.onchange('journal_id')
    def ext_onchange_journal_id(self):
        self.service_type = False
        self.service_type_detail = False

    # ISR Percibido       --> Este campo se va con 12 espacios en 0 para el 606
    # ITBIS Percibido     --> Este campo se va con 12 espacios en 0 para el 606
    # TODO Reparar funciones luego habilitar los siguientes campos
    payment_date = fields.Date(compute='_compute_taxes_fields', store=True)
    service_total_amount = fields.Monetary(
        compute='_compute_amount_fields',
        store=True,
        currency_field='company_currency_id')
    good_total_amount = fields.Monetary(compute='_compute_amount_fields',
                                        store=True,
                                        currency_field='company_currency_id')
    invoiced_itbis = fields.Monetary(compute='_compute_invoiced_itbis',
                                     store=True,
                                     currency_field='company_currency_id')
    withholded_itbis = fields.Monetary(compute='_compute_withheld_taxes',
                                       store=True,
                                       currency_field='company_currency_id')
    proportionality_tax = fields.Monetary(compute='_compute_taxes_fields',
                                          store=True,
                                          currency_field='company_currency_id')
    cost_itbis = fields.Monetary(compute='_compute_taxes_fields',
                                 store=True,
                                 currency_field='company_currency_id')
    advance_itbis = fields.Monetary(compute='_compute_advance_itbis',
                                    store=True,
                                    currency_field='company_currency_id')
    isr_withholding_type = fields.Char(compute='_compute_isr_withholding_type',
                                       store=True,
                                       size=2)
    income_withholding = fields.Monetary(compute='_compute_withheld_taxes',
                                         store=True,
                                         currency_field='company_currency_id')
    selective_tax = fields.Monetary(compute='_compute_taxes_fields',
                                    store=True,
                                    currency_field='company_currency_id')
    other_taxes = fields.Monetary(compute='_compute_taxes_fields',
                                  store=True,
                                  currency_field='company_currency_id')
    legal_tip = fields.Monetary(compute='_compute_taxes_fields',
                                store=True,
                                currency_field='company_currency_id')
    payment_form = fields.Selection([('01', 'Cash'),
                                     ('02', 'Check / Transfer / Deposit'),
                                     ('03', 'Credit Card / Debit Card'),
                                     ('04', 'Credit'), ('05', 'Swap'),
                                     ('06', 'Credit Note'), ('07', 'Mixed')],
                                    compute='_compute_in_invoice_payment_form',
                                    store=True)
    # TODO Reparar funciones luego habilitar los siguientes campos
    third_withheld_itbis = fields.Monetary(
        compute='_compute_withheld_taxes',
        store=True,
        currency_field='company_currency_id')
    third_income_withholding = fields.Monetary(
        compute='_compute_withheld_taxes',
        store=True,
        currency_field='company_currency_id')
    is_exterior = fields.Boolean(compute='_compute_is_exterior', store=True)
    service_type = fields.Selection([
        ('01', 'Gastos de Personal'),
        ('02', 'Gastos por Trabajos, Suministros y Servicios'),
        ('03', 'Arrendamientos'), ('04', 'Gastos de Activos Fijos'),
        ('05', 'Gastos de Representación'), ('06', 'Gastos Financieros'),
        ('07', 'Gastos de Seguros'),
        ('08', 'Gastos por Regalías y otros Intangibles')
    ])
    service_type_detail = fields.Many2one('invoice.service.type.detail')
    fiscal_status = fields.Selection(
        [('normal', 'Partial'), ('done', 'Reported'), ('blocked', 'Not Sent')],
        copy=False,
        help="* The \'Grey\' status means invoice isn't fully reported and may appear "
             "in other report if a withholding is applied.\n"
        "* The \'Green\' status means invoice is fully reported.\n"
        "* The \'Red\' status means invoice is included in a non sent DGII report.\n"
        "* The blank status means that the invoice have"
        "not been included in a report."
    )

    @api.model
    def norma_recompute(self):
        """
        This method add all compute fields into []env
        add_todo and then recompute
        all compute fields in case dgii config change and need to recompute.
        :return:
        """
        active_ids = self._context.get("active_ids")
        invoice_ids = self.browse(active_ids)
        for k, v in self.fields_get().items():
            if v.get("store") and v.get("depends"):
                self.env.add_to_compute(self._fields[k], invoice_ids)

        self.recompute()
