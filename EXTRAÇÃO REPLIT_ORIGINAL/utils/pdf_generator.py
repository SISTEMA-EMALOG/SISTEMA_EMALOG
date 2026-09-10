
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image, PageBreak, HRFlowable, KeepTogether
)
from reportlab.platypus.flowables import Flowable
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.pdfgen import canvas as pdfgen_canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import io
from datetime import datetime, timedelta
import os

# ── Brand palette ────────────────────────────────────────────────────────────
RED      = colors.HexColor('#D91B3C')
YELLOW   = colors.HexColor('#F5C518')
DARK     = colors.HexColor('#1C1C1C')
CHARCOAL = colors.HexColor('#2D2D2D')
MIDGRAY  = colors.HexColor('#6B7280')
LIGHTGRAY= colors.HexColor('#F3F4F6')
SILVER   = colors.HexColor('#E5E7EB')
WHITE    = colors.white

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

# ── Custom flowable: coloured full-width band ────────────────────────────────
class ColorBand(Flowable):
    def __init__(self, height, fill_color, content_fn=None):
        super().__init__()
        self._height = height
        self._fill = fill_color
        self._content_fn = content_fn
        self.width = CONTENT_W
        self.height = height

    def draw(self):
        c = self.canv
        c.setFillColor(self._fill)
        c.rect(0, 0, self.width, self.height, fill=1, stroke=0)
        if self._content_fn:
            self._content_fn(c, self.width, self.height)

# ── Helper: pill-shaped accent label ─────────────────────────────────────────
def _pill(c, x, y, w, h, bg, txt, txt_color=WHITE, fontsize=8):
    r = h / 2
    c.setFillColor(bg)
    c.roundRect(x, y, w, h, r, fill=1, stroke=0)
    c.setFillColor(txt_color)
    c.setFont('Helvetica-Bold', fontsize)
    c.drawCentredString(x + w / 2, y + (h - fontsize) / 2 + 1, txt)

# ── Style registry ────────────────────────────────────────────────────────────
def _styles():
    base = getSampleStyleSheet()
    reg = {}

    def ps(name, **kw):
        p = ParagraphStyle(name, parent=base['Normal'], **kw)
        reg[name] = p
        return p

    ps('h1',       fontSize=22, fontName='Helvetica-Bold', textColor=WHITE,
                   alignment=TA_LEFT,  spaceAfter=2,  leading=26)
    ps('h2',       fontSize=13, fontName='Helvetica-Bold', textColor=DARK,
                   alignment=TA_LEFT,  spaceAfter=4,  leading=16)
    ps('h3',       fontSize=10, fontName='Helvetica-Bold', textColor=DARK,
                   alignment=TA_LEFT,  spaceAfter=2,  leading=13)
    ps('label',    fontSize=7,  fontName='Helvetica-Bold', textColor=MIDGRAY,
                   alignment=TA_LEFT,  spaceAfter=1)
    ps('value',    fontSize=9,  fontName='Helvetica',      textColor=CHARCOAL,
                   alignment=TA_LEFT,  spaceAfter=2,  leading=12)
    ps('value_b',  fontSize=9,  fontName='Helvetica-Bold', textColor=CHARCOAL,
                   alignment=TA_LEFT,  spaceAfter=2,  leading=12)
    ps('center',   fontSize=9,  fontName='Helvetica',      textColor=CHARCOAL,
                   alignment=TA_CENTER,spaceAfter=2,  leading=12)
    ps('center_b', fontSize=9,  fontName='Helvetica-Bold', textColor=CHARCOAL,
                   alignment=TA_CENTER,spaceAfter=2,  leading=12)
    ps('price_big',fontSize=26, fontName='Helvetica-Bold', textColor=RED,
                   alignment=TA_CENTER,spaceAfter=0,  leading=30)
    ps('footer',   fontSize=7.5,fontName='Helvetica',      textColor=MIDGRAY,
                   alignment=TA_CENTER,leading=11)
    ps('small',    fontSize=7.5,fontName='Helvetica',      textColor=MIDGRAY,
                   alignment=TA_LEFT,  leading=10)
    ps('small_b',  fontSize=7.5,fontName='Helvetica-Bold', textColor=MIDGRAY,
                   alignment=TA_LEFT,  leading=10)
    ps('intro',    fontSize=9,  fontName='Helvetica',      textColor=CHARCOAL,
                   alignment=TA_LEFT,  spaceAfter=6,  leading=14)
    ps('bullet',   fontSize=9,  fontName='Helvetica',      textColor=CHARCOAL,
                   alignment=TA_LEFT,  spaceAfter=4,  leading=13, leftIndent=12)
    return reg

# ── Info card: label + value stacked ─────────────────────────────────────────
def _info_cell(label, value, S):
    return [Paragraph(label.upper(), S['label']), Paragraph(str(value), S['value'])]

def _info_cell_b(label, value, S):
    return [Paragraph(label.upper(), S['label']), Paragraph(str(value), S['value_b'])]

# ── Two-column card table ─────────────────────────────────────────────────────
def _card_table(rows, col_widths, bg=WHITE, border_color=SILVER):
    t = Table(rows, colWidths=col_widths)
    t.setStyle(TableStyle([
        ('BACKGROUND',   (0, 0), (-1, -1), bg),
        ('VALIGN',       (0, 0), (-1, -1), 'TOP'),
        ('ALIGN',        (0, 0), (-1, -1), 'LEFT'),
        ('LEFTPADDING',  (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING',   (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 8),
        ('BOX',          (0, 0), (-1, -1), 0.5, border_color),
        ('LINEBELOW',    (0, 0), (-1, -2), 0.3, SILVER),
    ]))
    return t

# ── Section header ────────────────────────────────────────────────────────────
def _section_header(title, accent=RED):
    data = [[Paragraph(title, ParagraphStyle('sh', parent=ParagraphStyle('x'),
              fontSize=8, fontName='Helvetica-Bold', textColor=WHITE,
              leading=10))]]
    t = Table(data, colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ('BACKGROUND',   (0,0), (-1,-1), accent),
        ('LEFTPADDING',  (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
        ('TOPPADDING',   (0,0), (-1,-1), 5),
        ('BOTTOMPADDING',(0,0), (-1,-1), 5),
    ]))
    return t

# ═══════════════════════════════════════════════════════════════════════════════
def generate_quote_pdf(quote, generated_by_user):
    """Gerar PDF com design moderno EMALOG."""
    try:
        buffer = io.BytesIO()

        # Canvas callbacks for header/footer on every page
        # Use absolute path so the image loads regardless of working directory
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        logo_path = os.path.join(base_dir, 'static', 'logo-emalog.png')

        def on_page(canv, doc):
            _draw_page_chrome(canv, doc, logo_path, quote)

        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            topMargin=52 * mm,   # space for the fixed header band
            bottomMargin=22 * mm,
            leftMargin=MARGIN,
            rightMargin=MARGIN,
        )

        S = _styles()
        content = []

        # ── A. INTRO TEXT ─────────────────────────────────────────────────
        emission_date = datetime.now().strftime('%d/%m/%Y')
        valid_until   = (datetime.now() + timedelta(days=15)).strftime('%d/%m/%Y')

        intro = (
            f"Prezado(a) <b>{quote.client.company_name}</b>, apresentamos nossa proposta de frete "
            f"personalizada para atender às suas necessidades logísticas com qualidade, segurança e "
            f"pontualidade. Esta proposta é válida até <b>{valid_until}</b>."
        )
        content.append(Paragraph(intro, S['intro']))
        content.append(Spacer(1, 6))

        # ── B. REMETENTE / DESTINATÁRIO ───────────────────────────────────
        content.append(_section_header('PARTES ENVOLVIDAS'))
        content.append(Spacer(1, 1))

        half = (CONTENT_W - 4) / 2
        parties_row = [[
            _card_table([
                [Paragraph('DE  (EMISSOR)', ParagraphStyle('x', fontSize=7, fontName='Helvetica-Bold',
                            textColor=RED, leading=9))],
                [Paragraph('EMALOG TECNOLOGIA E TRANSPORTE LTDA', S['value_b'])],
                [Paragraph('CNPJ: 34.571.369/0001-76', S['small'])],
                [Paragraph('R. da Consolação, 2302 — Consolação, São Paulo - SP', S['small'])],
                [Paragraph(f'Emitido por: {generated_by_user.username}', S['small'])],
            ], col_widths=[half], bg=LIGHTGRAY),
            _card_table([
                [Paragraph('PARA  (CLIENTE)', ParagraphStyle('x', fontSize=7, fontName='Helvetica-Bold',
                            textColor=RED, leading=9))],
                [Paragraph(quote.client.company_name, S['value_b'])],
                [Paragraph(f"CNPJ: {quote.client.cnpj or 'N/A'}", S['small'])],
                [Paragraph(f"Telefone: {quote.client.phone or 'N/A'}", S['small'])],
                [Paragraph(f"E-mail: {quote.client.email or 'N/A'}", S['small'])],
            ], col_widths=[half], bg=WHITE),
        ]]
        outer = Table(parties_row, colWidths=[half, half], hAlign='LEFT')
        outer.setStyle(TableStyle([
            ('LEFTPADDING',  (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING',   (0,0), (-1,-1), 0),
            ('BOTTOMPADDING',(0,0), (-1,-1), 0),
            ('VALIGN',       (0,0), (-1,-1), 'TOP'),
        ]))
        content.append(outer)
        content.append(Spacer(1, 10))

        # ── C. ROTA ───────────────────────────────────────────────────────
        content.append(_section_header('ROTA DE TRANSPORTE'))
        content.append(Spacer(1, 1))

        def _build_full_addr(street, number, complement, neighborhood, city, state, cep):
            """Monta endereço completo linha a linha."""
            line1_parts = [p for p in [street, number] if p]
            if complement:
                line1_parts.append(complement)
            line1 = ', '.join(filter(None, line1_parts))
            line2_parts = []
            if neighborhood:
                line2_parts.append(neighborhood)
            if city and state:
                line2_parts.append(f"{city} / {state}")
            if cep:
                line2_parts.append(f"CEP {cep}")
            line2 = ' — '.join(filter(None, line2_parts))
            return line1, line2

        orig_l1, orig_l2 = _build_full_addr(
            quote.origin_street, quote.origin_number, quote.origin_complement,
            quote.origin_neighborhood, quote.origin_city, quote.origin_state, quote.origin_cep
        )
        dest_l1, dest_l2 = _build_full_addr(
            quote.destination_street, quote.destination_number, quote.destination_complement,
            quote.destination_neighborhood, quote.destination_city, quote.destination_state, quote.destination_cep
        )

        pickup_notes   = getattr(quote, 'pickup_address_notes',   None) or ''
        delivery_notes = getattr(quote, 'delivery_address_notes', None) or ''

        # Nome da empresa no local (origem/destino)
        orig_company = getattr(quote, 'origin_company', '') or ''
        dest_company = getattr(quote, 'destination_company', '') or ''

        def _addr_rows(company, line1, line2, notes, label_color):
            rows = []
            if company:
                rows.append([Paragraph(company,
                    ParagraphStyle('co', fontSize=8, fontName='Helvetica-Bold',
                                   textColor=colors.HexColor('#1e3a5f'), leading=10))])
            rows.append([Paragraph(label_color == RED and 'LOCAL DE COLETA' or 'LOCAL DE ENTREGA',
                ParagraphStyle('lbl', fontSize=7, fontName='Helvetica-Bold',
                               textColor=label_color, leading=9))])
            if line1:
                rows.append([Paragraph(line1, S['value_b'])])
            if line2:
                rows.append([Paragraph(line2, S['small'])])
            if notes:
                rows.append([Spacer(1, 3)])
                rows.append([Paragraph('OBS. DE ENDEREÇO:',
                    ParagraphStyle('on', fontSize=7, fontName='Helvetica-Bold',
                                   textColor=colors.HexColor('#92400e'), leading=9))])
                rows.append([Paragraph(notes,
                    ParagraphStyle('ov', fontSize=8, fontName='Helvetica',
                                   textColor=colors.HexColor('#78350f'), leading=11,
                                   backColor=colors.HexColor('#fffbeb')))])
            return rows

        col3 = CONTENT_W / 3
        route_row = [[
            _card_table(
                _addr_rows(orig_company, orig_l1, orig_l2, pickup_notes, RED),
                col_widths=[col3 - 2]),
            _card_table([
                [Paragraph('', S['small'])],
                [Paragraph('→', ParagraphStyle('arr', fontSize=24, fontName='Helvetica-Bold',
                            textColor=YELLOW, alignment=TA_CENTER, leading=28))],
            ], col_widths=[col3 - 2], bg=LIGHTGRAY, border_color=LIGHTGRAY),
            _card_table(
                _addr_rows(dest_company, dest_l1, dest_l2, delivery_notes,
                           colors.HexColor('#15803d')),
                col_widths=[col3 - 2]),
        ]]
        route_tbl = Table(route_row, colWidths=[col3, col3, col3])
        route_tbl.setStyle(TableStyle([
            ('LEFTPADDING',  (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING',   (0,0), (-1,-1), 0),
            ('BOTTOMPADDING',(0,0), (-1,-1), 0),
            ('VALIGN',       (0,0), (-1,-1), 'TOP'),
        ]))
        content.append(route_tbl)
        content.append(Spacer(1, 10))

        # ── D. DETALHES DA CARGA ──────────────────────────────────────────
        content.append(_section_header('DADOS DA CARGA'))
        content.append(Spacer(1, 1))

        load_type_label = (quote.load_type or 'N/A').upper()
        vehicle_label   = (quote.vehicle_type or 'N/A').upper()
        weight_label    = f"{quote.load_weight} kg" if quote.load_weight else 'N/A'
        dims_label      = ' × '.join(filter(None, [
            f"{quote.load_length}cm" if quote.load_length else None,
            f"{quote.load_width}cm"  if quote.load_width  else None,
            f"{quote.load_height}cm" if quote.load_height else None,
        ])) or 'N/A'
        volume_label    = f"{quote.load_volume} m³" if quote.load_volume else 'N/A'
        nf_label        = f"R$ {quote.invoice_value:,.2f}" if quote.invoice_value else 'N/A'
        urgency_label   = 'SIM' if getattr(quote, 'is_urgent', False) else 'NÃO'
        collect_label   = quote.collection_date.strftime('%d/%m/%Y') if getattr(quote, 'collection_date', None) else 'N/A'
        delivery_label  = quote.delivery_date.strftime('%d/%m/%Y')   if getattr(quote, 'delivery_date', None)  else 'N/A'

        q4 = CONTENT_W / 4

        # Quando há itens individuais, dimensões já aparecem na tabela — evita duplicação
        items_preview = quote.cargo_items
        has_items = bool(items_preview)
        fourth_card_row1 = (
            _card_table([[Paragraph('QTD. DE ITENS',   S['label'])],
                         [Paragraph(f"{len(items_preview)} item(s)", S['value_b'])]],
                        col_widths=[q4-2])
            if has_items else
            _card_table([[Paragraph('DIMENSÕES (C×L×A)', S['label'])],
                         [Paragraph(dims_label,           S['value_b'])]],
                        col_widths=[q4-2])
        )
        cargo_specs = Table([
            [
                _card_table([[Paragraph('TIPO DE CARGA', S['label'])],
                             [Paragraph(load_type_label, S['value_b'])]],
                            col_widths=[q4-2]),
                _card_table([[Paragraph('VEÍCULO',       S['label'])],
                             [Paragraph(vehicle_label,   S['value_b'])]],
                            col_widths=[q4-2]),
                _card_table([[Paragraph('PESO TOTAL',    S['label'])],
                             [Paragraph(weight_label,    S['value_b'])]],
                            col_widths=[q4-2]),
                fourth_card_row1,
            ],
            [
                _card_table([[Paragraph('VOLUME',        S['label'])],
                             [Paragraph(volume_label,    S['value_b'])]],
                            col_widths=[q4-2]),
                _card_table([[Paragraph('VALOR DA NF',   S['label'])],
                             [Paragraph(nf_label,        S['value_b'])]],
                            col_widths=[q4-2]),
                _card_table([[Paragraph('URGENTE',       S['label'])],
                             [Paragraph(urgency_label,   S['value_b'])]],
                            col_widths=[q4-2]),
                _card_table([[Paragraph('COLETA / ENTREGA', S['label'])],
                             [Paragraph(f"{collect_label}  →  {delivery_label}", S['value_b'])]],
                            col_widths=[q4-2]),
            ],
        ], colWidths=[q4, q4, q4, q4])
        cargo_specs.setStyle(TableStyle([
            ('LEFTPADDING',  (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING',   (0,0), (-1,-1), 0),
            ('BOTTOMPADDING',(0,0), (-1,-1), 2),
            ('VALIGN',       (0,0), (-1,-1), 'TOP'),
        ]))
        content.append(cargo_specs)

        # ── D2. TABELA DE ITENS DA CARGA ──────────────────────────────────
        items = items_preview  # já calculado acima (evita segunda chamada)
        if items:
            content.append(Spacer(1, 6))

            # Cabeçalho da tabela
            col_w = [CONTENT_W * p for p in [0.04, 0.30, 0.08, 0.11, 0.11, 0.10, 0.13, 0.13]]
            hdr_style = ParagraphStyle('th', fontSize=7, fontName='Helvetica-Bold',
                                       textColor=WHITE, alignment=TA_CENTER, leading=9)
            hdr_style_l = ParagraphStyle('thl', fontSize=7, fontName='Helvetica-Bold',
                                         textColor=WHITE, alignment=TA_LEFT, leading=9)
            cell_style  = ParagraphStyle('td', fontSize=8, fontName='Helvetica',
                                         textColor=CHARCOAL, alignment=TA_CENTER, leading=10)
            cell_style_l = ParagraphStyle('tdl', fontSize=8, fontName='Helvetica',
                                          textColor=CHARCOAL, alignment=TA_LEFT, leading=10)
            cell_bold   = ParagraphStyle('tdb', fontSize=8, fontName='Helvetica-Bold',
                                         textColor=CHARCOAL, alignment=TA_CENTER, leading=10)

            tbl_rows = [[
                Paragraph('#',            hdr_style),
                Paragraph('DESCRIÇÃO',    hdr_style_l),
                Paragraph('QTD',          hdr_style),
                Paragraph('COMP. (CM)',   hdr_style),
                Paragraph('LARG. (CM)',   hdr_style),
                Paragraph('ALT. (CM)',    hdr_style),
                Paragraph('PESO/UN (KG)', hdr_style),
                Paragraph('TOTAL (KG)',   hdr_style),
            ]]

            total_weight = 0.0
            for idx, item in enumerate(items, 1):
                qty    = float(item.get('qty') or 1)
                wt     = float(item.get('weight') or 0)
                total  = qty * wt
                total_weight += total
                ln_val = f"{item['length']:.0f}" if item.get('length') else '—'
                wd_val = f"{item['width']:.0f}"  if item.get('width')  else '—'
                ht_val = f"{item['height']:.0f}" if item.get('height') else '—'
                tbl_rows.append([
                    Paragraph(str(idx),                cell_style),
                    Paragraph(item.get('desc') or '—', cell_style_l),
                    Paragraph(f"{qty:.0f}",            cell_style),
                    Paragraph(ln_val,                  cell_style),
                    Paragraph(wd_val,                  cell_style),
                    Paragraph(ht_val,                  cell_style),
                    Paragraph(f"{wt:,.2f}",            cell_style),
                    Paragraph(f"{total:,.2f}",         cell_bold),
                ])

            # Linha de total
            tbl_rows.append([
                Paragraph('', cell_style),
                Paragraph('', cell_style),
                Paragraph('', cell_style),
                Paragraph('', cell_style),
                Paragraph('', cell_style),
                Paragraph('Peso Total:', ParagraphStyle('ptlbl', fontSize=8,
                           fontName='Helvetica-Bold', textColor=CHARCOAL,
                           alignment=TA_RIGHT, leading=10)),
                Paragraph('', cell_style),
                Paragraph(f"{total_weight:,.2f} kg", ParagraphStyle('ptval', fontSize=8,
                           fontName='Helvetica-Bold', textColor=colors.HexColor('#15803d'),
                           alignment=TA_CENTER, leading=10)),
            ])

            items_tbl = Table(tbl_rows, colWidths=col_w)
            n_data = len(tbl_rows)
            items_tbl.setStyle(TableStyle([
                # Cabeçalho
                ('BACKGROUND',   (0, 0), (-1,  0), DARK),
                ('TEXTCOLOR',    (0, 0), (-1,  0), WHITE),
                # Linhas de dados — zebra
                ('ROWBACKGROUNDS', (0, 1), (-1, n_data - 2), [WHITE, LIGHTGRAY]),
                # Linha de total
                ('BACKGROUND',   (0, n_data - 1), (-1, n_data - 1), colors.HexColor('#f0fdf4')),
                ('LINEABOVE',    (0, n_data - 1), (-1, n_data - 1), 0.8, colors.HexColor('#16a34a')),
                # Bordas
                ('BOX',          (0, 0), (-1, -1), 0.5, SILVER),
                ('INNERGRID',    (0, 0), (-1, -2), 0.3, SILVER),
                # Padding
                ('LEFTPADDING',  (0, 0), (-1, -1), 6),
                ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING',   (0, 0), (-1, -1), 5),
                ('BOTTOMPADDING',(0, 0), (-1, -1), 5),
                ('VALIGN',       (0, 0), (-1, -1), 'MIDDLE'),
            ]))
            content.append(KeepTogether(items_tbl))

        content.append(Spacer(1, 10))

        # ── E. PROPOSTA DE VALOR ──────────────────────────────────────────
        content.append(_section_header('PROPOSTA DE VALOR', accent=DARK))
        content.append(Spacer(1, 1))

        sale_value = float(quote.sale_value or 0)
        service_desc = f"{vehicle_label} — Frete {load_type_label}"

        price_inner = Table([
            [Paragraph('SERVIÇO', S['label']),
             Paragraph('DESCRIÇÃO', S['label']),
             Paragraph('QTD', S['label']),
             Paragraph('VALOR TOTAL', S['label'])],
            [Paragraph('Transporte de Carga', S['value_b']),
             Paragraph(service_desc, S['value']),
             Paragraph('1', S['center']),
             Paragraph(f"R$ {sale_value:,.2f}", ParagraphStyle('vv', fontSize=10,
               fontName='Helvetica-Bold', textColor=RED, alignment=TA_RIGHT))],
        ], colWidths=[CONTENT_W*0.28, CONTENT_W*0.40, CONTENT_W*0.10, CONTENT_W*0.22])
        price_inner.setStyle(TableStyle([
            ('BACKGROUND',   (0, 0), (-1,  0), LIGHTGRAY),
            ('BACKGROUND',   (0, 1), (-1, -1), WHITE),
            ('LINEBELOW',    (0, 0), (-1,  0), 0.5, SILVER),
            ('LINEBELOW',    (0, 1), (-1, -1), 0.3, SILVER),
            ('LEFTPADDING',  (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
            ('TOPPADDING',   (0, 0), (-1, -1), 7),
            ('BOTTOMPADDING',(0, 0), (-1, -1), 7),
            ('VALIGN',       (0, 0), (-1, -1), 'MIDDLE'),
            ('BOX',          (0, 0), (-1, -1), 0.5, SILVER),
        ]))
        content.append(price_inner)
        content.append(Spacer(1, 4))

        # Total highlight box
        total_box = Table([[
            Paragraph('VALOR TOTAL DA PROPOSTA', ParagraphStyle('tlb', fontSize=9,
              fontName='Helvetica-Bold', textColor=CHARCOAL, alignment=TA_LEFT)),
            Paragraph(f"R$ {sale_value:,.2f}", ParagraphStyle('tv', fontSize=16,
              fontName='Helvetica-Bold', textColor=WHITE, alignment=TA_RIGHT)),
        ]], colWidths=[CONTENT_W * 0.6, CONTENT_W * 0.4])
        total_box.setStyle(TableStyle([
            ('BACKGROUND',   (0, 0), (-1, -1), RED),
            ('LEFTPADDING',  (0, 0), (-1, -1), 14),
            ('RIGHTPADDING', (0, 0), (-1, -1), 14),
            ('TOPPADDING',   (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING',(0, 0), (-1, -1), 10),
            ('VALIGN',       (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        content.append(total_box)
        content.append(Spacer(1, 12))

        # ── F. CONDIÇÕES ──────────────────────────────────────────────────
        content.append(KeepTogether([
            _section_header('CONDIÇÕES E OBSERVAÇÕES', accent=CHARCOAL),
            Spacer(1, 4),
            Paragraph("• <b>Validade da proposta:</b> 15 dias a partir da data de emissão.", S['bullet']),
            Paragraph("• <b>Cobertura de seguro:</b> Todas as cargas são protegidas por seguro durante o transporte.", S['bullet']),
            Paragraph("• <b>Prazo de entrega:</b> Conforme data indicada; sujeito a condições de tráfego e acesso.", S['bullet']),
            Paragraph("• <b>Horário de corte:</b> Solicitações de coleta até às 16h00 do dia útil.", S['bullet']),
            Paragraph("• <b>Não incluso:</b> Operações em sábados, domingos e feriados; ajudante; carga IMO/ANVISA/EXÉRCITO/IBAMA; pernoite ou estadia.", S['bullet']),
            Paragraph("• <b>Forma de pagamento:</b> A combinar entre as partes.", S['bullet']),
            Spacer(1, 6),
        ]))

        # ── G. OBSERVAÇÕES ADICIONAIS ─────────────────────────────────────
        if quote.additional_info:
            content.append(KeepTogether([
                _section_header('INFORMAÇÕES ADICIONAIS', accent=MIDGRAY),
                Spacer(1, 4),
                Paragraph(quote.additional_info, S['intro']),
                Spacer(1, 8),
            ]))

        # ── H. ASSINATURA ────────────────────────────────────────────────
        sig_data = [[
            Table([
                [Paragraph('LOCAL E DATA', S['label'])],
                [Paragraph(f"São Paulo, {emission_date}", S['value'])],
                [Spacer(1, 20)],
                [HRFlowable(width='90%', thickness=0.5, color=SILVER)],
                [Paragraph('Assinatura do Cliente', S['small'])],
            ], colWidths=[(CONTENT_W/2) - 10]),
            Table([
                [Paragraph('EMALOG TECNOLOGIA E TRANSPORTE LTDA', S['label'])],
                [Paragraph('CNPJ: 34.571.369/0001-76', S['value'])],
                [Spacer(1, 20)],
                [HRFlowable(width='90%', thickness=0.5, color=SILVER)],
                [Paragraph('Assinatura EMALOG', S['small'])],
            ], colWidths=[(CONTENT_W/2) - 10]),
        ]]
        sig_tbl = Table(sig_data, colWidths=[CONTENT_W/2, CONTENT_W/2])
        sig_tbl.setStyle(TableStyle([
            ('LEFTPADDING',  (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING',   (0,0), (-1,-1), 0),
            ('BOTTOMPADDING',(0,0), (-1,-1), 0),
            ('VALIGN',       (0,0), (-1,-1), 'BOTTOM'),
        ]))
        content.append(sig_tbl)

        # Build — callbacks passados aqui (API correta do ReportLab)
        doc.build(content, onFirstPage=on_page, onLaterPages=on_page)
        buffer.seek(0)
        pdf_data = buffer.getvalue()
        buffer.close()
        print(f"✅ PDF EMALOG gerado para cotação {quote.quote_number}")
        return pdf_data

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise e


def _draw_page_chrome(c, doc, logo_path, quote):
    """Desenha header e footer fixos em cada página."""
    from reportlab.lib.utils import ImageReader
    page_w, page_h = A4
    margin = MARGIN

    # ── HEADER BAND (vermelho) ─────────────────────────────────────────────
    header_h = 48 * mm
    c.setFillColor(RED)
    c.rect(0, page_h - header_h, page_w, header_h, fill=1, stroke=0)

    # Faixa amarela na base do header
    c.setFillColor(YELLOW)
    c.rect(0, page_h - header_h - 4, page_w, 4, fill=1, stroke=0)

    # Logo — usa ImageReader para leitura mais robusta
    logo_drawn = False
    if os.path.exists(logo_path):
        try:
            img_reader = ImageReader(logo_path)
            logo_w = 58 * mm
            logo_h = 22 * mm
            logo_x = margin
            logo_y = page_h - header_h + (header_h - logo_h) / 2
            c.drawImage(img_reader, logo_x, logo_y,
                        width=logo_w, height=logo_h,
                        preserveAspectRatio=True)
            logo_drawn = True
        except Exception as e:
            print(f"⚠️ Logo não carregou: {e}")

    if not logo_drawn:
        # Fallback: texto EMALOG em caso de falha da imagem
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 20)
        c.drawString(margin, page_h - header_h + (header_h / 2) - 8, 'EMALOG')

    # Linha divisória vertical separando logo do lado direito
    divider_x = page_w * 0.55
    c.setStrokeColor(colors.HexColor('#C0152F'))
    c.setLineWidth(0.5)
    c.line(divider_x, page_h - header_h + 8 * mm, divider_x, page_h - 8 * mm)

    # "PROPOSTA COMERCIAL DE FRETE" — direita, topo
    c.setFillColor(colors.HexColor('#FFE082'))
    c.setFont('Helvetica-Bold', 7.5)
    c.drawRightString(page_w - margin, page_h - 12 * mm, 'PROPOSTA COMERCIAL DE FRETE')

    # Número da cotação — direita, em amarelo grande e destacado
    c.setFillColor(YELLOW)
    c.setFont('Helvetica-Bold', 22)
    quote_num = str(getattr(quote, 'quote_number', 'N/A'))
    c.drawRightString(page_w - margin, page_h - 28 * mm, quote_num)

    # Data emissão e validade
    emission = datetime.now().strftime('%d/%m/%Y')
    valid    = (datetime.now() + timedelta(days=15)).strftime('%d/%m/%Y')
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#FECDD3'))
    c.drawRightString(page_w - margin, page_h - 38 * mm,
                      f"Emissão: {emission}   |   Válida até: {valid}")

    # ── FOOTER ────────────────────────────────────────────────────────────
    footer_h = 15 * mm
    c.setFillColor(DARK)
    c.rect(0, 0, page_w, footer_h, fill=1, stroke=0)

    # Faixa amarela no topo do footer
    c.setFillColor(YELLOW)
    c.rect(0, footer_h, page_w, 3, fill=1, stroke=0)

    # Texto do footer
    c.setFillColor(colors.HexColor('#9CA3AF'))
    c.setFont('Helvetica', 6.5)
    footer_txt = ('EMALOG TECNOLOGIA E TRANSPORTE LTDA  |  CNPJ: 34.571.369/0001-76  |'
                  '  R. da Consolação, 2302 — Consolação, São Paulo - SP, 01301-000')
    c.drawCentredString(page_w / 2, footer_h - 5.5 * mm, footer_txt)

    # Número de página (direita, amarelo)
    c.setFillColor(YELLOW)
    c.setFont('Helvetica-Bold', 7)
    c.drawRightString(page_w - margin, 4.5 * mm, f"Pág. {doc.page}")

    # Powered by (esquerda, cinza)
    c.setFillColor(colors.HexColor('#6B7280'))
    c.setFont('Helvetica', 6)
    c.drawString(margin, 4.5 * mm, 'Powered by EMALOG System')


# ── Freight PDF (minimal, mantido) ────────────────────────────────────────────
def generate_freight_pdf(freight):
    buffer = io.BytesIO()
    from reportlab.platypus import SimpleDocTemplate, Paragraph
    from reportlab.lib.styles import getSampleStyleSheet
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    content = [Paragraph(f"FRETE {freight.freight_number}", styles['Title'])]
    doc.build(content)
    buffer.seek(0)
    return buffer.getvalue()
