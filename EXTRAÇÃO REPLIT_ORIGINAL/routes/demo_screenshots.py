"""
Rotas de demonstração — apenas para captura de screenshots da apresentação.
Cada rota faz login automático do primeiro usuário cliente encontrado.
REMOVER após concluir a apresentação.
"""
from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_user, current_user
from models import User, Client, Quote, Freight, FreightStatusLog
from app import db
from sqlalchemy.orm import joinedload
from sqlalchemy import func
from datetime import date, datetime, timedelta
from collections import Counter
import calendar, json, logging

demo_bp = Blueprint('demo', __name__, url_prefix='/demo')

DEMO_SECRET = 'emalog2025'

def _auto_login():
    """Faz login automático do primeiro usuário cliente disponível."""
    if current_user.is_authenticated and current_user.role == 'cliente':
        return current_user
    user = User.query.filter_by(role='cliente').first()
    if user:
        login_user(user, remember=False)
        return user
    return None

# ── TELA 1: Login ──────────────────────────────────────────────────────────
@demo_bp.route('/login')
def demo_login():
    return render_template('auth/login.html')

# ── TELA 2: Dashboard do cliente ───────────────────────────────────────────
@demo_bp.route('/dashboard')
def demo_dashboard():
    user = _auto_login()
    if not user or not user.client_id:
        return "Nenhum usuário cliente encontrado no banco.", 400

    client = Client.query.get_or_404(user.client_id)
    cid = user.client_id
    today = date.today()
    month_start = today.replace(day=1)

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

    on_time_count = 0
    late_count = 0
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

    on_time_rate  = round(on_time_count / len(delivered) * 100) if delivered else 0
    total_revenue = sum(f.agreed_price or 0 for f in freights)
    month_revenue = sum(f.agreed_price or 0 for f in this_month)
    avg_ticket    = round(total_revenue / len(freights), 2) if freights else 0

    route_counter = Counter()
    for f in freights:
        if f.origin_city and f.destination_city:
            o = f"{f.origin_city}/{f.origin_state}" if f.origin_state else f.origin_city
            d = f"{f.destination_city}/{f.destination_state}" if f.destination_state else f.destination_city
            route_counter[f"{o} → {d}"] += 1
    top_routes = route_counter.most_common(6)

    load_type_counter = Counter()
    vehicle_counter   = Counter()
    for f in freights:
        load_type_counter[(f.quote.load_type if f.quote else None) or 'desconhecido'] += 1
        vehicle_counter[(f.quote.vehicle_type if f.quote else None) or 'desconhecido'] += 1

    monthly_labels, monthly_volumes, monthly_revenues = [], [], []
    cur_year, cur_month = today.year, today.month
    for i in range(11, -1, -1):
        m = cur_month - i
        y = cur_year
        while m <= 0:
            m += 12; y -= 1
        monthly_labels.append(f"{calendar.month_abbr[m]}/{str(y)[2:]}")
        cnt = sum(1 for f in freights if f.pickup_date and f.pickup_date.year == y and f.pickup_date.month == m)
        rev = sum(f.agreed_price or 0 for f in freights if f.pickup_date and f.pickup_date.year == y and f.pickup_date.month == m)
        monthly_volumes.append(cnt)
        monthly_revenues.append(round(rev, 2))

    status_dist = {
        'Ofertado':    sum(1 for f in freights if f.status == 'ofertado'),
        'Aceito':      sum(1 for f in freights if f.status == 'aceito'),
        'Em Trânsito': len(in_transit),
        'Entregue':    len(delivered),
        'Cancelado':   len(cancelled),
    }

    total_quotes             = Quote.query.filter_by(client_id=cid).count()
    pending_quotes           = Quote.query.filter_by(client_id=cid, status='pendente').count()
    awaiting_approval_quotes = Quote.query.filter_by(client_id=cid).filter(Quote.status.in_(['cotada', 'negociacao'])).count()
    approved_quotes          = Quote.query.filter_by(client_id=cid, status='aprovada').count()
    rejected_quotes          = Quote.query.filter_by(client_id=cid).filter(Quote.status.in_(['rejeitada', 'rejeitado'])).count()
    recent_quotes            = Quote.query.filter_by(client_id=cid).order_by(Quote.created_at.desc()).limit(8).all()

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

# ── TELA 3: Lista de Cotações ──────────────────────────────────────────────
@demo_bp.route('/quotes')
def demo_quotes():
    user = _auto_login()
    if not user or not user.client_id:
        return "Nenhum usuário cliente encontrado.", 400

    quotes = (Quote.query
              .filter_by(client_id=user.client_id)
              .options(joinedload(Quote.client))
              .order_by(Quote.created_at.desc())
              .paginate(page=1, per_page=20, error_out=False))

    return render_template('quotes/index.html',
                           quotes=quotes, clients=[],
                           search='', status='', selected_client='')

# ── TELA 4: Nova Cotação ───────────────────────────────────────────────────
@demo_bp.route('/quote-new')
def demo_quote_new():
    user = _auto_login()
    if not user or not user.client_id:
        return "Nenhum usuário cliente encontrado.", 400

    client = Client.query.get(user.client_id)
    return render_template('quotes/form.html',
        clients=[client] if client else [],
        today=datetime.now().strftime('%Y-%m-%d'),
        crm_opp=None,
        prefill_client=None,
    )

# ── TELA 5: Detalhe de Cotação ─────────────────────────────────────────────
@demo_bp.route('/quote-detail')
def demo_quote_detail():
    user = _auto_login()
    if not user or not user.client_id:
        return "Nenhum usuário cliente encontrado.", 400

    quote = (Quote.query
             .filter_by(client_id=user.client_id)
             .options(joinedload(Quote.client))
             .order_by(Quote.created_at.desc())
             .first())

    if not quote:
        return "Nenhuma cotação encontrada para este cliente.", 404

    drivers = []
    return render_template('quotes/view.html',
        quote=quote,
        drivers=drivers,
        client=quote.client,
    )

# ── TELA 6: Lista de Fretes ────────────────────────────────────────────────
@demo_bp.route('/freights')
def demo_freights():
    user = _auto_login()
    if not user or not user.client_id:
        return "Nenhum usuário cliente encontrado.", 400

    pagination = (Freight.query
                  .filter_by(client_id=user.client_id)
                  .options(joinedload(Freight.quote), joinedload(Freight.assigned_driver))
                  .order_by(Freight.created_at.desc())
                  .paginate(page=1, per_page=20, error_out=False))

    return render_template('freight/index.html',
        freights=pagination.items,
        pagination=pagination,
        clients=[],
        search='', status='', selected_client='',
    )

# ── TELA 7: Detalhe de Frete ───────────────────────────────────────────────
@demo_bp.route('/freight-detail')
def demo_freight_detail():
    user = _auto_login()
    if not user or not user.client_id:
        return "Nenhum usuário cliente encontrado.", 400

    freight = (Freight.query
               .filter_by(client_id=user.client_id)
               .options(joinedload(Freight.quote), joinedload(Freight.assigned_driver))
               .order_by(Freight.created_at.desc())
               .first())

    if not freight:
        return "Nenhum frete encontrado para este cliente.", 404

    status_logs = (FreightStatusLog.query
                   .filter_by(freight_id=freight.id)
                   .order_by(FreightStatusLog.created_at.asc())
                   .all())

    return render_template('freight/view.html',
        freight=freight,
        status_logs=status_logs,
        drivers=[],
        google_maps_key='',
    )
