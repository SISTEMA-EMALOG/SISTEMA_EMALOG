from flask import Blueprint, render_template, jsonify, redirect, url_for, flash
from flask_login import login_required, current_user
from models import Driver, Client, Quote, Freight, Payment, FreightStatusLog
from app import db
from sqlalchemy import func, and_
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta, date
from collections import Counter
import calendar
import logging
import json

dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/dashboard')

@dashboard_bp.route('/')
@login_required
def index():
    # Redirecionar clientes para dashboard específico
    if current_user.role == 'cliente':
        return redirect(url_for('dashboard.client_dashboard'))

    # ── Todas as contagens em UMA query usando CASE WHEN ─────────────────────
    from sqlalchemy import case as sa_case, text as sa_text
    q_counts = db.session.query(
        func.count(Quote.id).label('total_quotes'),
        func.sum(sa_case((Quote.status == 'pendente', 1), else_=0)).label('pending'),
        func.sum(sa_case((Quote.status.in_(['cotada', 'negociacao']), 1), else_=0)).label('awaiting'),
        func.sum(sa_case((Quote.status.in_(['aprovada', 'aprovada_cliente']), 1), else_=0)).label('aprovada'),
        func.sum(sa_case((Quote.status.in_(['rejeitada', 'negado']), 1), else_=0)).label('rejeitada'),
        func.sum(sa_case((Quote.status == 'cotada', 1), else_=0)).label('cotada'),
        func.sum(sa_case((Quote.status == 'negociacao', 1), else_=0)).label('negociacao'),
    ).one()
    f_counts = db.session.query(
        func.count(Freight.id).label('total'),
        func.sum(sa_case((Freight.status.in_(['ofertado', 'aceito', 'em_transito']), 1), else_=0)).label('active'),
    ).one()

    stats = {
        'total_drivers':        Driver.query.filter_by(active=True).count(),
        'total_clients':        Client.query.filter_by(active=True).count(),
        'total_quotes':         int(q_counts.total_quotes or 0),
        'pending_quotes':       int(q_counts.pending or 0),
        'awaiting_client_quotes': int(q_counts.awaiting or 0),
        'active_freights':      int(f_counts.active or 0),
    }

    pending_quotes       = stats['pending_quotes']
    awaiting_client_quotes = stats['awaiting_client_quotes']
    urgent_quotes = Quote.query.options(joinedload(Quote.client)).filter_by(status='pendente').order_by(Quote.created_at.asc()).limit(3).all()

    # Recent activities — com joinedload para evitar N+1
    recent_quotes   = Quote.query.options(joinedload(Quote.client)).order_by(Quote.created_at.desc()).limit(5).all()
    recent_freights = Freight.query.options(joinedload(Freight.client)).order_by(Freight.created_at.desc()).limit(5).all()

    # ── Receita mensal — UMA query GROUP BY ao invés de 6 loops ──────────────
    # date_trunc é função só de PostgreSQL — o ambiente atual roda em SQLite
    # (fallback, sem DATABASE_URL configurada). strftime é o equivalente no
    # SQLite. Mantido dialect-aware para não quebrar de novo quando migrarem
    # para um banco gerenciado.
    six_months_ago = (datetime.now().replace(day=1) - timedelta(days=150)).replace(day=1)
    if db.engine.dialect.name == 'sqlite':
        month_expr = func.strftime('%Y-%m', Freight.created_at)
    else:
        month_expr = func.date_trunc('month', Freight.created_at)

    monthly_rows = db.session.query(
        month_expr.label('month'),
        func.sum(Freight.agreed_price).label('revenue')
    ).filter(
        Freight.status == 'entregue',
        Freight.created_at >= six_months_ago
    ).group_by(month_expr
    ).order_by(month_expr).all()

    # Garante os últimos 6 meses mesmo sem dados
    # SQLite (strftime) já devolve string 'YYYY-MM'; PostgreSQL (date_trunc)
    # devolve datetime — trata os dois formatos.
    monthly_map = {
        (row.month if isinstance(row.month, str) else row.month.strftime('%Y-%m')): float(row.revenue or 0)
        for row in monthly_rows
    }
    monthly_revenue = []
    for i in range(5, -1, -1):
        d = (datetime.now().replace(day=1) - timedelta(days=30 * i)).replace(day=1)
        key = d.strftime('%Y-%m')
        monthly_revenue.append({'month': d.strftime('%b/%Y'), 'revenue': monthly_map.get(key, 0.0)})

    # ── Funil de Cotações — reutiliza q_counts já calculado ──────────────────
    funnel_statuses = ['pendente', 'cotada', 'negociacao', 'aprovada', 'rejeitada']
    funnel_labels_map = {
        'pendente': 'Pendente', 'cotada': 'Cotada/Enviada',
        'negociacao': 'Em Negociação', 'aprovada': 'Aprovada', 'rejeitada': 'Rejeitada'
    }
    funnel_data = {
        'pendente':   {'label': 'Pendente',         'count': int(q_counts.pending or 0)},
        'cotada':     {'label': 'Cotada/Enviada',   'count': int(q_counts.cotada or 0)},
        'negociacao': {'label': 'Em Negociação',    'count': int(q_counts.negociacao or 0)},
        'aprovada':   {'label': 'Aprovada',         'count': int(q_counts.aprovada or 0)},
        'rejeitada':  {'label': 'Rejeitada',        'count': int(q_counts.rejeitada or 0)},
    }

    quote_funnel = json.dumps({
        'labels': [funnel_data[s]['label'] for s in funnel_statuses],
        'data':   [funnel_data[s]['count'] for s in funnel_statuses],
    })

    # ── Alertas Operacionais ───────────────────────────────────────────────────
    alerts = []
    cutoff_24h = datetime.utcnow() - timedelta(hours=24)
    cutoff_48h = datetime.utcnow() - timedelta(hours=48)

    # Fretes ofertados sem motorista há mais de 24h
    sem_motorista = Freight.query.filter(
        Freight.status == 'ofertado',
        Freight.created_at <= cutoff_24h
    ).count()
    if sem_motorista:
        alerts.append({'type': 'warning', 'icon': 'fa-truck',
                        'msg': f'{sem_motorista} frete(s) sem motorista há mais de 24h',
                        'link': '/freight/?status=ofertado'})

    # Cotações pendentes há mais de 48h
    cot_antigas = Quote.query.filter(
        Quote.status == 'pendente',
        Quote.created_at <= cutoff_48h
    ).count()
    if cot_antigas:
        alerts.append({'type': 'danger', 'icon': 'fa-file-invoice',
                        'msg': f'{cot_antigas} cotação(ões) sem resposta há mais de 48h',
                        'link': '/quotes/?status=pendente'})

    # Fretes em trânsito com prazo de entrega vencido
    hoje = datetime.utcnow().date()
    atrasados = Freight.query.filter(
        Freight.status == 'em_transito',
        Freight.delivery_date < hoje
    ).count()
    if atrasados:
        alerts.append({'type': 'danger', 'icon': 'fa-exclamation-circle',
                        'msg': f'{atrasados} frete(s) em trânsito com prazo de entrega vencido',
                        'link': '/freight/?status=em_transito'})

    return render_template('dashboard/index.html',
                           stats=stats,
                           pending_quotes=pending_quotes,
                           awaiting_client_quotes=awaiting_client_quotes,
                           urgent_quotes=urgent_quotes,
                           recent_quotes=recent_quotes,
                           recent_freights=recent_freights,
                           monthly_revenue=monthly_revenue,
                           quote_funnel=quote_funnel,
                           alerts=alerts)

@dashboard_bp.route('/api/stats')
@login_required
def api_stats():
    """API endpoint for dashboard statistics"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'}), 403

    # Freight status distribution
    freight_stats = db.session.query(
        Freight.status,
        func.count(Freight.id).label('count')
    ).group_by(Freight.status).all()

    # Driver activity (freights per driver)
    driver_activity = db.session.query(
        Driver.name,
        func.count(Freight.id).label('freight_count')
    ).join(Freight, Driver.id == Freight.assigned_driver_id)\
     .group_by(Driver.id, Driver.name)\
     .order_by(func.count(Freight.id).desc())\
     .limit(10).all()

    return jsonify({
        'freight_status': [{'status': stat.status, 'count': stat.count} for stat in freight_stats],
        'driver_activity': [{'name': activity.name, 'count': activity.freight_count} for activity in driver_activity]
    })

@dashboard_bp.route('/api/dashboard-stats')
@login_required
def api_dashboard_stats():
    """API endpoint for main dashboard statistics"""
    try:
        from sqlalchemy import case
        pending_count = Quote.query.filter_by(status='pendente').count()
        stats = {
            'drivers':        Driver.query.filter_by(active=True).count(),
            'clients':        Client.query.filter_by(active=True).count(),
            'quotes':         pending_count,
            'total_quotes':   Quote.query.count(),
            'pending_quotes': pending_count,
            'freights':       Freight.query.filter(Freight.status.in_(['ofertado', 'aceito', 'em_transito'])).count()
        }
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        logging.error(f"Erro ao carregar stats do dashboard: {e}")
        return jsonify({'success': False, 'error': str(e), 'stats': {
            'drivers': 0, 'clients': 0, 'quotes': 0,
            'total_quotes': 0, 'pending_quotes': 0, 'freights': 0
        }})

@dashboard_bp.route('/client')
@login_required
def client_dashboard():
    """Dashboard específico para clientes"""
    if current_user.role != 'cliente':
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))

    if not current_user.client_id:
        flash('Usuário não está associado a nenhum cliente.', 'error')
        return redirect(url_for('auth.logout'))

    client = Client.query.get_or_404(current_user.client_id)
    cid = current_user.client_id
    today = date.today()
    month_start = today.replace(day=1)

    # ── Fretes ──────────────────────────────────────────────────────────────
    freights = (Freight.query
                .filter_by(client_id=cid)
                .options(joinedload(Freight.quote), joinedload(Freight.assigned_driver))
                .order_by(Freight.created_at.desc())
                .all())

    delivered   = [f for f in freights if f.status == 'entregue']
    cancelled   = [f for f in freights if f.status == 'cancelado']
    in_transit  = [f for f in freights if f.status == 'em_transito']
    active_list = [f for f in freights if f.status not in ['entregue', 'cancelado']]
    this_month  = [f for f in freights if f.pickup_date and f.pickup_date >= month_start]

    # On-time rate
    on_time_count = 0
    late_count    = 0
    for f in delivered:
        if f.delivery_date:
            log = (FreightStatusLog.query
                   .filter_by(freight_id=f.id, new_status='entregue')
                   .order_by(FreightStatusLog.created_at.desc())
                   .first())
            actual = log.created_at.date() if log else (f.updated_at.date() if f.updated_at else None)
            if actual:
                if actual <= f.delivery_date:
                    on_time_count += 1
                else:
                    late_count += 1
            else:
                on_time_count += 1
    on_time_rate = round(on_time_count / len(delivered) * 100) if delivered else 0

    # Revenue
    total_revenue = sum(f.agreed_price or 0 for f in freights)
    month_revenue = sum(f.agreed_price or 0 for f in this_month)
    avg_ticket    = round(total_revenue / len(freights), 2) if freights else 0

    # Top routes
    route_counter = Counter()
    for f in freights:
        if f.origin_city and f.destination_city:
            o = f"{f.origin_city}/{f.origin_state}" if f.origin_state else f.origin_city
            d = f"{f.destination_city}/{f.destination_state}" if f.destination_state else f.destination_city
            route_counter[f"{o} → {d}"] += 1
    top_routes = route_counter.most_common(6)

    # Load type
    load_type_counter = Counter()
    for f in freights:
        lt = (f.quote.load_type if f.quote else None) or 'desconhecido'
        load_type_counter[lt] += 1

    # Vehicle type
    vehicle_counter = Counter()
    for f in freights:
        vt = (f.quote.vehicle_type if f.quote else None) or 'desconhecido'
        vehicle_counter[vt] += 1

    # Monthly data — last 12 months
    monthly_labels   = []
    monthly_volumes  = []
    monthly_revenues = []
    cur_year  = today.year
    cur_month = today.month
    for i in range(11, -1, -1):
        m = cur_month - i
        y = cur_year
        while m <= 0:
            m += 12
            y -= 1
        monthly_labels.append(f"{calendar.month_abbr[m]}/{str(y)[2:]}")
        cnt = sum(1 for f in freights if f.pickup_date and f.pickup_date.year == y and f.pickup_date.month == m)
        rev = sum(f.agreed_price or 0 for f in freights if f.pickup_date and f.pickup_date.year == y and f.pickup_date.month == m)
        monthly_volumes.append(cnt)
        monthly_revenues.append(round(rev, 2))

    # Status distribution
    status_dist = {
        'Ofertado':    sum(1 for f in freights if f.status == 'ofertado'),
        'Aceito':      sum(1 for f in freights if f.status == 'aceito'),
        'Em Trânsito': len(in_transit),
        'Entregue':    len(delivered),
        'Cancelado':   len(cancelled),
    }

    # ── Cotações ────────────────────────────────────────────────────────────
    total_quotes   = Quote.query.filter_by(client_id=cid).count()
    # Aguardando precificação pelo operador
    pending_quotes = Quote.query.filter_by(client_id=cid, status='pendente').count()
    # Precificadas pelo operador — aguardando aprovação/contraproposta/rejeição do cliente
    awaiting_approval_quotes = Quote.query.filter_by(client_id=cid).filter(
        Quote.status.in_(['cotada', 'negociacao'])).count()
    approved_quotes = Quote.query.filter_by(client_id=cid, status='aprovada').count()
    rejected_quotes = Quote.query.filter_by(client_id=cid).filter(
        Quote.status.in_(['rejeitada', 'rejeitado'])).count()

    recent_quotes = (Quote.query.filter_by(client_id=cid)
                     .order_by(Quote.created_at.desc()).limit(8).all())

    logging.info(f"📊 Dashboard do cliente {client.company_name}: fretes={len(freights)}, no_prazo={on_time_rate}%")

    return render_template('dashboard/client.html',
        client=client,
        freights=freights,
        total_freights=len(freights),
        delivered_count=len(delivered),
        cancelled_count=len(cancelled),
        active_count=len(active_list),
        in_transit_count=len(in_transit),
        this_month_count=len(this_month),
        on_time_count=on_time_count,
        late_count=late_count,
        on_time_rate=on_time_rate,
        total_revenue=total_revenue,
        month_revenue=month_revenue,
        avg_ticket=avg_ticket,
        top_routes=top_routes,
        load_type_counter=dict(load_type_counter),
        vehicle_counter=dict(vehicle_counter),
        status_dist=status_dist,
        monthly_labels=json.dumps(monthly_labels),
        monthly_volumes=json.dumps(monthly_volumes),
        monthly_revenues=json.dumps(monthly_revenues),
        total_quotes=total_quotes,
        pending_quotes=pending_quotes,
        awaiting_approval_quotes=awaiting_approval_quotes,
        approved_quotes=approved_quotes,
        rejected_quotes=rejected_quotes,
        recent_quotes=recent_quotes,
        today=today,
    )

@dashboard_bp.route('/client/api/stats')
@login_required
def client_stats_api():
    """API para estatísticas atualizadas do cliente"""
    if current_user.role != 'cliente' or not current_user.client_id:
        return jsonify({'error': 'Acesso negado'}), 403

    # Forçar recarregamento
    db.session.expire_all()

    # Buscar estatísticas atualizadas
    stats = {
        'total_quotes': Quote.query.filter_by(client_id=current_user.client_id).count(),
        'pending_quotes': Quote.query.filter_by(client_id=current_user.client_id).filter(
            Quote.status.in_(['pendente'])
        ).count(),
        'approved_quotes': Quote.query.filter_by(client_id=current_user.client_id, status='aprovado_cliente').count(),
        'rejected_quotes': Quote.query.filter_by(client_id=current_user.client_id, status='rejeitado').count(),
        'total_freights': Freight.query.filter_by(client_id=current_user.client_id).count(),
        'active_freights': Freight.query.filter_by(client_id=current_user.client_id).filter(
            Freight.status.in_(['coletado', 'em_transito', 'aguardando_entrega'])
        ).count(),
        'completed_freights': Freight.query.filter_by(client_id=current_user.client_id, status='entregue').count()
    }

    return jsonify(stats)


# ── Página de Relatórios do cliente ──────────────────────────────────────────
@dashboard_bp.route('/client/reports')
@login_required
def client_reports():
    """Relatórios financeiros e operacionais — visível apenas quando o cliente acessa explicitamente"""
    if current_user.role != 'cliente' or not current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))

    from flask import request as req
    cid    = current_user.client_id
    client = Client.query.get_or_404(cid)
    today  = date.today()

    # Filtro de período via ?period=
    period = req.args.get('period', 'all')
    period_map = {'1m': 1, '3m': 3, '6m': 6, '12m': 12}
    if period in period_map:
        cutoff = today - timedelta(days=30 * period_map[period])
    else:
        cutoff = None

    q = (Freight.query
         .filter_by(client_id=cid)
         .options(joinedload(Freight.quote), joinedload(Freight.assigned_driver))
         .order_by(Freight.created_at.desc()))
    if cutoff:
        q = q.filter(Freight.created_at >= cutoff)
    freights = q.all()

    delivered   = [f for f in freights if f.status == 'entregue']
    in_transit  = [f for f in freights if f.status == 'em_transito']
    cancelled   = [f for f in freights if f.status == 'cancelado']
    month_start = today.replace(day=1)
    this_month  = [f for f in freights if f.pickup_date and f.pickup_date >= month_start]

    # On-time
    on_time_count = late_count = 0
    for f in delivered:
        if f.delivery_date:
            log = (FreightStatusLog.query
                   .filter_by(freight_id=f.id, new_status='entregue')
                   .order_by(FreightStatusLog.created_at.desc()).first())
            actual = log.created_at.date() if log else (f.updated_at.date() if hasattr(f, 'updated_at') and f.updated_at else None)
            if actual:
                on_time_count += 1 if actual <= f.delivery_date else 0
                late_count    += 0 if actual <= f.delivery_date else 1
            else:
                on_time_count += 1
    on_time_rate = round(on_time_count / len(delivered) * 100) if delivered else 0

    # Financeiro
    total_revenue = sum(float(f.agreed_price or 0) for f in freights)
    month_revenue = sum(float(f.agreed_price or 0) for f in this_month)
    avg_ticket    = round(total_revenue / len(freights), 2) if freights else 0

    # Histórico mensal com receita — para relatórios financeiros
    monthly_rows = []
    cur_year, cur_month = today.year, today.month
    monthly_labels, monthly_volumes, monthly_revenues = [], [], []
    for i in range(11, -1, -1):
        m = cur_month - i
        y = cur_year
        while m <= 0:
            m += 12; y -= 1
        lbl = f"{calendar.month_abbr[m]}/{str(y)[2:]}"
        cnt = sum(1 for f in freights if f.pickup_date and f.pickup_date.year == y and f.pickup_date.month == m)
        rev = sum(float(f.agreed_price or 0) for f in freights if f.pickup_date and f.pickup_date.year == y and f.pickup_date.month == m)
        monthly_labels.append(lbl)
        monthly_volumes.append(cnt)
        monthly_revenues.append(round(rev, 2))
        monthly_rows.append({'label': lbl, 'count': cnt, 'revenue': round(rev, 2)})

    # Top routes
    route_counter = Counter()
    for f in freights:
        if f.origin_city and f.destination_city:
            o = f"{f.origin_city}/{f.origin_state}" if f.origin_state else f.origin_city
            d = f"{f.destination_city}/{f.destination_state}" if f.destination_state else f.destination_city
            route_counter[f"{o} → {d}"] += 1
    top_routes = route_counter.most_common(8)

    load_type_counter = Counter()
    vehicle_counter   = Counter()
    for f in freights:
        load_type_counter[(f.quote.load_type    if f.quote else None) or 'desconhecido'] += 1
        vehicle_counter  [(f.quote.vehicle_type if f.quote else None) or 'desconhecido'] += 1

    total_quotes   = Quote.query.filter_by(client_id=cid).count()
    pending_quotes = Quote.query.filter_by(client_id=cid, status='pendente').count()
    awaiting_approval_quotes = Quote.query.filter_by(client_id=cid).filter(
        Quote.status.in_(['cotada', 'negociacao'])).count()

    # ── Gastos por trimestre (últimos 4 trimestres) ──────────────────────────
    all_freights_12m = Freight.query.filter(
        Freight.client_id == cid,
        Freight.created_at >= (today - timedelta(days=365)),
        Freight.status != 'cancelado',
    ).order_by(Freight.created_at).all()

    def _quarter_label(d):
        q = (d.month - 1) // 3 + 1
        return f"T{q}/{str(d.year)[2:]}"

    quarterly = {}
    for f in all_freights_12m:
        k = _quarter_label(f.created_at.date() if f.created_at else today)
        if k not in quarterly:
            quarterly[k] = {'count': 0, 'total': 0.0}
        quarterly[k]['count'] += 1
        quarterly[k]['total'] += float(f.agreed_price or 0)

    quarterly_list = [
        {'label': k, 'count': v['count'], 'total': round(v['total'], 2),
         'avg': round(v['total'] / v['count'], 2) if v['count'] else 0}
        for k, v in quarterly.items()
    ]

    # Gastos por rota (top 8)
    route_spend = {}
    for f in freights:
        if f.origin_city and f.destination_city:
            o = f"{f.origin_city}/{f.origin_state}" if f.origin_state else f.origin_city
            d = f"{f.destination_city}/{f.destination_state}" if f.destination_state else f.destination_city
            k = f"{o} → {d}"
        else:
            k = "Rota N/D"
        route_spend.setdefault(k, {'count': 0, 'total': 0.0})
        route_spend[k]['count'] += 1
        route_spend[k]['total'] += float(f.agreed_price or 0)

    spend_by_route = sorted(
        [{'rota': k, 'count': v['count'], 'total': round(v['total'], 2)}
         for k, v in route_spend.items()],
        key=lambda x: x['total'], reverse=True
    )[:8]

    # ── Cotações expandidas ──────────────────────────────────────────────────
    all_quotes = Quote.query.filter_by(client_id=cid).order_by(Quote.created_at.desc()).all()
    aprovadas_q   = sum(1 for q in all_quotes if q.status in ('aprovada', 'aprovada_cliente'))
    negadas_q     = sum(1 for q in all_quotes if q.status == 'negada')
    pendentes_q   = sum(1 for q in all_quotes if q.status in ('pendente', 'cotada', 'negociacao'))

    # Tempo médio de resposta (dias entre criação e atualização para cotações respondidas)
    respondidas = [q for q in all_quotes if q.status not in ('pendente',) and q.updated_at and q.created_at]
    if respondidas:
        total_dias = sum((q.updated_at - q.created_at).days for q in respondidas)
        tempo_medio_resposta = round(total_dias / len(respondidas), 1)
    else:
        tempo_medio_resposta = None

    # Histórico recente de cotações (últimas 10)
    cotacoes_recentes = all_quotes[:10]

    # ── SLA detalhado (timeline dos últimos 15 entregues) ───────────────────
    sla_timeline = []
    for f in delivered[:15]:
        log = (FreightStatusLog.query
               .filter_by(freight_id=f.id, new_status='entregue')
               .order_by(FreightStatusLog.created_at.desc()).first())
        actual = log.created_at.date() if log else (f.updated_at.date() if f.updated_at else None)
        if f.delivery_date and actual:
            delta = (actual - f.delivery_date).days
            on_time = delta <= 0
        else:
            delta = None
            on_time = None
        rota = ''
        if f.origin_city and f.destination_city:
            rota = f"{f.origin_city} → {f.destination_city}"
        sla_timeline.append({
            'numero': f.freight_number,
            'rota': rota or f"{f.origin[:20] if f.origin else ''} → {f.destination[:20] if f.destination else ''}",
            'expected': f.delivery_date.strftime('%d/%m/%y') if f.delivery_date else '—',
            'actual': actual.strftime('%d/%m/%y') if actual else '—',
            'delta': delta,
            'on_time': on_time,
        })

    chart_quarterly = json.dumps({
        'labels': [q['label'] for q in quarterly_list],
        'totals': [q['total'] for q in quarterly_list],
        'counts': [q['count'] for q in quarterly_list],
    })
    chart_route_spend = json.dumps({
        'labels': [r['rota'] for r in spend_by_route],
        'data':   [r['total'] for r in spend_by_route],
    })

    return render_template('dashboard/client_reports.html',
        client=client,
        period=period,
        total_freights=len(freights),
        delivered_count=len(delivered),
        in_transit_count=len(in_transit),
        cancelled_count=len(cancelled),
        this_month_count=len(this_month),
        on_time_count=on_time_count,
        late_count=late_count,
        on_time_rate=on_time_rate,
        total_revenue=total_revenue,
        month_revenue=month_revenue,
        avg_ticket=avg_ticket,
        top_routes=top_routes,
        load_type_counter=dict(load_type_counter),
        vehicle_counter=dict(vehicle_counter),
        monthly_rows=monthly_rows,
        monthly_labels=json.dumps(monthly_labels),
        monthly_volumes=json.dumps(monthly_volumes),
        monthly_revenues=json.dumps(monthly_revenues),
        total_quotes=total_quotes,
        pending_quotes=pending_quotes,
        awaiting_approval_quotes=awaiting_approval_quotes,
        today=today,
        quarterly_list=quarterly_list,
        spend_by_route=spend_by_route,
        chart_quarterly=chart_quarterly,
        chart_route_spend=chart_route_spend,
        aprovadas_q=aprovadas_q,
        negadas_q=negadas_q,
        pendentes_q=pendentes_q,
        tempo_medio_resposta=tempo_medio_resposta,
        cotacoes_recentes=cotacoes_recentes,
        sla_timeline=sla_timeline,
    )


# ── Exportação de dados KPI do cliente ───────────────────────────────────────
@dashboard_bp.route('/client/export')
@login_required
def client_export():
    """Exporta dados KPI do cliente em Excel ou CSV"""
    if current_user.role != 'cliente' or not current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))

    import io
    import csv as csv_mod
    from flask import send_file, request as req

    cid     = current_user.client_id
    client  = Client.query.get_or_404(cid)
    fmt     = req.args.get('type', 'excel')   # excel | csv
    period  = req.args.get('period', 'all')   # 1m | 3m | 6m | 12m | all
    today   = date.today()

    # ── Filtro de período ────────────────────────────────────────────────────
    period_map = {'1m': 1, '3m': 3, '6m': 6, '12m': 12}
    if period in period_map:
        cutoff = today - timedelta(days=30 * period_map[period])
    else:
        cutoff = None

    q = (Freight.query
         .filter_by(client_id=cid)
         .options(joinedload(Freight.quote), joinedload(Freight.assigned_driver))
         .order_by(Freight.created_at.desc()))
    if cutoff:
        q = q.filter(Freight.created_at >= cutoff)
    freights = q.all()

    delivered   = [f for f in freights if f.status == 'entregue']
    in_transit  = [f for f in freights if f.status == 'em_transito']
    cancelled   = [f for f in freights if f.status == 'cancelado']
    month_start = today.replace(day=1)
    this_month  = [f for f in freights if f.pickup_date and f.pickup_date >= month_start]

    # On-time
    on_time_count = late_count = 0
    for f in delivered:
        if f.delivery_date:
            log = (FreightStatusLog.query
                   .filter_by(freight_id=f.id, new_status='entregue')
                   .order_by(FreightStatusLog.created_at.desc()).first())
            actual = log.created_at.date() if log else (f.updated_at.date() if hasattr(f, 'updated_at') and f.updated_at else None)
            if actual:
                on_time_count += 1 if actual <= f.delivery_date else 0
                late_count    += 0 if actual <= f.delivery_date else 1
            else:
                on_time_count += 1
    on_time_rate = round(on_time_count / len(delivered) * 100) if delivered else 0

    total_revenue = sum(float(f.agreed_price or 0) for f in freights)
    month_revenue = sum(float(f.agreed_price or 0) for f in this_month)
    avg_ticket    = round(total_revenue / len(freights), 2) if freights else 0

    # Routes
    route_counter = Counter()
    for f in freights:
        if f.origin_city and f.destination_city:
            o = f"{f.origin_city}/{f.origin_state}" if f.origin_state else f.origin_city
            d = f"{f.destination_city}/{f.destination_state}" if f.destination_state else f.destination_city
            route_counter[f"{o} → {d}"] += 1

    load_type_counter = Counter()
    vehicle_counter   = Counter()
    for f in freights:
        load_type_counter[(f.quote.load_type  if f.quote else None) or 'desconhecido'] += 1
        vehicle_counter  [(f.quote.vehicle_type if f.quote else None) or 'desconhecido'] += 1

    # Monthly 12m
    monthly_rows = []
    cur_year, cur_month = today.year, today.month
    for i in range(11, -1, -1):
        m = cur_month - i
        y = cur_year
        while m <= 0:
            m += 12; y -= 1
        cnt = sum(1 for f in freights if f.pickup_date and f.pickup_date.year == y and f.pickup_date.month == m)
        rev = sum(float(f.agreed_price or 0) for f in freights if f.pickup_date and f.pickup_date.year == y and f.pickup_date.month == m)
        monthly_rows.append((f"{calendar.month_abbr[m]}/{str(y)[2:]}", cnt, round(rev, 2)))

    period_label = {'1m': 'Ultimo_mes', '3m': 'Ultimos_3meses',
                    '6m': 'Ultimos_6meses', '12m': 'Ultimo_ano'}.get(period, 'Historico_completo')
    safe_name    = client.company_name.replace(' ', '_').replace('/', '-')[:30]
    ts           = today.strftime('%Y%m%d')

    # ── CSV ─────────────────────────────────────────────────────────────────
    if fmt == 'csv':
        buf = io.StringIO()
        w   = csv_mod.writer(buf, delimiter=';')
        w.writerow(['Nº Frete', 'Origem', 'Destino', 'Status',
                    'Tipo Carga', 'Veículo', 'Coleta', 'Entrega Prevista',
                    'Valor (R$)', 'Motorista', 'Criado em'])
        for f in freights:
            w.writerow([
                f.freight_number,
                f"{f.origin_city or ''}/{f.origin_state or ''}".strip('/'),
                f"{f.destination_city or ''}/{f.destination_state or ''}".strip('/'),
                f.status,
                (f.quote.load_type    if f.quote else '') or '',
                (f.quote.vehicle_type if f.quote else '') or '',
                f.pickup_date.strftime('%d/%m/%Y')   if f.pickup_date   else '',
                f.delivery_date.strftime('%d/%m/%Y') if f.delivery_date else '',
                str(float(f.agreed_price or 0)).replace('.', ','),
                f.assigned_driver.name if f.assigned_driver else '',
                f.created_at.strftime('%d/%m/%Y') if f.created_at else '',
            ])
        output = io.BytesIO(buf.getvalue().encode('utf-8-sig'))
        fname  = f"EMALOG_{safe_name}_{period_label}_{ts}.csv"
        return send_file(output, as_attachment=True, download_name=fname,
                         mimetype='text/csv; charset=utf-8-sig')

    # ── Excel (.xlsx) ────────────────────────────────────────────────────────
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, numbers
        from openpyxl.utils import get_column_letter
    except ImportError:
        flash('Biblioteca openpyxl não instalada.', 'error')
        return redirect(url_for('dashboard.client_dashboard'))

    BLUE   = '1D4ED8'
    GREEN  = '15803D'
    PURPLE = '7C3AED'
    GRAY   = 'F3F4F6'
    WHITE  = 'FFFFFF'
    thin   = Side(style='thin', color='D1D5DB')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def hdr(cell, color=BLUE):
        cell.font      = Font(bold=True, color=WHITE, size=10)
        cell.fill      = PatternFill('solid', fgColor=color)
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border    = border

    def row_style(cell, shade=False):
        if shade:
            cell.fill = PatternFill('solid', fgColor=GRAY)
        cell.border    = border
        cell.alignment = Alignment(vertical='center')

    def autofit(ws, min_w=12, max_w=40):
        from openpyxl.cell.cell import MergedCell
        for col in ws.columns:
            first = col[0]
            if isinstance(first, MergedCell):
                continue
            width = max((len(str(c.value or '')) for c in col if not isinstance(c, MergedCell)), default=0)
            ws.column_dimensions[first.column_letter].width = min(max(width + 2, min_w), max_w)

    wb = openpyxl.Workbook()

    # ── ABA 1: Resumo KPI ────────────────────────────────────────────────────
    ws1 = wb.active
    ws1.title = '📊 Resumo KPI'
    ws1.merge_cells('A1:C1')
    title_cell = ws1['A1']
    title_cell.value     = f'EMALOG — Relatório de KPI | {client.company_name}'
    title_cell.font      = Font(bold=True, size=14, color=WHITE)
    title_cell.fill      = PatternFill('solid', fgColor=BLUE)
    title_cell.alignment = Alignment(horizontal='center', vertical='center')
    ws1.row_dimensions[1].height = 28

    ws1.merge_cells('A2:C2')
    sub = ws1['A2']
    sub.value     = f'Período: {period_label.replace("_", " ")} | Gerado em: {today.strftime("%d/%m/%Y")}'
    sub.font      = Font(italic=True, size=10, color='374151')
    sub.fill      = PatternFill('solid', fgColor='EFF6FF')
    sub.alignment = Alignment(horizontal='center')
    ws1.row_dimensions[2].height = 18

    kpis = [
        ('INDICADOR', 'VALOR', 'OBSERVAÇÃO'),
        ('Total de Fretes',         len(freights),         'No período selecionado'),
        ('Fretes Entregues',        len(delivered),        ''),
        ('Fretes Em Trânsito',      len(in_transit),       ''),
        ('Fretes Cancelados',       len(cancelled),        ''),
        ('Fretes Este Mês',         len(this_month),       'Mês corrente'),
        ('Taxa de Pontualidade',    f'{on_time_rate}%',    f'{on_time_count} no prazo · {late_count} atrasados'),
        ('Valor Total Contratado',  f'R$ {total_revenue:,.2f}', 'Soma agreed_price'),
        ('Valor Este Mês',          f'R$ {month_revenue:,.2f}', 'Mês corrente'),
        ('Ticket Médio por Frete',  f'R$ {avg_ticket:,.2f}',   'Média do período'),
        ('Cotações Solicitadas',    Quote.query.filter_by(client_id=cid).count(), ''),
        ('Cotações Pendentes',      Quote.query.filter_by(client_id=cid).filter(
            Quote.status.in_(['pendente'])).count(), 'Aguardando precificação'),
    ]
    for i, (a, b, c) in enumerate(kpis, 3):
        ws1.cell(i, 1, a); ws1.cell(i, 2, b); ws1.cell(i, 3, c)
        for col in range(1, 4):
            cell = ws1.cell(i, col)
            if i == 3:
                hdr(cell)
            else:
                row_style(cell, shade=(i % 2 == 0))
    ws1.column_dimensions['A'].width = 30
    ws1.column_dimensions['B'].width = 22
    ws1.column_dimensions['C'].width = 32

    # ── ABA 2: Histórico de Fretes ───────────────────────────────────────────
    ws2 = wb.create_sheet('🚛 Histórico de Fretes')
    cols2 = ['Nº Frete', 'Origem', 'Destino', 'Status', 'Tipo Carga',
             'Veículo', 'Coleta', 'Entrega Prevista', 'Valor (R$)', 'Motorista', 'Criado em']
    for c, h in enumerate(cols2, 1):
        hdr(ws2.cell(1, c, h))
    ws2.row_dimensions[1].height = 20
    for r, f in enumerate(freights, 2):
        shade = r % 2 == 0
        vals  = [
            f.freight_number,
            f"{f.origin_city or ''}/{f.origin_state or ''}".strip('/') or f.origin or '',
            f"{f.destination_city or ''}/{f.destination_state or ''}".strip('/') or f.destination or '',
            f.status,
            (f.quote.load_type    if f.quote else '') or '',
            (f.quote.vehicle_type if f.quote else '') or '',
            f.pickup_date.strftime('%d/%m/%Y')   if f.pickup_date   else '',
            f.delivery_date.strftime('%d/%m/%Y') if f.delivery_date else '',
            float(f.agreed_price or 0),
            f.assigned_driver.name if f.assigned_driver else '',
            f.created_at.strftime('%d/%m/%Y') if f.created_at else '',
        ]
        for c, v in enumerate(vals, 1):
            cell = ws2.cell(r, c, v)
            row_style(cell, shade)
            if c == 9:
                cell.number_format = 'R$ #,##0.00'
                cell.alignment = Alignment(horizontal='right')
    autofit(ws2)

    # ── ABA 3: Volume Mensal ─────────────────────────────────────────────────
    ws3 = wb.create_sheet('📅 Volume Mensal')
    for c, h in enumerate(['Mês', 'Qtd. Fretes', 'Receita (R$)'], 1):
        hdr(ws3.cell(1, c, h), color=GREEN)
    for r, (lbl, cnt, rev) in enumerate(monthly_rows, 2):
        ws3.cell(r, 1, lbl);  row_style(ws3.cell(r, 1), r % 2 == 0)
        ws3.cell(r, 2, cnt);  row_style(ws3.cell(r, 2), r % 2 == 0)
        c3 = ws3.cell(r, 3, rev)
        c3.number_format = 'R$ #,##0.00'
        c3.alignment = Alignment(horizontal='right')
        row_style(c3, r % 2 == 0)
    autofit(ws3, min_w=14)

    # ── ABA 4: Rotas Frequentes ──────────────────────────────────────────────
    ws4 = wb.create_sheet('🗺️ Rotas Frequentes')
    for c, h in enumerate(['Rota', 'Qtd. Fretes', '% do Total'], 1):
        hdr(ws4.cell(1, c, h), color=PURPLE)
    total_f = len(freights) or 1
    for r, (route, cnt) in enumerate(route_counter.most_common(20), 2):
        ws4.cell(r, 1, route); row_style(ws4.cell(r, 1), r % 2 == 0)
        ws4.cell(r, 2, cnt);   row_style(ws4.cell(r, 2), r % 2 == 0)
        pct = ws4.cell(r, 3, round(cnt / total_f * 100, 1))
        pct.number_format = '0.0"%"'
        pct.alignment = Alignment(horizontal='center')
        row_style(pct, r % 2 == 0)
    autofit(ws4, min_w=14)

    # ── ABA 5: Distribuição ──────────────────────────────────────────────────
    ws5 = wb.create_sheet('📦 Distribuição')
    ws5.cell(1, 1, 'TIPO DE CARGA').font = Font(bold=True, color=WHITE)
    ws5['A1'].fill = PatternFill('solid', fgColor='7C3AED')
    ws5['A1'].alignment = Alignment(horizontal='center')
    ws5.merge_cells('A1:B1')
    for c, h in enumerate(['Tipo', 'Qtd.'], 1):
        hdr(ws5.cell(2, c, h), color=PURPLE)
    for r, (tp, cnt) in enumerate(sorted(load_type_counter.items(), key=lambda x: -x[1]), 3):
        ws5.cell(r, 1, tp.title()); row_style(ws5.cell(r, 1), r % 2 == 0)
        ws5.cell(r, 2, cnt);        row_style(ws5.cell(r, 2), r % 2 == 0)

    row_off = len(load_type_counter) + 5
    ws5.cell(row_off, 1, 'TIPO DE VEÍCULO').font = Font(bold=True, color=WHITE)
    ws5.cell(row_off, 1).fill = PatternFill('solid', fgColor=BLUE)
    ws5.cell(row_off, 1).alignment = Alignment(horizontal='center')
    ws5.merge_cells(f'A{row_off}:B{row_off}')
    for c, h in enumerate(['Veículo', 'Qtd.'], 1):
        hdr(ws5.cell(row_off + 1, c, h))
    for i, (vt, cnt) in enumerate(sorted(vehicle_counter.items(), key=lambda x: -x[1]), 2):
        r = row_off + i
        ws5.cell(r, 1, vt.upper()); row_style(ws5.cell(r, 1), r % 2 == 0)
        ws5.cell(r, 2, cnt);        row_style(ws5.cell(r, 2), r % 2 == 0)
    autofit(ws5, min_w=18)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"EMALOG_{safe_name}_{period_label}_{ts}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')