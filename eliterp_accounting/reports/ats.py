# -*- coding: utf-8 -*-
# Copyright 2018 Elitumdevelop S.A, Ing. Mario Rangel
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

from odoo import api, fields, models
from datetime import datetime
from itertools import groupby
import logging
import base64
from io import StringIO, BytesIO
from jinja2 import Environment, FileSystemLoader
import os
from lxml import etree
from lxml.etree import DocumentInvalid
import time
from operator import itemgetter

STD_FORMAT = '%Y-%m-%d'


class AccountAts(dict):
    """
    representación del archivo ATS
    >>> ats.campo = 'valor'
    >>> ats['campo']
    'valor'
    """

    def __getattr__(self, item):
        try:
            return self.__getitem__(item)
        except KeyError:
            raise AttributeError(item)

    def __setattr__(self, item, value):
        if item in self.__dict__:
            dict.__setattr__(self, item, value)
        else:
            self.__setitem__(item, value)


class AtsXml(models.TransientModel):
    _name = 'eliterp.ats.xml'

    _description = "Ventana para generar ATS (.xml)"
    __logger = logging.getLogger(_name)

    @api.multi
    def _render_xml(self, ats):
        """
        Generar archivo .xml de la carpeta templates
        :param ats: 
        :return: 
        """
        template_path = os.path.join(os.path.dirname(__file__), 'templates')
        e = Environment(loader=FileSystemLoader(template_path))
        ats_template = e.get_template('ats.xml')
        return ats_template.render(ats)

    @api.multi
    def _validate_document(self, ats):
        """
        Validar documento con plantilla .xsd
        :param ats:
        :param error_log:
        :return:
        """
        file_path = os.path.join(os.path.dirname(__file__), 'XSD/ats.xsd')
        schema_file = open(file_path, 'rb')
        xmlschema_doc = etree.parse(schema_file)
        xmlschema = etree.XMLSchema(xmlschema_doc)
        utf8_parser = etree.XMLParser(encoding='utf-8')
        root = etree.fromstring(ats.encode('utf-8'), parser=utf8_parser)
        ok = True
        if not self.no_validate:
            try:
                xmlschema.assertValid(root)
            except DocumentInvalid:
                ok = False
        return ok, xmlschema

    def _convert_date(self, date):
        """
        fecha: '2012-12-15'
        return: '15/12/2012'
        """
        d = date.split('-')
        date = datetime(int(d[0]), int(d[1]), int(d[2]))
        return date.strftime('%d/%m/%Y')

    def _get_canceled(self, period):
        """
        Obtenemos facturas de clientes canceladas y retenciones de compras (Física)
        :param period:
        :return:
        """
        domain = [
            ('state', '=', 'cancel'),
            ('date_invoice', '>=', period.start_date),
            ('date_invoice', '<=', period.closing_date),
            ('type', '=', 'out_invoice')
        ]
        canceled = []
        for inv in self.env['account.invoice'].search(domain):
            auth = inv.sri_authorization_id
            detalleanulados = {
                'tipoComprobante': auth.code,
                'establecimiento': auth.establishment,
                'puntoEmision': auth.emission_point,
                'secuencialInicio': inv.reference[:9],
                'secuencialFin': inv.reference[:9],
                'autorizacion': auth.authorization
            }
            canceled.append(detalleanulados)
        # Retenciones anuladas
        domain_retention = [
            ('state', '=', 'cancel'),
            ('date_withhold', '>=', period.start_date),
            ('date_withhold', '<=', period.closing_date),
            ('type', '=', 'purchase'),
            ('is_sequential', '=', True)
        ]
        for ret in self.env['eliterp.withhold'].search(domain_retention):
            authorization = self.env['eliterp.sri.authorization'].search([('code', '=', '07'), ('active', '=', True)])
            detalleanulados = {
                'tipoComprobante': authorization.code,
                'establecimiento': authorization.establishment,
                'puntoEmision': authorization.emission_point,
                'secuencialInicio': ret.withhold_number[8:17],
                'secuencialFin': ret.withhold_number[8:17],
                'autorizacion': authorization.authorization
            }
            canceled.append(detalleanulados)
        return canceled

    def _get_iva(self, retention):
        """
        Obtenemos total de valores de iva
        :param retention:
        :return:
        """
        total = 0.00
        for line in retention.lines_withhold:
            if line.retention_type == 'iva':
                total += line.amount
        return total

    def _get_rent(self, retention):
        """
        Obtenemos total de valores de renta
        :param retention:
        :return:
        """
        total = 0.00
        for line in retention.lines_withhold:
            if line.retention_type == 'rent':
                total += line.amount
        return total

    def _get_sales(self, period):
        """
        Obtenemos las ventas en el período seleccionado
        :param period:
        :return:
        """
        domain = [
            ('state', 'in', ('open', 'paid')),
            ('date_invoice', '>=', period.start_date),
            ('date_invoice', '<=', period.closing_date),
            ('type', '=', 'out_invoice')
        ]
        sales = []
        for inv in self.env['account.invoice'].search(domain):
            detalleventas = {
                'tpIdCliente': inv.partner_id.type_documentation,
                'idCliente': inv.partner_id.documentation_number,
                'parteRel': 'NO',
                'tipoComprobante': inv.sri_authorization_id.code,
                'tipoEm': 'F',
                'numeroComprobantes': 1,
                'baseNoGraIva': 0.00,
                'baseImponible': inv.amount_untaxed,
                'baseImpGrav': inv.amount_untaxed,
                'montoIva': inv.amount_tax,
                'montoIce': '0.00',
                'valorRetIva': self._get_iva(inv.withhold_id) if inv.withhold_id else 0.00,
                'valorRetRenta': self._get_rent(inv.withhold_id) if inv.withhold_id else 0.00
            }
            sales.append(detalleventas)
        sales = sorted(sales, key=itemgetter('idCliente'))
        ats_sales = []
        for ruc, group in groupby(sales, key=itemgetter('idCliente')):
            baseimp = 0.00
            nograviva = 0.00
            montoiva = 0.00
            retiva = 0.00
            impgrav = 0.00
            retrenta = 0.00
            numComp = 0
            for i in group:
                nograviva += i['baseNoGraIva']
                baseimp += i['baseImponible']
                impgrav += i['baseImpGrav']
                montoiva += i['montoIva']
                retiva += i['valorRetIva']
                retrenta += i['valorRetRenta']
                numComp += 1
            detalle = {
                'tpIdCliente': inv.partner_id.type_documentation,
                'idCliente': ruc,
                'parteRel': 'NO',
                'tipoComprobante': inv.sri_authorization_id.code,
                'tipoEm': 'F',
                'numeroComprobantes': numComp,
                'baseNoGraIva': '%.2f' % nograviva,
                'baseImponible': '%.2f' % baseimp,
                'baseImpGrav': '%.2f' % impgrav,
                'montoIva': '%.2f' % montoiva,
                'montoIce': '0.00',
                'valorRetIva': '%.2f' % retiva,
                'valorRetRenta': '%.2f' % retrenta,
                'formaPago': '20'
            }
            ats_sales.append(detalle)
        return ats_sales

    def _get_refund(self, invoice):
        """
        Obtenemos la nota de crédito de la factura
        :param invoice:
        :return:
        """
        refund = self.env['account.invoice'].search([('invoice_reference', '=', invoice.id)])
        return {
            'docModificado': '01',
            'estabModificado': refund.establishment,
            'ptoEmiModificado': refund.emission_point,
            'secModificado': refund.reference,
            'autModificado': refund.origin,
        }

    def _get_withhold(self, w):
        """
        Obtenemos los datos de la retención si es física (Entregada)
        :param w:
        :return:
        """
        authorization = self.env['eliterp.sri.authorization'].search([('code', '=', '07'), ('active', '=', True)])
        return {
            'estabRetencion1': w.withhold_number[:3],
            'ptoEmiRetencion1': w.withhold_number[4:7],
            'secRetencion1': w.withhold_number[8:],
            'autRetencion1': authorization.authorization,
            'fechaEmiRet1': self._convert_date(w.date_withhold)
        }

    def _process_lines(self, invoice):
        """
        Obtenemos las líneas de retención de la factura
        :param invoice:
        :return:
        """
        data_air = []
        temp = {}
        withhold = invoice.withhold_id
        if withhold:
            for line in withhold.lines_withhold:
                if line.retention_type == 'rent':
                    if not temp.get(line.tax_id.code):
                        temp[line.tax_id.code] = {
                            'baseImpAir': 0,
                            'valRetAir': 0
                        }
                    temp[line.tax_id.code]['baseImpAir'] += line.base_taxable
                    temp[line.tax_id.code]['codRetAir'] = line.tax_id.code
                    temp[line.tax_id.code]['porcentajeAir'] = int(line.tax_id.amount)
                    temp[line.tax_id.code]['valRetAir'] += abs(line.amount)
        for k, v in temp.items():
            data_air.append(v)
        return data_air

    def _get_retention_iva(self, invoice):
        """
        Obtenemos el monto por código de retención
        :param invoice:
        :return:
        """
        retBien10 = 0
        retServ20 = 0
        retBien = 0
        retServ = 0
        retServ100 = 0
        withhold = invoice.withhold_id
        if withhold:
            for tax in withhold.lines_withhold:
                if tax.retention_type == 'iva':
                    if tax.tax_id.code == '725':
                        # Retención 30%
                        retBien += abs(tax.amount)
                    if tax.tax_id.code == '727':
                        # Retención 70%
                        retServ += abs(tax.amount)
                    if tax.tax_id.code == '729':
                        # Retención 100%
                        retServ100 += abs(tax.amount)
        return retBien10, retServ20, retBien, retServ, retServ100

    def _get_purchases(self, period, pay_limit):
        """
        Obtenemos las facturas de compras en período
        :param period:
        :param pay_limit:
        :return:
        """
        object_invoice = self.env['account.invoice']
        domain_purchase = [
            ('state', 'in', ['open', 'paid']),
            ('date_invoice', '>=', period.start_date),
            ('date_invoice', '<=', period.closing_date),
            ('type', 'in', ['in_invoice', 'in_refund'])
        ]
        purchases = object_invoice.search(domain_purchase)
        ats_purchases = []
        for line in purchases:
            if not line.partner_id.type_documentation == '06':  # TODO: Proveedores con pasaporte no se procesa
                detallecompras = {}
                valRetBien10, valRetServ20, valorRetBienes, valorRetServicios, valRetServ100 = self._get_retention_iva(
                    line)
                detallecompras.update({
                    'codSustento': line.tax_support_id.code,
                    'tpIdProv': line.partner_id.type_documentation,
                    'idProv': line.partner_id.documentation_number,
                    'tipoComprobante': '01' if line.type == 'in_invoice' else '02',
                    'parteRel': 'NO',
                    'fechaRegistro': self._convert_date(line.date_invoice),
                    'establecimiento': line.establishment,
                    'puntoEmision': line.emission_point,
                    'secuencial': line.reference,
                    'fechaEmision': self._convert_date(line.date_invoice),
                    'autorizacion': line.authorization,
                    'baseNoGraIva': '0.00',
                    'baseImponible': '%.2f' % line.amount_untaxed,
                    'baseImpGrav': '%.2f' % line.amount_untaxed,
                    'baseImpExe': '0.00',
                    'montoIce': '0.00',
                    'montoIva': '%.2f' % line.amount_tax,
                    'valRetBien10': '0.00',
                    'valRetServ20': '0.00',
                    'valorRetBienes': '%.2f' % valorRetBienes,
                    'valRetServ50': '0.00',
                    'valorRetServicios': '%.2f' % valorRetServicios,
                    'valRetServ100': '%.2f' % valRetServ100,
                    'totbasesImpReemb': '0.00',
                    'detalleAir': self._process_lines(line)
                })
                if line.amount_total >= pay_limit:  # Formas de pago
                    detallecompras.update({'pay': True})
                    detallecompras.update({'formaPago': line.way_pay_id.code})
                if line.have_withhold:  # Si tiene retención entregada (Física)
                    if line.withhold_id.is_sequential:
                        detallecompras.update({'retencion': True})
                        detallecompras.update(self._get_withhold(line.withhold_id))
                if line.have_credit_note:  # Si tiene nota de cŕedito
                    detallecompras.update({'nota': True})
                    detallecompras.update(self._get_refund(line))
                ats_purchases.append(detallecompras)
        return ats_purchases

    def _get_total_sales(self, period):
        """
        Obtenemos el total de ventas en dicho período
        :param period:
        :return:
        """
        sql = "SELECT type, sum(amount_untaxed) AS base \
                          FROM account_invoice \
                          WHERE type IN ('out_invoice', 'out_refund') \
                          AND state IN ('open','paid') \
                          AND date_invoice BETWEEN '%s' AND '%s'" % (period.start_date, period.closing_date)
        sql += " GROUP BY type;"
        self.env.cr.execute(sql)
        res = self.env.cr.fetchall()
        result = sum(map(lambda x: x[0] == 'out_refund' and x[1] * -1 or x[1], res))
        return result

    def _get_date_value(self, date, t='%Y'):
        """
        Obtenemos el valor del mes y año
        :param date:
        :param t:
        :return:
        """
        return time.strftime(t, time.strptime(date, STD_FORMAT))

    @api.multi
    def generate_ats(self):
        ats = AccountAts()
        # Información de cabecera
        period = self.period_id
        ats.TipoIDInformante = 'R'
        ats.IdInformante = self.company_id.partner_id.documentation_number
        ats.razonSocial = self.company_id.name.replace('.', '')
        ats.Anio = self._get_date_value(period.start_date, '%Y')
        ats.Mes = self._get_date_value(period.start_date, '%m')
        ats.numEstabRuc = self.establishment.zfill(3)
        ats.totalVentas = '%.2f' % self._get_total_sales(period)
        ats.codigoOperativo = 'IVA'
        # Compras
        ats.compras = self._get_purchases(period, self.pay_limit)
        # Ventas
        ats.ventas = self._get_sales(period)
        # Ventas del establecimiento
        ats.codEstab = '001'
        ats.ventasEstab = '%.2f' % self._get_total_sales(period)
        ats.ivaComp = '0.00'
        # Cancelados
        ats.anulados = self._get_canceled(period)
        # Proceso del archivo a exportar
        ats_rendered = self._render_xml(ats)
        ok, schema = self._validate_document(ats_rendered)
        buf = StringIO()
        buf.write(ats_rendered)
        out = base64.b64encode(bytes(buf.getvalue(), 'utf-8'))
        buf.close()
        buf_error = StringIO()
        for error in schema.error_log: # Si tiene errores se imprimen en documento
            buf_error.write("ERROR ON LINE %s: %s" % (error.line, error.message.encode("utf-8")) + '\n')
        out_error = base64.b64encode(bytes(buf_error.getvalue(), 'utf-8'))
        buf_error.close()
        name = "%s-%s%s.xml" % (
            "ATS",
            period.start_date[5:7],
            period.start_date[:4]
        )
        data2save = {
            'state': ok and 'export' or 'export_error',
            'file': out,
            'file_name': name
        }
        if not ok:
            data2save.update({
                'state': 'export_error',
                'error_file': out_error,
                'error_file_name': 'Errores.txt'
            })
        self.write(data2save)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eliterp.ats.xml',
            'view_mode': ' form',
            'view_type': ' form',
            'res_id': self.id,
            'views': [(False, 'form')],
            'target': 'new',
        }

    @api.multi
    def _default_company(self):
        """
        Obtenemos la compañía por defecto del usuario
        :return:
        """
        return self.env.user.company_id.id

    file_name = fields.Char('Nombre de archivo', size=50, readonly=True)
    error_file_name = fields.Char('Nombre del archivo de error', size=50, readonly=True)
    year_accounting = fields.Many2one(
        'eliterp.account.period',
        'Año contable',
        required=True
    )
    period_id = fields.Many2one(
        'eliterp.lines.account.period',
        'Período',
        required=True
    )
    company_id = fields.Many2one(
        'res.company',
        'Companía',
        default=_default_company
    )
    establishment = fields.Char(
        'No. establecimiento',
        size=3,
        required=True,
        default='001'
    )
    pay_limit = fields.Float('Límite de pago', default=1000)
    file = fields.Binary('Archivo XML')
    error_file = fields.Binary('Archivo de error')
    no_validate = fields.Boolean('No validar', help="Permite validar el archivo XML con esquema XSD.")
    state = fields.Selection(
        (
            ('choose', 'Elegir'),
            ('export', 'Generado'),
            ('export_error', 'Error')
        ),
        string='Estado',
        default='choose'
    )
