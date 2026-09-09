from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from models import Driver, Client, Quote, Freight, Payment, Lead, Opportunity, User, FreightStatusLog
from app import db
from sqlalchemy import func, and_, or_
from sqlalchemy.orm import aliased
from datetime import datetime, timedelta
import calendar as _cal
import json
import logging
import os

reports_bp = Blueprint('reports', __name__, url_prefix='/reports')


def _date_range(request):
    _now = datetime.now()
    _last = _cal.monthrange(_now.year, _now.month)[1]
    start = request.args.get('start_date', _now.replace(day=1).strftime('%Y-%m-%d'))
    end   = request.args.get('end_date',   _now.replace(day=_last).strftime('%Y-%m-%d'))
    start_dt = datetime.strptime(start, '%Y-%m-%d')
    end_dt   = datetime.strptime(end,   '%Y-%m-%d').replace(hour=23, minute=59, second=59)
    return start, end, start_dt, end_dt


@reports_bp.route('/')
@login_required
def index():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))

    start_date, end_date, start_dt, end_dt = _date_range(request)

    # ── 1. KPIs ──────────────────────────────────────────────────────────────
    total_fretes = Freight.query.filter(
        Freight.created_at.between(start_dt, end_dt)
    ).count()

    fretes_entregues = Freight.query.filter(
        Freight.created_at.between(start_dt, end_dt),
        Freight.status == 'entregue'
    ).count()

    fretes_ativos = Freight.query.filter(
        Freight.created_at.between(start_dt, end_dt),
        Freight.status.in_(['aceito', 'em_coleta', 'em_transito'])
    ).count()

    receita_total = db.session.query(func.sum(Freight.agreed_price)).filter(
        Freight.created_at.between(start_dt, end_dt),
        Freight.status.in_(['aceito', 'em_coleta', 'em_transito', 'entregue'])
    ).scalar() or 0

    custo_total = db.session.query(func.sum(Freight.driver_cost)).filter(
        Freight.created_at.between(start_dt, end_dt),
        Freight.status.in_(['aceito', 'em_coleta', 'em_transito', 'entregue'])
    ).scalar() or 0

    margem = float(receita_total) - float(custo_total)
    margem_pct = round(margem / float(receita_total) * 100, 1) if receita_total else 0

    total_cotacoes = Quote.query.filter(
        Quote.created_at.between(start_dt, end_dt)
    ).count()

    cotacoes_aprovadas = Quote.query.filter(
        Quote.created_at.between(start_dt, end_dt),
        Quote.status.in_(['aprovada', 'aprovada_cliente'])
    ).count()

    taxa_conversao = round(cotacoes_aprovadas / total_cotacoes * 100, 1) if total_cotacoes else 0

    pagamentos_pendentes_valor = db.session.query(func.sum(Payment.amount)).filter(
        Payment.status == 'pendente',
        Payment.payment_type.in_(['carregamento_70', 'finalizacao_30', 'frete_total'])
    ).scalar() or 0

    kpis = dict(
        total_fretes=total_fretes,
        fretes_entregues=fretes_entregues,
        fretes_ativos=fretes_ativos,
        receita_total=float(receita_total),
        custo_total=float(custo_total),
        margem=margem,
        margem_pct=margem_pct,
        total_cotacoes=total_cotacoes,
        cotacoes_aprovadas=cotacoes_aprovadas,
        taxa_conversao=taxa_conversao,
        pagamentos_pendentes_valor=float(pagamentos_pendentes_valor),
    )

    # ── 2. Evolução Mensal (últimos 6 meses) ─────────────────────────────────
    monthly_data = []
    for i in range(5, -1, -1):
        ref = datetime.now().replace(day=1)
        # go back i months
        month = ref.month - i
        year  = ref.year
        while month <= 0:
            month += 12
            year  -= 1
        m_start = datetime(year, month, 1)
        m_end   = datetime(year, month, _cal.monthrange(year, month)[1], 23, 59, 59)

        m_rec = db.session.query(func.sum(Freight.agreed_price)).filter(
            Freight.created_at.between(m_start, m_end),
            Freight.status.in_(['aceito', 'em_coleta', 'em_transito', 'entregue'])
        ).scalar() or 0

        m_cus = db.session.query(func.sum(Freight.driver_cost)).filter(
            Freight.created_at.between(m_start, m_end),
            Freight.status.in_(['aceito', 'em_coleta', 'em_transito', 'entregue'])
        ).scalar() or 0

        m_frt = Freight.query.filter(
            Freight.created_at.between(m_start, m_end)
        ).count()

        monthly_data.append({
            'month':   m_start.strftime('%b/%y'),
            'receita': round(float(m_rec), 2),
            'custo':   round(float(m_cus), 2),
            'margem':  round(float(m_rec) - float(m_cus), 2),
            'fretes':  m_frt,
        })

    # ── 3. Top 10 Clientes por fretes ────────────────────────────────────────
    top_clientes_rows = db.session.query(
        Client.company_name,
        func.count(Freight.id).label('total_fretes'),
        func.sum(Freight.agreed_price).label('receita'),
        func.sum(Freight.driver_cost).label('custo'),
    ).join(Freight, Freight.client_id == Client.id).filter(
        Freight.created_at.between(start_dt, end_dt)
    ).group_by(Client.id).order_by(func.count(Freight.id).desc()).limit(10).all()

    top_clientes = []
    for r in top_clientes_rows:
        rec = float(r.receita or 0)
        cus = float(r.custo or 0)
        margem_c = rec - cus
        top_clientes.append({
            'nome':   r.company_name,
            'fretes': r.total_fretes,
            'receita': rec,
            'custo':   cus,
            'margem':  round(margem_c, 2),
            'margem_pct': round(margem_c / rec * 100, 1) if rec else 0,
        })

    # ── 4. Top 10 Rotas mais atendidas ────────────────────────────────────────
    rotas_rows = db.session.query(
        func.coalesce(Freight.origin_city, Freight.origin).label('orig'),
        func.coalesce(Freight.destination_city, Freight.destination).label('dest'),
        func.count(Freight.id).label('freq'),
        func.avg(Freight.agreed_price).label('preco_medio'),
        func.sum(Freight.agreed_price).label('receita_total'),
    ).filter(
        Freight.created_at.between(start_dt, end_dt)
    ).group_by('orig', 'dest').order_by(func.count(Freight.id).desc()).limit(10).all()

    top_rotas = []
    for r in rotas_rows:
        orig = r.orig or 'Origem N/D'
        dest = r.dest or 'Destino N/D'
        top_rotas.append({
            'rota':         f'{orig} → {dest}',
            'freq':         r.freq,
            'preco_medio':  round(float(r.preco_medio or 0), 2),
            'receita_total': round(float(r.receita_total or 0), 2),
        })

    # ── 5. Top 15 Motoristas ──────────────────────────────────────────────────
    motoristas_rows = db.session.query(
        Driver.name,
        Driver.truck_type,
        func.count(Freight.id).label('total_fretes'),
        func.sum(Freight.agreed_price).label('receita'),
        func.sum(Freight.driver_cost).label('custo'),
    ).join(Freight, Freight.assigned_driver_id == Driver.id).filter(
        Freight.created_at.between(start_dt, end_dt)
    ).group_by(Driver.id).order_by(func.count(Freight.id).desc()).limit(15).all()

    top_motoristas = []
    for r in motoristas_rows:
        rec = float(r.receita or 0)
        cus = float(r.custo or 0)
        top_motoristas.append({
            'nome':       r.name,
            'tipo':       r.truck_type or 'N/D',
            'fretes':     r.total_fretes,
            'receita':    rec,
            'custo':      cus,
            'margem':     round(rec - cus, 2),
        })

    # ── 6. Distribuição por tipo de veículo ──────────────────────────────────
    tipos_rows = db.session.query(
        Driver.truck_type,
        func.count(Freight.id).label('total')
    ).join(Freight, Freight.assigned_driver_id == Driver.id).filter(
        Freight.created_at.between(start_dt, end_dt),
        Driver.truck_type.isnot(None)
    ).group_by(Driver.truck_type).order_by(func.count(Freight.id).desc()).all()

    if not tipos_rows:
        tipos_rows = db.session.query(
            Quote.vehicle_type,
            func.count(Quote.id).label('total')
        ).filter(
            Quote.created_at.between(start_dt, end_dt),
            Quote.vehicle_type.isnot(None)
        ).group_by(Quote.vehicle_type).order_by(func.count(Quote.id).desc()).all()
        tipos_veiculo = [{'tipo': r.vehicle_type, 'total': r.total} for r in tipos_rows]
    else:
        tipos_veiculo = [{'tipo': r.truck_type, 'total': r.total} for r in tipos_rows]

    # ── 7. Status dos fretes ─────────────────────────────────────────────────
    status_fretes_rows = db.session.query(
        Freight.status, func.count(Freight.id).label('total')
    ).filter(Freight.created_at.between(start_dt, end_dt)).group_by(Freight.status).all()

    status_labels_map = {
        'ofertado': 'Ofertado', 'aceito': 'Aceito', 'em_coleta': 'Em Coleta', 'em_transito': 'Em Trânsito',
        'entregue': 'Entregue', 'cancelado': 'Cancelado', 'negociacao': 'Negociação',
        'pendente': 'Pendente',
    }
    status_fretes = [{'status': status_labels_map.get(r.status, r.status), 'total': r.total}
                     for r in status_fretes_rows]

    # ── 8. Status das cotações ────────────────────────────────────────────────
    status_cot_rows = db.session.query(
        Quote.status, func.count(Quote.id).label('total')
    ).filter(Quote.created_at.between(start_dt, end_dt)).group_by(Quote.status).all()

    status_cot_map = {
        'pendente': 'Pendente', 'cotada': 'Cotada', 'aprovada': 'Aprovada',
        'aprovada_cliente': 'Aprovada (Cliente)', 'negociacao': 'Negociação', 'negada': 'Negada',
    }
    status_cotacoes = [{'status': status_cot_map.get(r.status, r.status), 'total': r.total}
                       for r in status_cot_rows]

    # ── 9. Pagamentos pendentes por motorista ─────────────────────────────────
    pag_rows = db.session.query(
        Driver.name,
        func.count(Payment.id).label('qtd'),
        func.sum(Payment.amount).label('total'),
    ).join(Payment, Payment.driver_id == Driver.id).filter(
        Payment.status == 'pendente',
        Payment.payment_type.in_(['carregamento_70', 'finalizacao_30', 'frete_total'])
    ).group_by(Driver.id).order_by(func.sum(Payment.amount).desc()).all()

    pagamentos_motoristas = [
        {'nome': r.name, 'qtd': r.qtd, 'total': float(r.total or 0)}
        for r in pag_rows
    ]

    # ── 10. Tipos de carga (dedicado vs fracionado) ───────────────────────────
    carga_rows = db.session.query(
        Quote.load_type, func.count(Quote.id).label('total')
    ).filter(
        Quote.created_at.between(start_dt, end_dt),
        Quote.load_type.isnot(None)
    ).group_by(Quote.load_type).all()
    tipos_carga = [{'tipo': r.load_type, 'total': r.total} for r in carga_rows]

    # ── 11. CRM & Vendas ─────────────────────────────────────────────────────
    crm_total_leads  = Lead.query.count()
    crm_leads_novos  = Lead.query.filter_by(status='novo').count()
    crm_convertidos  = Lead.query.filter_by(status='convertido').count()
    crm_perdidos     = Lead.query.filter_by(status='perdido').count()
    crm_proposta     = Lead.query.filter_by(status='proposta').count()
    crm_qualificado  = Lead.query.filter_by(status='qualificado').count()
    crm_em_contato   = Lead.query.filter_by(status='em_contato').count()
    crm_conv_rate    = round(crm_convertidos / crm_total_leads * 100, 1) if crm_total_leads else 0

    pipeline_valor = db.session.query(func.sum(Opportunity.value)).filter(
        Opportunity.stage.notin_(['ganho', 'perdido']),
        Opportunity.value.isnot(None)
    ).scalar() or 0

    opps_abertas = db.session.query(func.count(Opportunity.id)).filter(
        Opportunity.stage.notin_(['ganho', 'perdido'])
    ).scalar() or 0

    # Leads por fonte
    fonte_rows = db.session.query(
        Lead.source,
        func.count(Lead.id).label('total'),
        func.sum(db.case((Lead.status == 'convertido', 1), else_=0)).label('conv'),
    ).group_by(Lead.source).order_by(func.count(Lead.id).desc()).all()

    crm_fontes = []
    for r in fonte_rows:
        t = r.total or 0
        c = int(r.conv or 0)
        crm_fontes.append({
            'fonte': (r.source or 'outro').replace('_', ' ').title(),
            'total': t,
            'conv':  c,
            'taxa':  round(c / t * 100, 1) if t else 0,
        })

    # Oportunidades por stage
    stage_rows = db.session.query(
        Opportunity.stage,
        func.count(Opportunity.id).label('qtd'),
        func.sum(Opportunity.value).label('valor'),
    ).group_by(Opportunity.stage).all()

    stage_map = {
        'prospeccao': 'Prospecção', 'contato': 'Contato', 'reuniao': 'Reunião',
        'proposta': 'Proposta', 'negociacao': 'Negociação', 'ganho': 'Ganho', 'perdido': 'Perdido',
    }
    crm_stages = [{'stage': stage_map.get(r.stage, r.stage), 'qtd': r.qtd, 'valor': float(r.valor or 0)}
                  for r in stage_rows]

    # Performance por vendedor (leads + cotações vinculadas + receita gerada)
    vend_rows = db.session.query(
        User.id,
        User.username,
        func.count(Lead.id).label('leads'),
        func.sum(db.case((Lead.status == 'convertido', 1), else_=0)).label('conv'),
    ).outerjoin(Lead, Lead.assigned_to == User.id).filter(
        User.role.in_(['vendedor', 'admin', 'operador'])
    ).group_by(User.id).order_by(func.count(Lead.id).desc()).all()

    vendedor_perf = []
    for r in vend_rows:
        leads = r.leads or 0
        conv  = int(r.conv or 0)
        vendedor_perf.append({
            'nome':  r.username,
            'leads': leads,
            'conv':  conv,
            'taxa':  round(conv / leads * 100, 1) if leads else 0,
        })

    # Dados JSON para funil CRM
    chart_crm_funil = json.dumps({
        'labels': ['Leads', 'Em Contato', 'Qualificado', 'Proposta', 'Convertido'],
        'data':   [crm_total_leads, crm_em_contato, crm_qualificado, crm_proposta, crm_convertidos],
    })
    chart_crm_fontes = json.dumps({
        'labels': [f['fonte'] for f in crm_fontes],
        'data':   [f['total'] for f in crm_fontes],
        'conv':   [f['conv']  for f in crm_fontes],
    })
    chart_crm_stages = json.dumps({
        'labels': [s['stage'] for s in crm_stages],
        'data':   [s['qtd']   for s in crm_stages],
        'valor':  [s['valor'] for s in crm_stages],
    })

    # ── 12. Carteira de Clientes (RFM) ────────────────────────────────────────
    _now = datetime.now()
    _12m = _now - timedelta(days=365)

    carteira_rows = db.session.query(
        Client.id,
        Client.company_name,
        Client.city,
        Client.state,
        func.max(Freight.created_at).label('ultima_compra'),
        func.count(Freight.id).label('freq_12m'),
        func.sum(Freight.agreed_price).label('valor_12m'),
    ).outerjoin(Freight, and_(
        Freight.client_id == Client.id,
        Freight.created_at >= _12m,
        Freight.status != 'cancelado',
    )).filter(Client.active == True).group_by(Client.id).order_by(
        func.max(Freight.created_at).desc().nulls_last()
    ).all()

    carteira_rfm = []
    for r in carteira_rows:
        ultima = r.ultima_compra
        dias   = (_now - ultima).days if ultima else 9999
        freq   = r.freq_12m or 0
        valor  = float(r.valor_12m or 0)

        if dias <= 30:   r_score = 3
        elif dias <= 90: r_score = 2
        else:            r_score = 1

        if freq >= 5:   f_score = 3
        elif freq >= 2: f_score = 2
        else:           f_score = 1

        if valor >= 10000:  m_score = 3
        elif valor >= 3000: m_score = 2
        else:               m_score = 1

        rfm = r_score + f_score + m_score

        if rfm >= 8:       risco = 'Campeão'
        elif rfm >= 6:     risco = 'Fiel'
        elif rfm >= 4:     risco = 'Em risco'
        elif dias <= 180:  risco = 'Dormindo'
        else:              risco = 'Perdido'

        carteira_rfm.append({
            'id':     r.id,
            'nome':   r.company_name,
            'cidade': f"{r.city or ''}/{r.state or ''}".strip('/'),
            'dias':   dias if dias < 9999 else None,
            'freq':   freq,
            'valor':  valor,
            'rfm':    rfm,
            'risco':  risco,
        })

    rfm_resumo = {}
    for c in carteira_rfm:
        rfm_resumo[c['risco']] = rfm_resumo.get(c['risco'], 0) + 1

    clientes_dormentes = [c for c in carteira_rfm if c['risco'] in ('Dormindo', 'Perdido')]
    chart_rfm = json.dumps({'labels': list(rfm_resumo.keys()), 'data': list(rfm_resumo.values())})

    # ── 13. SLA Operacional ───────────────────────────────────────────────────
    # Use FreightStatusLog to find exact delivery datetime
    log_alias = aliased(FreightStatusLog)
    sla_raw = db.session.query(
        Freight.id,
        func.coalesce(Freight.origin_city, Freight.origin).label('orig'),
        func.coalesce(Freight.destination_city, Freight.destination).label('dest'),
        Client.company_name.label('cliente'),
        Freight.delivery_date,
        func.min(log_alias.created_at).label('delivered_at'),
    ).join(Client, Freight.client_id == Client.id).outerjoin(
        log_alias, and_(
            log_alias.freight_id == Freight.id,
            log_alias.new_status == 'entregue',
        )
    ).filter(
        Freight.status == 'entregue',
        Freight.delivery_date.isnot(None),
        Freight.created_at.between(start_dt, end_dt),
    ).group_by(Freight.id, Client.company_name).all()

    sla_total = sla_on_time = sla_late = 0
    sla_por_rota  = {}
    sla_por_cliente = {}

    for row in sla_raw:
        sla_total += 1
        actual_dt = row.delivered_at or None
        actual_d  = actual_dt.date() if actual_dt else None
        on_time   = (actual_d <= row.delivery_date) if actual_d else True

        if on_time: sla_on_time += 1
        else:       sla_late    += 1

        rota_k = f"{row.orig or 'N/D'} → {row.dest or 'N/D'}"
        if rota_k not in sla_por_rota:
            sla_por_rota[rota_k] = {'total': 0, 'on_time': 0}
        sla_por_rota[rota_k]['total']   += 1
        sla_por_rota[rota_k]['on_time'] += 1 if on_time else 0

        cli_k = row.cliente
        if cli_k not in sla_por_cliente:
            sla_por_cliente[cli_k] = {'total': 0, 'on_time': 0}
        sla_por_cliente[cli_k]['total']   += 1
        sla_por_cliente[cli_k]['on_time'] += 1 if on_time else 0

    sla_geral_taxa = round(sla_on_time / sla_total * 100, 1) if sla_total else 0

    sla_rotas_list = sorted([
        {'rota': k, 'total': v['total'], 'on_time': v['on_time'],
         'atrasados': v['total'] - v['on_time'],
         'taxa': round(v['on_time'] / v['total'] * 100, 1) if v['total'] else 0}
        for k, v in sla_por_rota.items()
    ], key=lambda x: x['total'], reverse=True)[:12]

    sla_clientes_list = sorted([
        {'cliente': k, 'total': v['total'], 'on_time': v['on_time'],
         'atrasados': v['total'] - v['on_time'],
         'taxa': round(v['on_time'] / v['total'] * 100, 1) if v['total'] else 0}
        for k, v in sla_por_cliente.items()
    ], key=lambda x: x['total'], reverse=True)[:10]

    chart_sla_gauge = json.dumps({
        'labels': ['No Prazo', 'Atrasados', 'Sem Data'],
        'data':   [sla_on_time, sla_late, sla_total - sla_on_time - sla_late],
    })

    # ── JSON para charts ──────────────────────────────────────────────────────
    chart_mensal = json.dumps({
        'labels':   [m['month'] for m in monthly_data],
        'receita':  [m['receita'] for m in monthly_data],
        'custo':    [m['custo'] for m in monthly_data],
        'margem':   [m['margem'] for m in monthly_data],
        'fretes':   [m['fretes'] for m in monthly_data],
    })

    chart_status_fretes = json.dumps({
        'labels': [s['status'] for s in status_fretes],
        'data':   [s['total'] for s in status_fretes],
    })

    chart_status_cotacoes = json.dumps({
        'labels': [s['status'] for s in status_cotacoes],
        'data':   [s['total'] for s in status_cotacoes],
    })

    chart_tipos_veiculo = json.dumps({
        'labels': [t['tipo'] for t in tipos_veiculo],
        'data':   [t['total'] for t in tipos_veiculo],
    })

    chart_carga = json.dumps({
        'labels': [t['tipo'] for t in tipos_carga],
        'data':   [t['total'] for t in tipos_carga],
    })

    return render_template('reports/index.html',
        start_date=start_date, end_date=end_date,
        kpis=kpis,
        monthly_data=monthly_data,
        top_clientes=top_clientes,
        top_rotas=top_rotas,
        top_motoristas=top_motoristas,
        tipos_veiculo=tipos_veiculo,
        tipos_carga=tipos_carga,
        status_fretes=status_fretes,
        status_cotacoes=status_cotacoes,
        pagamentos_motoristas=pagamentos_motoristas,
        chart_mensal=chart_mensal,
        chart_status_fretes=chart_status_fretes,
        chart_status_cotacoes=chart_status_cotacoes,
        chart_tipos_veiculo=chart_tipos_veiculo,
        chart_carga=chart_carga,
        crm_total_leads=crm_total_leads,
        crm_leads_novos=crm_leads_novos,
        crm_convertidos=crm_convertidos,
        crm_perdidos=crm_perdidos,
        crm_em_contato=crm_em_contato,
        crm_qualificado=crm_qualificado,
        crm_proposta=crm_proposta,
        crm_conv_rate=crm_conv_rate,
        pipeline_valor=float(pipeline_valor),
        opps_abertas=opps_abertas,
        crm_fontes=crm_fontes,
        crm_stages=crm_stages,
        vendedor_perf=vendedor_perf,
        chart_crm_funil=chart_crm_funil,
        chart_crm_fontes=chart_crm_fontes,
        chart_crm_stages=chart_crm_stages,
        carteira_rfm=carteira_rfm,
        rfm_resumo=rfm_resumo,
        clientes_dormentes=clientes_dormentes,
        chart_rfm=chart_rfm,
        sla_total=sla_total,
        sla_on_time=sla_on_time,
        sla_late=sla_late,
        sla_geral_taxa=sla_geral_taxa,
        sla_rotas_list=sla_rotas_list,
        sla_clientes_list=sla_clientes_list,
        chart_sla_gauge=chart_sla_gauge,
        datetime=datetime, timedelta=timedelta,
    )


@reports_bp.route('/export')
@login_required
def export():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))

    import io
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        flash('Biblioteca openpyxl não instalada.', 'error')
        return redirect(url_for('reports.index'))
    from flask import send_file

    start_date, end_date, start_dt, end_dt = _date_range(request)

    wb = openpyxl.Workbook()

    # ── Aba 1: Fretes ─────────────────────────────────────────────────────────
    ws1 = wb.active
    ws1.title = 'Fretes'
    header_font = Font(bold=True, color='FFFFFF')
    header_fill = PatternFill('solid', fgColor='1D4ED8')
    headers = ['Nº Frete', 'Cliente', 'Origem', 'Destino', 'Status',
               'Valor Venda (R$)', 'Custo Motorista (R$)', 'Margem (R$)', 'Data Criação']
    for col, h in enumerate(headers, 1):
        cell = ws1.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center')

    freights = Freight.query.filter(
        Freight.created_at.between(start_dt, end_dt)
    ).order_by(Freight.created_at.desc()).all()

    for row, f in enumerate(freights, 2):
        sale = float(f.agreed_price or 0)
        cost = float(f.driver_cost or 0)
        ws1.append([
            f.freight_number,
            f.client.company_name if f.client else '',
            f.origin,
            f.destination,
            f.status,
            sale,
            cost,
            round(sale - cost, 2),
            f.created_at.strftime('%d/%m/%Y') if f.created_at else ''
        ])

    for col in ws1.columns:
        ws1.column_dimensions[col[0].column_letter].width = max(len(str(col[0].value or '')), 14)

    # ── Aba 2: Cotações ──────────────────────────────────────────────────────
    ws2 = wb.create_sheet('Cotações')
    cot_headers = ['Nº Cotação', 'Cliente', 'Origem', 'Destino', 'Status',
                   'Custo Motorista (R$)', 'Valor Venda (R$)', 'Data Criação']
    for col, h in enumerate(cot_headers, 1):
        cell = ws2.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center')

    quotes = Quote.query.filter(
        Quote.created_at.between(start_dt, end_dt)
    ).order_by(Quote.created_at.desc()).all()

    for row, q in enumerate(quotes, 2):
        orig = f"{q.origin_city or ''}/{q.origin_state or ''}" if q.origin_city else q.origin_cep
        dest = f"{q.destination_city or ''}/{q.destination_state or ''}" if q.destination_city else q.destination_cep
        ws2.append([
            q.quote_number,
            q.client.company_name if q.client else '',
            orig,
            dest,
            q.status,
            float(q.driver_cost or 0),
            float(q.sale_value or 0),
            q.created_at.strftime('%d/%m/%Y') if q.created_at else ''
        ])

    for col in ws2.columns:
        ws2.column_dimensions[col[0].column_letter].width = max(len(str(col[0].value or '')), 14)

    # ── Aba 3: Pagamentos Motoristas ─────────────────────────────────────────
    ws3 = wb.create_sheet('Pagamentos')
    pay_headers = ['Motorista', 'Frete', 'Tipo', 'Valor (R$)', 'Status', 'Vencimento']
    for col, h in enumerate(pay_headers, 1):
        cell = ws3.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill

    from models import Payment as Pmt
    payments = Pmt.query.join(Freight, Pmt.freight_id == Freight.id).filter(
        Freight.created_at.between(start_dt, end_dt)
    ).order_by(Pmt.due_date).all()

    for p in payments:
        driver_name = p.driver.name if p.driver else 'N/D'
        freight_num = p.freight.freight_number if p.freight else 'N/D'
        ws3.append([
            driver_name, freight_num, p.payment_type,
            float(p.amount or 0), p.status,
            p.due_date.strftime('%d/%m/%Y') if p.due_date else ''
        ])

    for col in ws3.columns:
        ws3.column_dimensions[col[0].column_letter].width = max(len(str(col[0].value or '')), 14)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"EMALOG_Relatorio_{start_date}_{end_date}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ── API endpoints preservados (usados pelos relatórios avançados) ─────────────

@reports_bp.route('/drivers')
@login_required
def drivers_report():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'})
    try:
        start_date, end_date, start_dt, end_dt = _date_range(request)
        drivers = Driver.query.all()
        if not drivers:
            return jsonify([])

        driver_ids = [d.id for d in drivers]

        # Single aggregated query — avoids N+1
        rows = db.session.query(
            Freight.assigned_driver_id,
            func.count(Freight.id).label('total'),
            func.sum(
                db.case((Freight.status == 'entregue', 1), else_=0)
            ).label('done'),
            func.sum(
                db.case((Freight.status == 'entregue', Freight.agreed_price), else_=0)
            ).label('rev'),
        ).filter(
            Freight.assigned_driver_id.in_(driver_ids),
            Freight.created_at.between(start_dt, end_dt)
        ).group_by(Freight.assigned_driver_id).all()

        stats = {r.assigned_driver_id: r for r in rows}
        data = []
        for driver in drivers:
            r = stats.get(driver.id)
            total = r.total if r else 0
            done  = int(r.done or 0) if r else 0
            rev   = float(r.rev or 0) if r else 0.0
            data.append({
                'driver_id': driver.id, 'driver_name': driver.name,
                'total_freights': total, 'completed_freights': done,
                'completion_rate': round(done / total * 100, 2) if total else 0,
                'total_revenue': round(rev, 2),
                'avg_revenue': round(rev / done, 2) if done else 0,
            })
        return jsonify(data)
    except Exception as e:
        db.session.rollback()
        logging.error(f"drivers_report: {e}")
        return jsonify({'error': str(e)}), 500


@reports_bp.route('/clients')
@login_required
def clients_report():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'})
    try:
        start_date, end_date, start_dt, end_dt = _date_range(request)
        clients = Client.query.filter_by(active=True).all()
        if not clients:
            return jsonify([])

        client_ids = [c.id for c in clients]

        # Single aggregated queries — avoids N+1
        quote_rows = db.session.query(
            Quote.client_id,
            func.count(Quote.id).label('total'),
            func.sum(
                db.case((Quote.status.in_(['aprovada', 'aprovada_cliente']), 1), else_=0)
            ).label('approved'),
        ).filter(
            Quote.client_id.in_(client_ids),
            Quote.created_at.between(start_dt, end_dt)
        ).group_by(Quote.client_id).all()

        freight_rows = db.session.query(
            Freight.client_id,
            func.count(Freight.id).label('total'),
            func.sum(
                db.case((Freight.status == 'entregue', Freight.agreed_price), else_=0)
            ).label('spent'),
        ).filter(
            Freight.client_id.in_(client_ids),
            Freight.created_at.between(start_dt, end_dt)
        ).group_by(Freight.client_id).all()

        q_stats = {r.client_id: r for r in quote_rows}
        f_stats = {r.client_id: r for r in freight_rows}

        data = []
        for c in clients:
            qr = q_stats.get(c.id)
            fr = f_stats.get(c.id)
            tq = qr.total if qr else 0
            aq = int(qr.approved or 0) if qr else 0
            tf = fr.total if fr else 0
            spent = float(fr.spent or 0) if fr else 0.0
            data.append({
                'client_id': c.id, 'company_name': c.company_name,
                'total_quotes': tq, 'approved_quotes': aq,
                'approval_rate': round(aq / tq * 100, 2) if tq else 0,
                'total_freights': tf, 'total_spent': round(spent, 2),
            })
        return jsonify(data)
    except Exception as e:
        db.session.rollback()
        logging.error(f"clients_report: {e}")
        return jsonify({'error': str(e)}), 500


@reports_bp.route('/financial')
@login_required
def financial_report():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'})
    try:
        start_date, end_date, start_dt, end_dt = _date_range(request)
        delivered = Freight.query.filter(Freight.created_at.between(start_dt, end_dt), Freight.status == 'entregue').all()
        revenue = sum(float(f.agreed_price or 0) for f in delivered)
        advances = db.session.query(func.sum(Payment.amount)).filter(
            Payment.due_date.between(start_dt.date(), end_dt.date()),
            Payment.payment_type == 'adiantamento'
        ).scalar() or 0
        payments = db.session.query(func.sum(Payment.amount)).filter(
            Payment.due_date.between(start_dt.date(), end_dt.date()),
            Payment.payment_type == 'pagamento'
        ).scalar() or 0
        monthly = []
        cur = start_dt.replace(day=1)
        while cur <= end_dt:
            nm = cur.replace(month=cur.month % 12 + 1, year=cur.year + (cur.month // 12)) if cur.month < 12 else cur.replace(year=cur.year + 1, month=1)
            mf = Freight.query.filter(Freight.created_at.between(cur, nm - timedelta(seconds=1)), Freight.status == 'entregue').all()
            mr = sum(float(f.agreed_price or 0) for f in mf)
            monthly.append({'month': cur.strftime('%b/%Y'), 'revenue': round(mr, 2), 'advances': 0, 'profit': round(mr, 2)})
            cur = nm
        return jsonify({'summary': {'total_revenue': round(revenue, 2), 'total_advances': float(advances), 'total_payments': float(payments), 'net_profit': round(revenue - float(advances), 2)}, 'monthly_breakdown': monthly})
    except Exception as e:
        logging.error(f"financial_report: {e}")
        return jsonify({'error': str(e)}), 500


@reports_bp.route('/operational')
@login_required
def operational_report():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'})
    try:
        start_date, end_date, start_dt, end_dt = _date_range(request)
        freights = Freight.query.filter(Freight.created_at.between(start_dt, end_dt)).all()
        quotes   = Quote.query.filter(Quote.created_at.between(start_dt, end_dt)).all()
        fstatus = {}
        for f in freights:
            fstatus[f.status] = fstatus.get(f.status, 0) + 1
        qstatus = {}
        for q in quotes:
            qstatus[q.status] = qstatus.get(q.status, 0) + 1
        routes = {}
        for f in freights:
            orig = f.origin_city or f.origin or 'N/D'
            dest = f.destination_city or f.destination or 'N/D'
            key  = f'{orig} → {dest}'
            routes.setdefault(key, {'count': 0, 'total': 0})
            routes[key]['count'] += 1
            routes[key]['total'] += float(f.agreed_price or 0)
        top_routes = sorted([{'route': k, 'frequency': v['count'], 'avg_price': round(v['total'] / v['count'], 2)} for k, v in routes.items()], key=lambda x: x['frequency'], reverse=True)[:10]
        return jsonify({'freight_status': [{'status': k, 'count': v} for k, v in fstatus.items()], 'quote_status': [{'status': k, 'count': v} for k, v in qstatus.items()], 'top_routes': top_routes})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@reports_bp.route('/profitability')
@login_required
def profitability_report():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'})
    try:
        start_date, end_date, start_dt, end_dt = _date_range(request)
        # Lucratividade por cliente — uma única query GROUP BY (evita N+1)
        client_rows = (
            db.session.query(
                Client.company_name,
                db.func.sum(Freight.agreed_price).label('revenue'),
                db.func.sum(Freight.driver_cost).label('cost'),
                db.func.count(Freight.id).label('freight_count'),
            )
            .join(Freight, Freight.client_id == Client.id)
            .filter(Freight.status == 'entregue', Freight.created_at.between(start_dt, end_dt))
            .group_by(Client.id, Client.company_name)
            .order_by(db.desc(db.func.sum(Freight.agreed_price) - db.func.sum(Freight.driver_cost)))
            .limit(20)
            .all()
        )
        cp = []
        for row in client_rows:
            r = float(row.revenue or 0)
            cost = float(row.cost or 0)
            p = r - cost
            cp.append({'client_name': row.company_name, 'total_revenue': round(r, 2), 'total_cost': round(cost, 2),
                       'profit': round(p, 2), 'margin': round(p / r * 100, 2) if r else 0, 'freight_count': row.freight_count})

        # Lucratividade por motorista — uma única query GROUP BY
        driver_rows = (
            db.session.query(
                Driver.name,
                db.func.sum(Freight.agreed_price).label('revenue'),
                db.func.sum(Freight.driver_cost).label('cost'),
                db.func.count(Freight.id).label('freight_count'),
            )
            .join(Freight, Freight.assigned_driver_id == Driver.id)
            .filter(Freight.status == 'entregue', Freight.created_at.between(start_dt, end_dt))
            .group_by(Driver.id, Driver.name)
            .order_by(db.desc(db.func.sum(Freight.agreed_price) - db.func.sum(Freight.driver_cost)))
            .limit(20)
            .all()
        )
        dp = []
        for row in driver_rows:
            r = float(row.revenue or 0)
            cost = float(row.cost or 0)
            p = r - cost
            dp.append({'driver_name': row.name, 'total_revenue': round(r, 2), 'total_cost': round(cost, 2),
                       'profit': round(p, 2), 'margin': round(p / r * 100, 2) if r else 0, 'freight_count': row.freight_count})

        return jsonify({'client_profitability': cp, 'driver_profitability': dp})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@reports_bp.route('/advanced')
@login_required
def advanced_report_page():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))
    return render_template('reports/advanced.html')


@reports_bp.route('/filters-data')
@login_required
def filters_data():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'})
    try:
        states = [s[0] for s in db.session.query(Client.state).distinct().filter(Client.state.isnot(None)).all() if s[0]]
        clients = [{'id': c.id, 'name': c.company_name} for c in db.session.query(Client.id, Client.company_name).all()]
        drivers = [{'id': d.id, 'name': d.name} for d in db.session.query(Driver.id, Driver.name).all()]
        fstatus = [s[0] for s in db.session.query(Freight.status).distinct().all() if s[0]]
        vtypes  = [v[0] for v in db.session.query(Quote.vehicle_type).distinct().filter(Quote.vehicle_type.isnot(None)).all() if v[0]]
        return jsonify({'states': sorted(states), 'clients': clients, 'drivers': drivers, 'statuses': fstatus, 'vehicle_types': vtypes})
    except Exception as e:
        return jsonify({'error': str(e)})


@reports_bp.route('/states')
@login_required
def states_report():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'})
    try:
        start_date, end_date, start_dt, end_dt = _date_range(request)
        rows = db.session.query(
            Client.state,
            func.count(Freight.id).label('total_freights'),
            func.sum(func.case([(Freight.status == 'entregue', Freight.agreed_price)], else_=0)).label('total_revenue'),
            func.count(func.distinct(Client.id)).label('unique_clients')
        ).join(Freight, Client.id == Freight.client_id).filter(Client.state.isnot(None)).group_by(Client.state).order_by(func.count(Freight.id).desc()).all()
        return jsonify([{'state': r.state, 'total_freights': r.total_freights, 'total_revenue': float(r.total_revenue or 0), 'unique_clients': r.unique_clients} for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)})


@reports_bp.route('/client-details/<int:client_id>')
@login_required
def client_details(client_id):
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'})
    try:
        client   = Client.query.get_or_404(client_id)
        freights = Freight.query.filter_by(client_id=client_id).order_by(Freight.created_at.desc()).all()
        total_revenue = sum(f.agreed_price for f in freights if f.status == 'entregue' and f.agreed_price)
        return jsonify({
            'client': {'id': client.id, 'name': client.company_name},
            'total_freights': len(freights),
            'total_revenue': float(total_revenue),
            'freights': [{'freight_number': f.freight_number, 'origin': f.origin, 'destination': f.destination, 'agreed_price': float(f.agreed_price or 0), 'status': f.status, 'created_at': f.created_at.strftime('%d/%m/%Y')} for f in freights[:10]],
        })
    except Exception as e:
        return jsonify({'error': str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# AGENTE IA DE RELATÓRIOS
# ─────────────────────────────────────────────────────────────────────────────

def _build_ai_reports_context():
    """Build a rich reports context for the AI agent, covering all KPI domains."""
    now = datetime.now()
    _30d  = now - timedelta(days=30)
    _90d  = now - timedelta(days=90)
    _365d = now - timedelta(days=365)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # ── Operacional ──────────────────────────────────────────────────────────
    total_fretes   = db.session.query(func.count(Freight.id)).scalar() or 0
    fretes_30d     = db.session.query(func.count(Freight.id)).filter(Freight.created_at >= _30d).scalar() or 0
    fretes_mes     = db.session.query(func.count(Freight.id)).filter(Freight.created_at >= month_start).scalar() or 0
    fretes_ativos  = db.session.query(func.count(Freight.id)).filter(
        Freight.status.in_(['aceito', 'em_coleta', 'em_transito'])
    ).scalar() or 0
    fretes_entregues = db.session.query(func.count(Freight.id)).filter(Freight.status == 'entregue').scalar() or 0
    fretes_cancel  = db.session.query(func.count(Freight.id)).filter(Freight.status == 'cancelado').scalar() or 0

    receita_mes    = db.session.query(func.sum(Freight.agreed_price)).filter(
        Freight.created_at >= month_start,
        Freight.status.in_(['aceito', 'em_coleta', 'em_transito', 'entregue'])
    ).scalar() or 0
    custo_mes      = db.session.query(func.sum(Freight.driver_cost)).filter(
        Freight.created_at >= month_start,
        Freight.status.in_(['aceito', 'em_coleta', 'em_transito', 'entregue'])
    ).scalar() or 0
    margem_mes     = float(receita_mes) - float(custo_mes)
    margem_pct     = round(margem_mes / float(receita_mes) * 100, 1) if receita_mes else 0

    receita_total  = db.session.query(func.sum(Freight.agreed_price)).filter(
        Freight.status.in_(['aceito', 'em_coleta', 'em_transito', 'entregue'])
    ).scalar() or 0
    custo_total    = db.session.query(func.sum(Freight.driver_cost)).filter(
        Freight.status.in_(['aceito', 'em_coleta', 'em_transito', 'entregue'])
    ).scalar() or 0

    # ── Top Clientes (30d) ──────────────────────────────────────────────────
    top_cli = db.session.query(
        Client.company_name,
        func.count(Freight.id).label('qtd'),
        func.sum(Freight.agreed_price).label('rec'),
        func.sum(Freight.driver_cost).label('cst')
    ).join(Freight, Client.id == Freight.client_id).filter(
        Freight.created_at >= _30d,
        Freight.status.in_(['aceito', 'em_coleta', 'em_transito', 'entregue'])
    ).group_by(Client.company_name).order_by(func.sum(Freight.agreed_price).desc()).limit(8).all()

    top_cli_lines = []
    for c in top_cli:
        rec = float(c.rec or 0)
        cst = float(c.cst or 0)
        mrg = round((rec - cst) / rec * 100, 1) if rec else 0
        top_cli_lines.append(f"  {c.company_name}: {c.qtd} fretes | Receita R${rec:,.0f} | Margem {mrg}%")

    # ── Top Rotas (30d) ─────────────────────────────────────────────────────
    top_rotas = db.session.query(
        func.coalesce(Freight.origin_city, Freight.origin).label('orig'),
        func.coalesce(Freight.destination_city, Freight.destination).label('dest'),
        func.count(Freight.id).label('freq'),
        func.avg(Freight.agreed_price).label('avg_price')
    ).filter(
        Freight.created_at >= _30d
    ).group_by('orig', 'dest').order_by(func.count(Freight.id).desc()).limit(6).all()

    rota_lines = [f"  {r.orig} → {r.dest}: {r.freq} viagens | Preço médio R${float(r.avg_price or 0):,.0f}"
                  for r in top_rotas if r.orig and r.dest]

    # ── Motoristas ──────────────────────────────────────────────────────────
    mot_ativos  = db.session.query(func.count(Driver.id)).filter(Driver.active == True).scalar() or 0
    mot_disp    = db.session.query(func.count(Driver.id)).filter(Driver.active == True, Driver.availability_status == 'disponivel').scalar() or 0
    mot_em_frete= db.session.query(func.count(Driver.id)).filter(Driver.active == True, Driver.availability_status == 'em_frete').scalar() or 0

    # ── Cotações ────────────────────────────────────────────────────────────
    cot_total     = db.session.query(func.count(Quote.id)).scalar() or 0
    cot_pendentes = db.session.query(func.count(Quote.id)).filter(Quote.status == 'pendente').scalar() or 0
    cot_aprovadas = db.session.query(func.count(Quote.id)).filter(Quote.status == 'aprovada').scalar() or 0
    cot_negadas   = db.session.query(func.count(Quote.id)).filter(Quote.status == 'negada').scalar() or 0
    taxa_conv_cot = round(cot_aprovadas / cot_total * 100, 1) if cot_total else 0

    # ── Financeiro ──────────────────────────────────────────────────────────
    pag_pendentes = db.session.query(func.count(Payment.id)).filter(Payment.status == 'pendente').scalar() or 0
    pag_valor     = db.session.query(func.sum(Payment.amount)).filter(Payment.status == 'pendente').scalar() or 0
    pag_pagos_mes = db.session.query(func.sum(Payment.amount)).filter(
        Payment.status == 'pago', Payment.created_at >= month_start
    ).scalar() or 0

    # ── CRM & Leads ─────────────────────────────────────────────────────────
    try:
        leads_total  = db.session.query(func.count(Lead.id)).scalar() or 0
        leads_30d    = db.session.query(func.count(Lead.id)).filter(Lead.created_at >= _30d).scalar() or 0
        leads_conv   = db.session.query(func.count(Lead.id)).filter(Lead.status == 'convertido').scalar() or 0
        taxa_conv_lead = round(leads_conv / leads_total * 100, 1) if leads_total else 0

        stages_raw = db.session.query(
            Lead.status, func.count(Lead.id)
        ).group_by(Lead.status).all()
        stages_lines = [f"  {s}: {n}" for s, n in stages_raw if s]

        opp_total  = db.session.query(func.count(Opportunity.id)).scalar() or 0
        opp_ganhas = db.session.query(func.count(Opportunity.id)).filter(Opportunity.status == 'ganho').scalar() or 0
        opp_perdidas= db.session.query(func.count(Opportunity.id)).filter(Opportunity.status == 'perdido').scalar() or 0
        opp_valor  = db.session.query(func.sum(Opportunity.value)).filter(
            Opportunity.status == 'ganho'
        ).scalar() or 0
    except Exception:
        leads_total = leads_30d = leads_conv = taxa_conv_lead = 0
        stages_lines = []
        opp_total = opp_ganhas = opp_perdidas = opp_valor = 0

    # ── RFM Carteira ────────────────────────────────────────────────────────
    try:
        cli_fretes = db.session.query(
            Client.id,
            Client.company_name,
            func.max(Freight.created_at).label('last_freight'),
            func.count(Freight.id).label('freq'),
            func.sum(Freight.agreed_price).label('valor')
        ).join(Freight, Client.id == Freight.client_id).filter(
            Freight.status == 'entregue'
        ).group_by(Client.id, Client.company_name).all()

        rfm_counts = {'Campeão': 0, 'Fiel': 0, 'Em risco': 0, 'Dormindo': 0, 'Perdido': 0}
        dormentes_names = []
        for row in cli_fretes:
            days_ago = (now - row.last_freight).days if row.last_freight else 9999
            r = 3 if days_ago <= 30 else (2 if days_ago <= 90 else 1)
            f = 3 if row.freq >= 5 else (2 if row.freq >= 2 else 1)
            m = 3 if float(row.valor or 0) >= 10000 else (2 if float(row.valor or 0) >= 3000 else 1)
            score = r + f + m
            seg = 'Campeão' if score >= 8 else ('Fiel' if score >= 6 else ('Em risco' if score >= 4 else ('Dormindo' if score >= 2 else 'Perdido')))
            rfm_counts[seg] += 1
            if seg in ('Dormindo', 'Perdido'):
                dormentes_names.append(row.company_name)
        rfm_lines = [f"  {k}: {v} clientes" for k, v in rfm_counts.items() if v > 0]
        dorm_preview = dormentes_names[:5]
    except Exception:
        rfm_lines = []
        dorm_preview = []

    # ── SLA ─────────────────────────────────────────────────────────────────
    try:
        sla_log = aliased(FreightStatusLog)
        sla_rows = db.session.query(
            Freight.id,
            Freight.delivery_date,
            func.min(FreightStatusLog.created_at).label('delivered_at')
        ).join(FreightStatusLog, FreightStatusLog.freight_id == Freight.id).filter(
            FreightStatusLog.new_status == 'entregue',
            Freight.delivery_date.isnot(None),
            Freight.created_at >= _90d
        ).group_by(Freight.id, Freight.delivery_date).all()

        sla_on = sum(1 for r in sla_rows if r.delivered_at and r.delivered_at.date() <= r.delivery_date)
        sla_tot = len(sla_rows)
        sla_taxa = round(sla_on / sla_tot * 100, 1) if sla_tot else 0
    except Exception:
        sla_on = sla_tot = 0
        sla_taxa = 0

    # ── Monta contexto ──────────────────────────────────────────────────────
    ctx = f"""Você é o Agente de Análise de Relatórios da EMALOG, especializado em interpretar indicadores de logística.
Responda SEMPRE em português brasileiro, de forma clara, objetiva e acessível — o usuário pode não ter formação técnica.
Use formatação com marcadores quando listar dados. Seja analítico: aponte tendências, riscos e oportunidades quando pertinente.
Nunca invente dados. Se não souber, diga claramente.

=== SNAPSHOT DOS INDICADORES — {now.strftime('%d/%m/%Y %H:%M')} ===

📦 OPERACIONAL
  Total histórico de fretes: {total_fretes:,}
  Fretes nos últimos 30 dias: {fretes_30d}
  Fretes neste mês: {fretes_mes}
  Em andamento agora (aceito/em trânsito/em coleta): {fretes_ativos}
  Entregues (total): {fretes_entregues}
  Cancelados (total): {fretes_cancel}

💰 FINANCEIRO (mês atual)
  Receita bruta: R${float(receita_mes):,.2f}
  Custo com motoristas: R${float(custo_mes):,.2f}
  Margem bruta: R${margem_mes:,.2f} ({margem_pct}%)
  Receita acumulada total: R${float(receita_total):,.2f}
  Custo acumulado total: R${float(custo_total):,.2f}
  Pagamentos pendentes p/ motoristas: {pag_pendentes} pagamentos | R${float(pag_valor):,.2f}
  Pago a motoristas neste mês: R${float(pag_pagos_mes):,.2f}

🏆 TOP CLIENTES (últimos 30 dias por receita)
{chr(10).join(top_cli_lines) if top_cli_lines else '  Nenhum dado disponível'}

🗺️ TOP ROTAS (últimos 30 dias)
{chr(10).join(rota_lines) if rota_lines else '  Nenhum dado disponível'}

🚛 MOTORISTAS
  Ativos na plataforma: {mot_ativos}
  Disponíveis agora: {mot_disp}
  Em frete agora: {mot_em_frete}

📋 COTAÇÕES
  Total de cotações: {cot_total}
  Pendentes (aguardando aprovação): {cot_pendentes}
  Aprovadas: {cot_aprovadas}
  Negadas: {cot_negadas}
  Taxa de conversão: {taxa_conv_cot}%

🎯 CRM & LEADS
  Total de leads: {leads_total}
  Novos leads (30 dias): {leads_30d}
  Leads convertidos: {leads_conv} ({taxa_conv_lead}%)
  Estágios do funil:
{chr(10).join(stages_lines) if stages_lines else '  Sem dados de estágio'}
  Oportunidades: {opp_total} | Ganhas: {opp_ganhas} | Perdidas: {opp_perdidas}
  Valor ganho em oportunidades: R${float(opp_valor):,.2f}

👥 CARTEIRA DE CLIENTES (segmentação RFM)
{chr(10).join(rfm_lines) if rfm_lines else '  Sem dados de segmentação'}
  Clientes dormentes/perdidos recentes: {', '.join(dorm_preview) if dorm_preview else 'nenhum identificado'}

📊 SLA OPERACIONAL (últimos 90 dias)
  Fretes com prazo avaliado: {sla_tot}
  Entregues no prazo: {sla_on} ({sla_taxa}%)
  Entregues com atraso: {sla_tot - sla_on}

=== FIM DO SNAPSHOT ===

Responda a pergunta do usuário com base nesses indicadores. Se quiser sugerir ações ou alertas relevantes, faça isso ao final da sua resposta.
"""
    return ctx


_gemini_client_reports = None


def _get_gemini():
    global _gemini_client_reports
    if _gemini_client_reports is None:
        try:
            from google import genai
            _gemini_client_reports = genai.Client(
                api_key=os.environ.get("AI_INTEGRATIONS_GEMINI_API_KEY"),
                http_options={
                    'api_version': '',
                    'base_url': os.environ.get("AI_INTEGRATIONS_GEMINI_BASE_URL")
                }
            )
        except Exception as e:
            logging.error(f"Gemini client init error: {e}")
    return _gemini_client_reports


@reports_bp.route('/ai-chat', methods=['POST'])
@login_required
def ai_chat():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'}), 403

    data = request.get_json()
    if not data or not data.get('message'):
        return jsonify({'error': 'Mensagem vazia'}), 400

    user_message = data['message'].strip()[:1000]

    try:
        from google.genai import types

        context = _build_ai_reports_context()
        client  = _get_gemini()
        if not client:
            return jsonify({'error': 'Serviço de IA indisponível no momento.'}), 503

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[types.Content(role='user', parts=[types.Part(text=user_message)])],
            config=types.GenerateContentConfig(
                system_instruction=context,
                max_output_tokens=4096
            )
        )
        answer = response.text or 'Não foi possível gerar uma resposta.'
        return jsonify({'answer': answer})

    except Exception as e:
        error_msg = str(e)
        logging.error(f"Reports AI chat erro: {error_msg}")
        if 'FREE_CLOUD_BUDGET_EXCEEDED' in error_msg:
            return jsonify({'error': 'Orçamento de IA excedido. Verifique seu plano no Replit.'}), 429
        return jsonify({'error': f'Erro ao consultar IA: {error_msg}'}), 500
