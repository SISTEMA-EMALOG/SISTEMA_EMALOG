import json
import logging
import os
import re
import tempfile
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session as flask_session, current_app
from flask_login import login_required, current_user
from models import db, Lead, Opportunity, CRMActivity, User, Client, Quote, Freight, LeadBatch
from datetime import datetime, date, timedelta
from functools import wraps

crm_bp = Blueprint('crm', __name__, url_prefix='/crm')
logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────

STAGES = [
    ('prospeccao', 'Prospecção',       'gray',    10),
    ('proposta',   'Cotação Enviada',  'purple',  50),
    ('negociacao', 'Em Negociação',    'amber',   75),
    ('ganho',      'Ganho',            'green',  100),
    ('perdido',    'Perdido',          'red',      0),
]
STAGE_DICT = {s[0]: s for s in STAGES}

LEAD_STATUSES = [
    ('novo',        'Novo',           'gray'),
    ('em_contato',  'Em Contato',     'blue'),
    ('qualificado', 'Qualificado',    'green'),
    ('proposta',    'Proposta Env.',  'purple'),
    ('convertido',  'Convertido',     'teal'),
    ('perdido',     'Perdido',        'red'),
]

SOURCES = [
    ('indicacao',    'Indicação'),
    ('linkedin',     'LinkedIn'),
    ('site',         'Site EMALOG'),
    ('cold_call',    'Cold Call'),
    ('whatsapp',     'WhatsApp'),
    ('evento',       'Feira / Evento'),
    ('operacional',  'Lead Operacional ⭐'),
    ('outro',        'Outro'),
]

ACTIVITY_TYPES = [
    ('ligacao',  'Ligação',   'fa-phone',         'blue'),
    ('email',    'E-mail',    'fa-envelope',      'indigo'),
    ('reuniao',  'Reunião',   'fa-handshake',     'purple'),
    ('visita',   'Visita',    'fa-map-marker-alt','teal'),
    ('proposta', 'Proposta',  'fa-file-contract', 'amber'),
    ('tarefa',   'Tarefa',    'fa-tasks',         'orange'),
    ('nota',     'Nota',      'fa-sticky-note',   'gray'),
]
AT_DICT = {t[0]: t for t in ACTIVITY_TYPES}

VEHICLE_TYPES = ['VUC', '3/4', 'Toco', 'Truck', 'Bitruck', 'Carreta', 'Van', 'Fiorino']

FREQUENCIES = [
    ('diario',   'Diário'),
    ('semanal',  'Semanal'),
    ('quinzenal','Quinzenal'),
    ('mensal',   'Mensal'),
    ('eventual', 'Eventual'),
]

# ── Decorator de acesso ───────────────────────────────────────────────────────

def crm_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.role not in ('admin', 'operador', 'vendedor'):
            flash('Acesso negado ao CRM.', 'error')
            return redirect(url_for('dashboard.index'))
        return f(*args, **kwargs)
    return decorated

# ── Dashboard ─────────────────────────────────────────────────────────────────

@crm_bp.route('/')
@login_required
@crm_required
def dashboard():
    today      = date.today()
    month_start = datetime.combine(today.replace(day=1), datetime.min.time())
    thirty_ago  = datetime.combine(today - timedelta(days=30), datetime.min.time())

    total_leads     = Lead.query.count()
    new_leads_30d   = Lead.query.filter(Lead.created_at >= thirty_ago).count()

    open_opps = Opportunity.query.filter(
        Opportunity.stage.notin_(['ganho', 'perdido'])
    ).count()
    pipeline_value = db.session.query(db.func.sum(Opportunity.value)).filter(
        Opportunity.stage.notin_(['ganho', 'perdido'])
    ).scalar() or 0

    won_30d = Opportunity.query.filter(
        Opportunity.stage == 'ganho',
        Opportunity.updated_at >= thirty_ago
    ).count()
    closed_30d = Opportunity.query.filter(
        Opportunity.stage.in_(['ganho', 'perdido']),
        Opportunity.updated_at >= thirty_ago
    ).count()
    conversion_rate = round(won_30d / closed_30d * 100, 1) if closed_30d else 0

    pending_activities = CRMActivity.query.filter_by(is_done=False).count()
    overdue_activities = CRMActivity.query.filter(
        CRMActivity.is_done == False,
        CRMActivity.scheduled_at < datetime.utcnow(),
        CRMActivity.scheduled_at.isnot(None)
    ).count()

    today_start = datetime.combine(today, datetime.min.time())
    today_end   = datetime.combine(today, datetime.max.time())
    week_end    = datetime.combine(today + timedelta(days=7), datetime.max.time())

    today_activities = CRMActivity.query.filter(
        CRMActivity.is_done == False,
        CRMActivity.scheduled_at.between(today_start, today_end)
    ).order_by(CRMActivity.scheduled_at).all()

    upcoming = CRMActivity.query.filter(
        CRMActivity.is_done == False,
        CRMActivity.scheduled_at.between(today_start, week_end)
    ).order_by(CRMActivity.scheduled_at).limit(20).all()

    # Single grouped query — avoids 2×N queries per stage
    _stage_rows = db.session.query(
        Opportunity.stage,
        db.func.count(Opportunity.id).label('cnt'),
        db.func.sum(Opportunity.value).label('val'),
    ).group_by(Opportunity.stage).all()
    _stage_map = {r.stage: r for r in _stage_rows}
    pipeline_by_stage = []
    for sid, sname, color, prob in STAGES:
        r = _stage_map.get(sid)
        pipeline_by_stage.append({
            'id': sid, 'name': sname, 'color': color,
            'count': r.cnt if r else 0,
            'value': float(r.val or 0) if r else 0,
        })

    recent_leads = Lead.query.order_by(Lead.updated_at.desc()).limit(8).all()

    won_opps  = Opportunity.query.filter(Opportunity.stage=='ganho', Opportunity.updated_at>=month_start).all()
    won_value = sum(o.value or 0 for o in won_opps)

    # ── Ranking de vendedores ──────────────────────────────────────────────────
    sellers = User.query.filter(User.role.in_(['admin', 'operador', 'vendedor'])).all()
    seller_ranking = []
    for seller in sellers:
        leads_assigned   = Lead.query.filter_by(assigned_to=seller.id).count()
        contacted_count  = Lead.query.filter(
            Lead.assigned_to == seller.id,
            Lead.status.in_(['em_contato', 'qualificado', 'proposta', 'convertido', 'perdido'])
        ).count()
        converted_count  = Lead.query.filter_by(assigned_to=seller.id, status='convertido').count()
        activities_done  = CRMActivity.query.filter_by(created_by=seller.id, is_done=True).count()
        activities_30d   = CRMActivity.query.filter(
            CRMActivity.created_by == seller.id,
            CRMActivity.created_at >= thirty_ago
        ).count()
        opps_won_month   = Opportunity.query.filter(
            Opportunity.assigned_to == seller.id,
            Opportunity.stage == 'ganho',
            Opportunity.updated_at >= month_start
        ).count()
        won_val_month    = db.session.query(db.func.sum(Opportunity.value)).filter(
            Opportunity.assigned_to == seller.id,
            Opportunity.stage == 'ganho',
            Opportunity.updated_at >= month_start
        ).scalar() or 0
        contact_rate     = round(contacted_count / leads_assigned * 100) if leads_assigned else 0

        if leads_assigned == 0 and activities_done == 0:
            continue

        seller_ranking.append({
            'seller':          seller,
            'leads':           leads_assigned,
            'contacted':       contacted_count,
            'converted':       converted_count,
            'contact_rate':    contact_rate,
            'activities_30d':  activities_30d,
            'opps_won_month':  opps_won_month,
            'won_val_month':   won_val_month,
        })

    seller_ranking.sort(key=lambda x: (x['opps_won_month'], x['contacted'], x['activities_30d']), reverse=True)

    # ── Alerta de lote esgotando ───────────────────────────────────────────────
    from models import LeadBatch
    from sqlalchemy import func as _func
    batches_alert = []
    for b in LeadBatch.query.order_by(LeadBatch.created_at.desc()).limit(5).all():
        total = Lead.query.filter_by(import_batch=b.batch_id).count()
        if total == 0:
            continue
        contacted = Lead.query.filter(
            Lead.import_batch == b.batch_id,
            Lead.status.in_(['em_contato', 'qualificado', 'proposta', 'convertido', 'perdido'])
        ).count()
        pct = round(contacted / total * 100) if total else 0
        if pct >= 70:
            batches_alert.append({'name': b.name, 'pct': pct, 'batch_id': b.batch_id})

    return render_template('crm/dashboard.html',
        total_leads=total_leads, new_leads_30d=new_leads_30d,
        open_opps=open_opps, pipeline_value=pipeline_value,
        conversion_rate=conversion_rate,
        pending_activities=pending_activities, overdue_activities=overdue_activities,
        today_activities=today_activities, upcoming=upcoming,
        pipeline_by_stage=pipeline_by_stage,
        recent_leads=recent_leads,
        won_value=won_value, won_opps_count=len(won_opps),
        today=today,
        LEAD_STATUS_DICT={s[0]: s for s in LEAD_STATUSES},
        AT_DICT=AT_DICT,
        seller_ranking=seller_ranking,
        batches_alert=batches_alert,
    )

# ── Leads — listagem ──────────────────────────────────────────────────────────

@crm_bp.route('/leads')
@login_required
@crm_required
def leads_index():
    q           = request.args.get('q', '').strip()
    status_f    = request.args.get('status', '')
    source_f    = request.args.get('source', '')
    state_f     = request.args.get('state', '')
    city_f      = request.args.get('city', '').strip()
    segment_f   = request.args.get('segment', '').strip()
    ai_f        = request.args.get('ai', '')
    batch_f     = request.args.get('batch', '').strip()
    page        = request.args.get('page', 1, type=int)

    query = Lead.query
    if q:
        query = query.filter(db.or_(
            Lead.company_name.ilike(f'%{q}%'),
            Lead.contact_name.ilike(f'%{q}%'),
            Lead.contact_email.ilike(f'%{q}%'),
            Lead.city.ilike(f'%{q}%'),
            Lead.cnpj.ilike(f'%{q}%'),
        ))
    if status_f:
        query = query.filter_by(status=status_f)
    if source_f:
        query = query.filter_by(source=source_f)
    if state_f:
        query = query.filter_by(state=state_f)
    if city_f:
        query = query.filter(Lead.city.ilike(f'%{city_f}%'))
    if segment_f:
        query = query.filter(db.or_(
            Lead.segment.ilike(f'%{segment_f}%'),
            Lead.industry_type.ilike(f'%{segment_f}%'),
        ))
    if ai_f == '1':
        query = query.filter_by(ai_enriched=True)
    if batch_f:
        query = query.filter_by(import_batch=batch_f)
    hot_f = request.args.get('hot', '')
    if hot_f == '1':
        query = query.filter_by(is_hot=True)

    leads = query.order_by(Lead.updated_at.desc()).paginate(page=page, per_page=20, error_out=False)

    # Lote ativo (para exibir banner no topo)
    active_batch = LeadBatch.query.filter_by(batch_id=batch_f).first() if batch_f else None

    from sqlalchemy import func as _sfunc
    _raw_counts = dict(db.session.query(Lead.status, _sfunc.count(Lead.id)).group_by(Lead.status).all())
    status_counts = {s[0]: _raw_counts.get(s[0], 0) for s in LEAD_STATUSES}

    # Listas para filtros dinâmicos
    states_available = [r[0] for r in db.session.query(Lead.state).filter(Lead.state.isnot(None), Lead.state != '').distinct().order_by(Lead.state).all()]

    # Última atividade por lead (para exibir no card)
    from sqlalchemy import func as _func
    lead_ids = [l.id for l in leads.items]
    last_activity_per_lead = {}
    quote_count_per_lead   = {}
    if lead_ids:
        # Subquery: max created_at por lead
        subq = db.session.query(
            CRMActivity.lead_id,
            _func.max(CRMActivity.created_at).label('max_ca')
        ).filter(CRMActivity.lead_id.in_(lead_ids)).group_by(CRMActivity.lead_id).subquery()

        acts = db.session.query(CRMActivity).join(
            subq, db.and_(
                CRMActivity.lead_id   == subq.c.lead_id,
                CRMActivity.created_at == subq.c.max_ca,
            )
        ).all()
        for act in acts:
            last_activity_per_lead[act.lead_id] = act

        # Contagem de cotações via oportunidades
        from models import Opportunity, Quote
        opp_rows = db.session.query(Opportunity.lead_id, Opportunity.id)\
                       .filter(Opportunity.lead_id.in_(lead_ids)).all()
        opp_id_to_lead = {r[1]: r[0] for r in opp_rows}
        if opp_id_to_lead:
            q_counts = db.session.query(
                Quote.opportunity_id, _func.count(Quote.id)
            ).filter(Quote.opportunity_id.in_(list(opp_id_to_lead.keys())))\
             .group_by(Quote.opportunity_id).all()
            for opp_id, cnt in q_counts:
                lid = opp_id_to_lead[opp_id]
                quote_count_per_lead[lid] = quote_count_per_lead.get(lid, 0) + cnt

    return render_template('crm/leads/index.html',
        leads=leads, q=q, status_f=status_f, source_f=source_f,
        state_f=state_f, city_f=city_f, segment_f=segment_f, ai_f=ai_f,
        batch_f=batch_f, active_batch=active_batch,
        LEAD_STATUSES=LEAD_STATUSES, SOURCES=SOURCES,
        status_counts=status_counts, SOURCES_DICT=dict(SOURCES),
        LEAD_STATUS_DICT={s[0]: s for s in LEAD_STATUSES},
        states_available=states_available,
        last_activity_per_lead=last_activity_per_lead,
        quote_count_per_lead=quote_count_per_lead,
        now=datetime.utcnow(),
    )

# ── Leads — CRUD ──────────────────────────────────────────────────────────────

@crm_bp.route('/leads/new', methods=['GET', 'POST'])
@login_required
@crm_required
def leads_new():
    clients_json = _get_clients_json()

    if request.method == 'POST':
        client_mode = request.form.get('client_mode', 'novo')
        converted_client_id = None
        company = request.form.get('company_name', '').strip()

        def _tmpl(err=None):
            if err:
                flash(err, 'error')
            return render_template('crm/leads/form.html', lead=None,
                                   SOURCES=SOURCES, sellers=_get_sellers(),
                                   clients_json=clients_json)

        if client_mode == 'existente':
            cid = _safe_int(request.form.get('converted_client_id'))
            if cid:
                existing_client = Client.query.get(cid)
                if existing_client:
                    converted_client_id = cid
                    if not company:
                        company = existing_client.company_name
            if not converted_client_id:
                return _tmpl('Selecione um cliente válido da lista.')

        elif request.form.get('also_create_client'):
            cnpj = request.form.get('cnpj', '').strip()
            if not cnpj:
                flash('CNPJ é obrigatório para criar o cliente no sistema.', 'warning')
            else:
                existing_by_cnpj = Client.query.filter_by(cnpj=cnpj).first()
                if existing_by_cnpj:
                    converted_client_id = existing_by_cnpj.id
                    flash(f'CNPJ já cadastrado — lead vinculado ao cliente "{existing_by_cnpj.company_name}".', 'info')
                else:
                    new_client = Client(
                        company_name=(company[:100] if company else 'N/I'),
                        cnpj=cnpj,
                        phone=request.form.get('contact_phone', '').strip() or 'N/I',
                        email=request.form.get('contact_email', '').strip() or 'contato@emalog.com.br',
                        city=request.form.get('city', '').strip(),
                        state=request.form.get('state', '').strip(),
                        active=True,
                        created_by=current_user.id,
                    )
                    db.session.add(new_client)
                    db.session.flush()
                    converted_client_id = new_client.id
                    flash(f'Cliente "{company}" criado no banco de dados!', 'success')

        if not company:
            return _tmpl('Nome da empresa é obrigatório.')

        lead = Lead(
            company_name=company,
            cnpj=request.form.get('cnpj', '').strip(),
            contact_name=request.form.get('contact_name', '').strip(),
            contact_phone=request.form.get('contact_phone', '').strip(),
            contact_email=request.form.get('contact_email', '').strip(),
            city=request.form.get('city', '').strip(),
            state=request.form.get('state', '').strip(),
            segment=request.form.get('segment', '').strip(),
            industry_type=request.form.get('industry_type', '').strip(),
            website=request.form.get('website', '').strip(),
            employee_count=request.form.get('employee_count', '').strip(),
            zip_code=request.form.get('zip_code', '').strip(),
            address=request.form.get('address', '').strip(),
            source=request.form.get('source', 'outro'),
            status='novo',
            estimated_monthly_value=_safe_float(request.form.get('estimated_monthly_value')),
            notes=request.form.get('notes', '').strip(),
            assigned_to=_safe_int(request.form.get('assigned_to')) or current_user.id,
            converted_client_id=converted_client_id,
        )
        db.session.add(lead)
        db.session.commit()
        flash(f'Lead "{lead.company_name}" criado!', 'success')
        return redirect(url_for('crm.leads_show', id=lead.id))

    return render_template('crm/leads/form.html', lead=None,
                           SOURCES=SOURCES, sellers=_get_sellers(),
                           clients_json=clients_json)


@crm_bp.route('/leads/<int:id>')
@login_required
@crm_required
def leads_show(id):
    import re as _re
    lead = Lead.query.get_or_404(id)
    activities    = CRMActivity.query.filter_by(lead_id=id).order_by(CRMActivity.created_at.desc()).all()
    opportunities = Opportunity.query.filter_by(lead_id=id).order_by(Opportunity.updated_at.desc()).all()

    # Cotações vinculadas às oportunidades deste lead
    opp_ids = [o.id for o in opportunities]
    quotes_by_opp = {}
    _synced = False
    if opp_ids:
        for q in Quote.query.filter(Quote.opportunity_id.in_(opp_ids)).order_by(Quote.created_at.desc()).all():
            quotes_by_opp.setdefault(q.opportunity_id, []).append(q)

    # Sincronizar valor da oportunidade com o da cotação mais recente vinculada
    # (garante que valores editados na cotação se reflitam no funil)
    for opp in opportunities:
        linked = quotes_by_opp.get(opp.id)
        if linked:
            best_quote = linked[0]  # já ordenado por created_at desc
            if best_quote.sale_value and best_quote.sale_value != opp.value:
                opp.value = best_quote.sale_value
                opp.updated_at = datetime.utcnow()
                _synced = True
    if _synced:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    # Verificar se já existe cliente com mesmo CNPJ (mesmo sem vínculo formal)
    client_by_cnpj = None
    if lead.cnpj and not lead.converted_client_id:
        digits = _re.sub(r'\D', '', lead.cnpj)
        if len(digits) == 14:
            client_by_cnpj = Client.query.filter(
                db.func.regexp_replace(Client.cnpj, r'\D', '', 'g') == digits
            ).first()

    # Cotações diretas associadas a este lead via clientes com mesmo CNPJ
    direct_quotes = []
    if lead.converted_client_id:
        direct_quotes = Quote.query.filter_by(client_id=lead.converted_client_id)\
                            .order_by(Quote.created_at.desc()).limit(10).all()
    elif client_by_cnpj:
        direct_quotes = Quote.query.filter_by(client_id=client_by_cnpj.id)\
                            .order_by(Quote.created_at.desc()).limit(10).all()

    # Fretes vinculados às cotações aprovadas deste lead
    all_quote_ids = [q.id for qs in quotes_by_opp.values() for q in qs]
    all_quote_ids += [q.id for q in direct_quotes]
    freights_by_quote = {}
    if all_quote_ids:
        for f in Freight.query.filter(Freight.quote_id.in_(all_quote_ids))\
                              .order_by(Freight.created_at.desc()).all():
            freights_by_quote[f.quote_id] = f

    return render_template('crm/leads/show.html',
        lead=lead, activities=activities, opportunities=opportunities,
        quotes_by_opp=quotes_by_opp,
        client_by_cnpj=client_by_cnpj,
        direct_quotes=direct_quotes,
        freights_by_quote=freights_by_quote,
        STAGES=STAGES, STAGE_DICT=STAGE_DICT,
        LEAD_STATUSES=LEAD_STATUSES,
        LEAD_STATUS_DICT={s[0]: s for s in LEAD_STATUSES},
        SOURCES_DICT=dict(SOURCES),
        ACTIVITY_TYPES=ACTIVITY_TYPES, AT_DICT=AT_DICT,
        VEHICLE_TYPES=VEHICLE_TYPES, FREQUENCIES=FREQUENCIES,
        now=datetime.utcnow(),
    )


@crm_bp.route('/leads/<int:id>/convert-client', methods=['POST'])
@login_required
@crm_required
def leads_convert_client(id):
    import re as _re
    lead = Lead.query.get_or_404(id)

    # Se já vinculado, apenas redirecionar
    if lead.converted_client_id:
        flash('Lead já está vinculado a um cliente.', 'info')
        return redirect(url_for('crm.leads_show', id=id))

    # Verificar se já existe cliente com mesmo CNPJ
    if lead.cnpj:
        digits = _re.sub(r'\D', '', lead.cnpj)
        existing = None
        if len(digits) == 14:
            existing = Client.query.filter(
                db.func.regexp_replace(Client.cnpj, r'\D', '', 'g') == digits
            ).first()
        if existing:
            lead.converted_client_id = existing.id
            lead.status = 'convertido'
            lead.updated_at = datetime.utcnow()
            db.session.commit()
            flash(f'Lead vinculado ao cliente existente "{existing.company_name}".', 'success')
            return redirect(url_for('clients.view', id=existing.id))

    # Criar novo cliente com todos os dados do lead
    phone = lead.contact_phone or lead.whatsapp or 'N/I'
    email = lead.contact_email or 'contato@empresa.com'
    new_client = Client(
        company_name = lead.company_name[:100],
        cnpj         = lead.cnpj or '',
        phone        = phone,
        email        = email,
        cep          = lead.zip_code or '',
        city         = lead.city or '',
        state        = lead.state or '',
        created_by   = current_user.id,
    )
    # Endereço estruturado
    if hasattr(lead, 'street') and lead.street:
        new_client.street       = lead.street
        new_client.number       = getattr(lead, 'number_addr', '') or ''
        new_client.complement   = getattr(lead, 'complement', '') or ''
        new_client.neighborhood = getattr(lead, 'neighborhood', '') or ''
    if lead.address:
        new_client.address = lead.address

    db.session.add(new_client)
    db.session.flush()

    lead.converted_client_id = new_client.id
    lead.status              = 'convertido'
    lead.updated_at          = datetime.utcnow()

    # Registrar atividade de conversão
    act = CRMActivity(
        lead_id       = lead.id,
        activity_type = 'nota',
        title         = f'Lead convertido em cliente por {current_user.username}',
        notes         = f'Cliente "{new_client.company_name}" criado automaticamente a partir deste lead.',
        is_done       = True,
        created_by    = current_user.id,
    )
    db.session.add(act)
    db.session.commit()

    flash(f'✅ Cliente "{new_client.company_name}" criado com sucesso!', 'success')
    return redirect(url_for('clients.view', id=new_client.id))


@crm_bp.route('/leads/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@crm_required
def leads_edit(id):
    lead = Lead.query.get_or_404(id)
    clients_json = _get_clients_json()

    if request.method == 'POST':
        client_mode = request.form.get('client_mode', 'novo')

        if client_mode == 'existente':
            cid = _safe_int(request.form.get('converted_client_id'))
            if cid and Client.query.get(cid):
                lead.converted_client_id = cid
        elif request.form.get('also_create_client'):
            cnpj = request.form.get('cnpj', '').strip()
            company = request.form.get('company_name', '').strip()
            if cnpj and not lead.converted_client_id:
                existing_by_cnpj = Client.query.filter_by(cnpj=cnpj).first()
                if existing_by_cnpj:
                    lead.converted_client_id = existing_by_cnpj.id
                    flash(f'CNPJ já cadastrado — vinculado ao cliente "{existing_by_cnpj.company_name}".', 'info')
                else:
                    new_client = Client(
                        company_name=(company[:100] if company else lead.company_name[:100]),
                        cnpj=cnpj,
                        phone=request.form.get('contact_phone', '').strip() or lead.contact_phone or 'N/I',
                        email=request.form.get('contact_email', '').strip() or lead.contact_email or 'contato@emalog.com.br',
                        city=request.form.get('city', '').strip() or lead.city or '',
                        state=request.form.get('state', '').strip() or lead.state or '',
                        active=True,
                        created_by=current_user.id,
                    )
                    db.session.add(new_client)
                    db.session.flush()
                    lead.converted_client_id = new_client.id
                    flash(f'Cliente "{new_client.company_name}" criado no banco de dados!', 'success')
        else:
            lead.converted_client_id = None

        lead.company_name            = request.form.get('company_name', lead.company_name).strip() or lead.company_name
        lead.cnpj                    = request.form.get('cnpj', '').strip()
        lead.contact_name            = request.form.get('contact_name', '').strip()
        lead.contact_phone           = request.form.get('contact_phone', '').strip()
        lead.contact_email           = request.form.get('contact_email', '').strip()
        lead.city                    = request.form.get('city', '').strip()
        lead.state                   = request.form.get('state', '').strip()
        lead.segment                 = request.form.get('segment', '').strip()
        lead.industry_type           = request.form.get('industry_type', '').strip()
        lead.website                 = request.form.get('website', '').strip()
        lead.employee_count          = request.form.get('employee_count', '').strip()
        lead.zip_code                = request.form.get('zip_code', '').strip()
        lead.address                 = request.form.get('address', '').strip()
        lead.source                  = request.form.get('source', lead.source)
        lead.estimated_monthly_value = _safe_float(request.form.get('estimated_monthly_value'))
        lead.notes                   = request.form.get('notes', '').strip()
        lead.assigned_to             = _safe_int(request.form.get('assigned_to')) or lead.assigned_to
        lead.updated_at              = datetime.utcnow()
        db.session.commit()
        flash('Lead atualizado!', 'success')
        return redirect(url_for('crm.leads_show', id=lead.id))

    return render_template('crm/leads/form.html', lead=lead,
                           SOURCES=SOURCES, sellers=_get_sellers(),
                           clients_json=clients_json)


@crm_bp.route('/leads/import', methods=['GET', 'POST'])
@login_required
@crm_required
def leads_import():
    if request.method == 'GET':
        return render_template('crm/leads/import.html', sellers=_get_sellers())

    uploaded = request.files.get('excel_file')
    if not uploaded or not uploaded.filename:
        flash('Selecione um arquivo Excel ou CSV.', 'error')
        return redirect(url_for('crm.leads_import'))

    ext = os.path.splitext(uploaded.filename)[1].lower()
    if ext not in ('.xlsx', '.xls', '.csv'):
        flash('Formato inválido. Use .xlsx, .xls ou .csv.', 'error')
        return redirect(url_for('crm.leads_import'))

    try:
        import pandas as pd
        import io as _io
        content = uploaded.read()
        if ext == '.csv':
            df = pd.read_csv(_io.BytesIO(content), dtype=str)
        else:
            df = pd.read_excel(_io.BytesIO(content), dtype=str)
    except Exception as e:
        flash(f'Erro ao ler arquivo: {e}', 'error')
        return redirect(url_for('crm.leads_import'))

    # ── Mapeamento de colunas por nome (arquivos com cabeçalho) ───────────
    COL_MAP = {
        'company_name':  ['empresa','razao_social','razão social','razao social','nome','company','company_name','nome empresa','nome_empresa','razão_social'],
        'cnpj':          ['cnpj'],
        'contact_name':  ['contato','nome contato','nome_contato','contact','responsavel','responsável','representante'],
        'contact_phone': ['telefone','celular','phone','fone','tel','telefone_fixo','fone_fixo'],
        'contact_email': ['email','e-mail','email contato','email_contato','e_mail'],
        'city':          ['cidade','city','municipio','município'],
        'state':         ['estado','uf','state','uf_estado'],
        'segment':       ['segmento','segment','setor','ramo','atividade'],
        'industry_type': ['industria','indústria','industry','tipo industria','tipo_industria','ramo_atividade'],
        'source':        ['origem','source','canal'],
        'zip_code':      ['cep','zip','zip_code','cep_endereco'],
        'address':       ['endereco','endereço','address','logradouro'],
        'street':        ['rua','street'],
        'neighborhood':  ['bairro','neighborhood'],
        'website':       ['site','website','url','homepage'],
        'whatsapp':      ['whatsapp','zap','wpp'],
        'employee_count':['funcionarios','funcionários','employees','colaboradores'],
        'estimated_monthly_value': ['valor mensal','faturamento','valor estimado','monthly_value','valor_mensal'],
        'notes':         ['observacoes','observações','notas','notes','obs','comentarios','comentários'],
    }

    df.columns = [str(c).strip().lower() for c in df.columns]

    def _find_col(field):
        for alias in COL_MAP.get(field, [field]):
            if alias in df.columns:
                return alias
        return None

    def _row_val(row, col):
        if not col or col not in row:
            return ''
        v = str(row[col]).strip()
        return '' if v in ('nan','None','') else v

    # Checar se há cabeçalho reconhecível
    matched = sum(1 for f in ['company_name','contact_email','city','state','cnpj'] if _find_col(f))
    use_pattern = matched == 0

    batch_id    = f'IMP-{datetime.now().strftime("%Y%m%d%H%M%S")}'
    raw_leads   = []
    detection   = 'headers'

    # ── Fluxo A: arquivo com cabeçalhos reconhecidos ──────────────────────
    if not use_pattern:
        for _, row in df.iterrows():
            ld = {'import_batch': batch_id}
            for field in COL_MAP:
                ld[field] = _row_val(row, _find_col(field))
            raw_leads.append(ld)

    # ── Fluxo B: arquivo sem cabeçalho — detecção por padrão de dados ────
    else:
        detection = 'pattern'
        try:
            if ext == '.csv':
                df_raw = pd.read_csv(_io.BytesIO(content), header=None, dtype=str)
            else:
                df_raw = pd.read_excel(_io.BytesIO(content), header=None, dtype=str)
        except Exception as e:
            flash(f'Erro ao ler arquivo: {e}', 'error')
            return redirect(url_for('crm.leads_import'))

        BR_STATES = {'AC','AL','AP','AM','BA','CE','DF','ES','GO','MA','MT','MS',
                     'MG','PA','PB','PR','PE','PI','RJ','RN','RS','RO','RR','SC',
                     'SP','SE','TO'}
        BR_DDDS   = {'11','12','13','14','15','16','17','18','19','21','22','24',
                     '27','28','31','32','33','34','35','37','38','41','42','43',
                     '44','45','46','47','48','49','51','53','54','55','61','62',
                     '63','64','65','66','67','68','69','71','73','74','75','77',
                     '79','81','82','83','84','85','86','87','88','89','91','92',
                     '93','94','95','96','97','98','99'}

        def _digits(v):
            return re.sub(r'\D', '', str(v))

        def _sample(ci, n=30):
            return [v for v in df_raw.iloc[:n, ci].fillna('').astype(str)
                    if v not in ('', 'nan', 'None')]

        def _pct(ci, fn, n=30):
            s = _sample(ci, n)
            return sum(1 for v in s if fn(v)) / len(s) if s else 0

        num_cols  = len(df_raw.columns)
        col_roles = {}  # {col_idx: role_name}

        for ci in range(num_cols):
            samp = _sample(ci)
            if not samp:
                continue

            if _pct(ci, lambda v: '@' in v and '.' in v.split('@')[-1]) > 0.4:
                role = 'contact_email' if 'contact_email' not in col_roles.values() else 'contact_email2'
                col_roles[ci] = role

            elif _pct(ci, lambda v: v.strip().startswith('http') or v.strip().startswith('www')) > 0.35:
                if 'website' not in col_roles.values():
                    col_roles[ci] = 'website'

            elif _pct(ci, lambda v: v.strip().upper() in BR_STATES and len(v.strip()) == 2) > 0.5:
                if 'state' not in col_roles.values():
                    col_roles[ci] = 'state'

            elif _pct(ci, lambda v: _digits(v).replace('.','') in BR_DDDS and 1 < len(_digits(v)) < 4) > 0.5:
                if 'phone_ddd' not in col_roles.values():
                    col_roles[ci] = 'phone_ddd'

            elif _pct(ci, lambda v: 8 <= len(_digits(v)) <= 9) > 0.45:
                if 'contact_phone_raw' not in col_roles.values():
                    col_roles[ci] = 'contact_phone_raw'
                elif 'contact_cell' not in col_roles.values():
                    col_roles[ci] = 'contact_cell'

            elif _pct(ci, lambda v: any(v.lower().startswith(p)
                      for p in ('rua','av.','avenida','r.','rod.','rodovia',
                                'travessa','alameda','estrada','praça'))) > 0.25:
                if 'street' not in col_roles.values():
                    col_roles[ci] = 'street'

            elif _pct(ci, lambda v: len(_digits(v)) == 8 and not v.strip().isdigit()) > 0.4:
                if 'zip_code' not in col_roles.values():
                    col_roles[ci] = 'zip_code'

        # Nome da empresa: 1ª coluna de texto com comprimento médio razoável
        if 'company_name' not in col_roles.values():
            for ci in range(min(4, num_cols)):
                if ci in col_roles:
                    continue
                s = _sample(ci)
                if s and sum(1 for v in s if 3 < len(v) < 120 and not _digits(v) == v) / len(s) > 0.7:
                    col_roles[ci] = 'company_name'
                    break

        # Cidade e Bairro: colunas de texto entre rua e estado
        # Estratégia: última coluna de texto antes do estado = cidade
        #             primeira coluna de texto antes dessa = bairro
        street_ci = next((c for c, r in col_roles.items() if r == 'street'), None)
        state_ci  = next((c for c, r in col_roles.items() if r == 'state'), None)
        if street_ci is not None and state_ci is not None:
            free_text_cols = []
            for ci in range(street_ci + 1, state_ci):
                if ci not in col_roles:
                    s = _sample(ci)
                    if s and sum(1 for v in s if 3 < len(v) < 60) / len(s) > 0.55:
                        free_text_cols.append(ci)
            # Última = cidade, penúltima (se existir) = bairro
            if free_text_cols:
                if 'city' not in col_roles.values():
                    col_roles[free_text_cols[-1]] = 'city'
                if len(free_text_cols) >= 2 and 'neighborhood' not in col_roles.values():
                    col_roles[free_text_cols[-2]] = 'neighborhood'

        # Setor/indústria: coluna de texto longo
        if 'industry_type' not in col_roles.values():
            for ci in range(num_cols):
                if ci not in col_roles:
                    s = _sample(ci)
                    if s and sum(1 for v in s if len(v) > 20) / len(s) > 0.4:
                        col_roles[ci] = 'industry_type'
                        break

        current_app.logger.info(f'📊 Detecção de colunas (padrão): {col_roles}')

        ddd_ci = next((c for c, r in col_roles.items() if r == 'phone_ddd'), None)

        def _gv(row, ci):
            if ci is None or ci not in df_raw.columns:
                return ''
            v = str(row[ci]).strip().replace('.0', '').strip()
            return '' if v in ('nan', 'None', '') else v

        for _, row in df_raw.iterrows():
            ld = {f: '' for f in ['company_name','cnpj','contact_name','contact_phone',
                                   'contact_email','city','state','segment','industry_type',
                                   'source','zip_code','address','street','neighborhood',
                                   'website','whatsapp','employee_count',
                                   'estimated_monthly_value','notes','import_batch']}
            ld['import_batch'] = batch_id

            for ci, role in col_roles.items():
                val = _gv(row, ci)
                if not val:
                    continue

                if role == 'company_name':
                    ld['company_name'] = val.title() if val.isupper() else val

                elif role == 'contact_email':
                    ld['contact_email'] = val.lower()

                elif role == 'contact_email2':
                    if not ld['contact_email']:
                        ld['contact_email'] = val.lower()

                elif role == 'state':
                    ld['state'] = val.upper()[:2]

                elif role == 'phone_ddd':
                    pass  # usado apenas como prefixo

                elif role == 'contact_phone_raw':
                    ddd = _gv(row, ddd_ci)
                    ld['contact_phone'] = f'({ddd}) {val}' if ddd else val

                elif role == 'contact_cell':
                    ddd = _gv(row, ddd_ci)
                    ld['whatsapp'] = f'({ddd}) {val}' if ddd else val

                elif role == 'street':
                    ld['street'] = val

                elif role == 'neighborhood':
                    ld['neighborhood'] = val

                elif role in ('contact_name','city','segment','industry_type',
                              'website','zip_code','address','whatsapp','cnpj','notes'):
                    ld[role] = val

            if (ld['street'] or ld['neighborhood']) and not ld['address']:
                ld['address'] = ', '.join(p for p in [ld['street'], ld['neighborhood']] if p)

            raw_leads.append(ld)

    if not raw_leads:
        flash('Arquivo vazio ou sem dados válidos.', 'warning')
        return redirect(url_for('crm.leads_import'))

    # ── Validação: filtrar linhas sem nome e normalizar ───────────────────
    default_source = request.form.get('default_source', 'outro')
    valid_sources  = [s[0] for s in SOURCES]

    valid_leads = []
    skipped_count = 0
    for ld in raw_leads:
        name = ld.get('company_name', '').strip()
        if not name:
            skipped_count += 1
            continue
        # Normalizar source
        src = ld.get('source', '').lower().strip()
        ld['source'] = src if src in valid_sources else default_source
        # Normalizar estado
        ld['state'] = (ld.get('state') or '').upper()[:2]
        valid_leads.append(ld)

    # ── Agente de normalização de telefones ───────────────────────────────
    phone_corrections = []
    try:
        from utils.phone_normalizer import normalize_phones_with_ai
        valid_leads, phones_fixed, phone_corrections = normalize_phones_with_ai(valid_leads)
    except Exception as _pe:
        import logging as _log
        _log.getLogger(__name__).warning(f'Normalizador de telefones falhou: {_pe}')
        phones_fixed = 0

    # ── Salvar preview temporário ─────────────────────────────────────────
    assigned_id = _safe_int(request.form.get('assigned_to')) or current_user.id
    preview_data = {
        'batch_id':          batch_id,
        'assigned_id':       assigned_id,
        'valid_leads':       valid_leads,
        'skipped_count':     skipped_count,
        'detection':         detection,
        'filename':          uploaded.filename,
        'phones_fixed':      phones_fixed,
        'phone_corrections': phone_corrections,
    }
    tmp_path = os.path.join(tempfile.gettempdir(), f'emalog_import_{batch_id}.json')
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(preview_data, f, ensure_ascii=False, default=str)

    flask_session['import_preview_batch'] = batch_id
    return redirect(url_for('crm.leads_import_preview'))


@crm_bp.route('/leads/import/preview', methods=['GET'])
@login_required
@crm_required
def leads_import_preview():
    batch_id = flask_session.get('import_preview_batch')
    if not batch_id:
        flash('Nenhuma importação pendente de aprovação.', 'warning')
        return redirect(url_for('crm.leads_import'))

    tmp_path = os.path.join(tempfile.gettempdir(), f'emalog_import_{batch_id}.json')
    if not os.path.exists(tmp_path):
        flash('Dados de preview expirados. Faça o upload novamente.', 'warning')
        flask_session.pop('import_preview_batch', None)
        return redirect(url_for('crm.leads_import'))

    with open(tmp_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    return render_template(
        'crm/leads/import_preview.html',
        batch_id          = data['batch_id'],
        valid_leads       = data['valid_leads'],
        skipped_count     = data.get('skipped_count', 0),
        detection         = data.get('detection', 'headers'),
        filename          = data.get('filename', ''),
        assigned_id       = data['assigned_id'],
        sellers           = _get_sellers(),
        SOURCES           = SOURCES,
        phones_fixed      = data.get('phones_fixed', 0),
        phone_corrections = data.get('phone_corrections', []),
    )


@crm_bp.route('/leads/import/confirm', methods=['POST'])
@login_required
@crm_required
def leads_import_confirm():
    batch_id = flask_session.get('import_preview_batch')
    if not batch_id:
        flash('Sessão de importação expirada.', 'error')
        return redirect(url_for('crm.leads_import'))

    tmp_path = os.path.join(tempfile.gettempdir(), f'emalog_import_{batch_id}.json')
    if not os.path.exists(tmp_path):
        flash('Dados de importação expirados. Faça o upload novamente.', 'error')
        flask_session.pop('import_preview_batch', None)
        return redirect(url_for('crm.leads_import'))

    with open(tmp_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    valid_leads = data['valid_leads']
    batch_id    = data['batch_id']
    assigned_id = _safe_int(request.form.get('assigned_id')) or data.get('assigned_id') or current_user.id

    # ── Inserir no banco (pular CNPJs já existentes) ──────────────────────
    import re as _re
    existing_cnpjs = set()
    for row in db.session.query(Lead.cnpj).filter(Lead.cnpj.isnot(None)).all():
        existing_cnpjs.add(_re.sub(r'\D', '', row[0] or ''))

    inserted   = 0
    skipped_db = 0

    for ld in valid_leads:
        raw_cnpj = _re.sub(r'\D', '', ld.get('cnpj', ''))
        if raw_cnpj and len(raw_cnpj) == 14 and raw_cnpj in existing_cnpjs:
            skipped_db += 1
            continue
        if raw_cnpj:
            existing_cnpjs.add(raw_cnpj)

        src = ld.get('source', '').lower()
        valid_sources = [s[0] for s in SOURCES]
        src = src if src in valid_sources else 'outro'

        try:
            emv = float(str(ld.get('estimated_monthly_value', '')).replace(',', '.').replace('R$', '').strip()) if ld.get('estimated_monthly_value') else None
        except Exception:
            emv = None

        lead = Lead(
            company_name   = (ld.get('company_name') or 'N/I')[:200],
            cnpj           = (ld.get('cnpj') or '')[:20],
            contact_name   = (ld.get('contact_name') or '')[:150],
            contact_phone  = (ld.get('contact_phone') or '')[:30],
            contact_email  = (ld.get('contact_email') or '')[:150],
            city           = (ld.get('city') or '')[:100],
            state          = (ld.get('state') or '')[:2],
            segment        = (ld.get('segment') or '')[:100],
            industry_type  = (ld.get('industry_type') or '')[:100],
            source         = src,
            status         = 'novo',
            estimated_monthly_value = emv,
            notes          = (ld.get('notes') or '')[:500] or None,
            zip_code       = (ld.get('zip_code') or '')[:10],
            address        = (ld.get('address') or '')[:300],
            website        = (ld.get('website') or '')[:200],
            employee_count = (ld.get('employee_count') or '')[:30],
            import_batch   = batch_id,
            ai_enriched    = False,
            assigned_to    = assigned_id,
            whatsapp       = (ld.get('whatsapp') or '')[:30],
            street         = (ld.get('street') or '')[:200],
            number_addr    = (ld.get('number_addr') or '')[:20],
            complement     = (ld.get('complement') or '')[:100],
            neighborhood   = (ld.get('neighborhood') or '')[:100],
        )
        db.session.add(lead)
        inserted += 1

    db.session.commit()

    # ── Registrar lote ─────────────────────────────────────────────────────
    batch_name = request.form.get('batch_name', '').strip() or data.get('filename', batch_id)
    existing_batch = LeadBatch.query.filter_by(batch_id=batch_id).first()
    if not existing_batch and inserted > 0:
        lb = LeadBatch(
            batch_id    = batch_id,
            name        = batch_name[:200],
            filename    = data.get('filename', ''),
            source      = request.form.get('default_source') or data.get('default_source', 'outro'),
            assigned_to = assigned_id,
            total_leads = inserted,
            created_by  = current_user.id,
        )
        db.session.add(lb)
        db.session.commit()

    # Limpar arquivos e sessão
    try:
        os.remove(tmp_path)
    except Exception:
        pass
    flask_session.pop('import_preview_batch', None)

    skipped_no_name = data.get('skipped_count', 0)
    flash(f'✅ Importação concluída: {inserted} leads inseridos, {skipped_db} CNPJ(s) duplicado(s) ignorado(s), {skipped_no_name} linha(s) sem nome ignorada(s).', 'success')
    return redirect(url_for('crm.batches_index'))


# ── Lotes de Importação ───────────────────────────────────────────────────────

@crm_bp.route('/lotes')
@login_required
@crm_required
def batches_index():
    from sqlalchemy import func as _func

    batches = LeadBatch.query.order_by(LeadBatch.created_at.desc()).all()

    batch_stats = []
    for b in batches:
        total      = Lead.query.filter_by(import_batch=b.batch_id).count()
        contacted  = Lead.query.filter(
            Lead.import_batch == b.batch_id,
            Lead.status.in_(['em_contato', 'qualificado', 'proposta', 'convertido', 'perdido'])
        ).count()
        converted  = Lead.query.filter_by(import_batch=b.batch_id, status='convertido').count()
        lost       = Lead.query.filter_by(import_batch=b.batch_id, status='perdido').count()
        new_leads  = Lead.query.filter_by(import_batch=b.batch_id, status='novo').count()

        # cotações vinculadas via oportunidades
        lead_ids = [r[0] for r in db.session.query(Lead.id).filter_by(import_batch=b.batch_id).all()]
        opp_ids  = [r[0] for r in db.session.query(Opportunity.id).filter(Opportunity.lead_id.in_(lead_ids)).all()] if lead_ids else []
        quotes_count  = Quote.query.filter(Quote.opportunity_id.in_(opp_ids)).count() if opp_ids else 0
        freights_count = Freight.query.join(Quote, Freight.quote_id == Quote.id).filter(
            Quote.opportunity_id.in_(opp_ids)
        ).count() if opp_ids else 0

        coverage_pct = round(contacted / total * 100) if total else 0
        conversion_pct = round(converted / total * 100, 1) if total else 0

        # atualizar total se divergir
        if b.total_leads != total:
            b.total_leads = total

        batch_stats.append({
            'batch':          b,
            'total':          total,
            'contacted':      contacted,
            'new_leads':      new_leads,
            'converted':      converted,
            'lost':           lost,
            'quotes':         quotes_count,
            'freights':       freights_count,
            'coverage_pct':   coverage_pct,
            'conversion_pct': conversion_pct,
            'exhausted':      coverage_pct >= 80,
        })

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    # lotes sem registro formal (importados antes da feature)
    registered_ids = {b.batch_id for b in batches}
    orphan_batches = db.session.query(Lead.import_batch, _func.count(Lead.id)).filter(
        Lead.import_batch.isnot(None),
        Lead.import_batch != '',
        Lead.import_batch.notin_(list(registered_ids)) if registered_ids else True,
    ).group_by(Lead.import_batch).order_by(Lead.import_batch.desc()).all()

    return render_template('crm/batches/index.html',
        batch_stats=batch_stats,
        orphan_batches=orphan_batches,
        SOURCES_DICT={s[0]: s[1] for s in SOURCES},
    )


@crm_bp.route('/lotes/<int:id>/edit', methods=['POST'])
@login_required
@crm_required
def batches_edit(id):
    batch = LeadBatch.query.get_or_404(id)
    name = request.form.get('name', '').strip()
    notes = request.form.get('notes', '').strip()
    if name:
        batch.name  = name[:200]
        batch.notes = notes
        db.session.commit()
        flash('Lote atualizado.', 'success')
    return redirect(url_for('crm.batches_index'))


@crm_bp.route('/lotes/<int:id>/delete', methods=['POST'])
@login_required
@crm_required
def batches_delete(id):
    batch = LeadBatch.query.get_or_404(id)
    name  = batch.name
    db.session.delete(batch)
    db.session.commit()
    flash(f'Registro do lote "{name}" removido (os leads permanecem).', 'success')
    return redirect(url_for('crm.batches_index'))


@crm_bp.route('/lotes/registrar-orfao', methods=['POST'])
@login_required
@crm_required
def batches_register_orphan():
    batch_id = request.form.get('batch_id', '').strip()
    name     = request.form.get('name', '').strip() or batch_id
    if not batch_id:
        flash('ID do lote inválido.', 'error')
        return redirect(url_for('crm.batches_index'))
    if LeadBatch.query.filter_by(batch_id=batch_id).first():
        flash('Lote já registrado.', 'warning')
        return redirect(url_for('crm.batches_index'))
    total = Lead.query.filter_by(import_batch=batch_id).count()
    lb = LeadBatch(
        batch_id    = batch_id,
        name        = name[:200],
        total_leads = total,
        created_by  = current_user.id,
    )
    db.session.add(lb)
    db.session.commit()
    flash(f'Lote "{name}" registrado com {total} leads.', 'success')
    return redirect(url_for('crm.batches_index'))


@crm_bp.route('/leads/<int:id>/delete', methods=['POST'])
@login_required
@crm_required
def leads_delete(id):
    lead = Lead.query.get_or_404(id)
    name = lead.company_name
    db.session.delete(lead)
    db.session.commit()
    flash(f'Lead "{name}" removido.', 'success')
    return redirect(url_for('crm.leads_index'))


@crm_bp.route('/leads/<int:id>/status', methods=['POST'])
@login_required
@crm_required
def leads_update_status(id):
    lead = Lead.query.get_or_404(id)
    new_status = (request.json or {}).get('status') or request.form.get('status')
    if new_status not in [s[0] for s in LEAD_STATUSES]:
        return jsonify({'success': False}), 400
    lead.status     = new_status
    lead.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})


@crm_bp.route('/leads/<int:id>/activity', methods=['POST'])
@login_required
@crm_required
def leads_add_activity(id):
    lead = Lead.query.get_or_404(id)
    title = request.form.get('title', '').strip()
    if not title:
        flash('Título da atividade é obrigatório.', 'error')
        return redirect(url_for('crm.leads_show', id=id))

    is_done      = request.form.get('is_done') == '1'
    scheduled_dt = _parse_dt(request.form.get('scheduled_at'))

    act = CRMActivity(
        lead_id=id,
        activity_type=request.form.get('activity_type', 'nota'),
        title=title,
        notes=request.form.get('notes', '').strip(),
        scheduled_at=scheduled_dt,
        is_done=is_done,
        completed_at=datetime.utcnow() if is_done else None,
        outcome=request.form.get('outcome', '').strip(),
        created_by=current_user.id,
    )
    db.session.add(act)

    # Atualizar etapa do lead se solicitado
    new_status = request.form.get('new_status', '').strip()
    valid_statuses = [s[0] for s in LEAD_STATUSES]
    if new_status and new_status in valid_statuses:
        lead.status = new_status

    # Sempre recalcular next_contact_at — pega o mínimo de todos os follow-ups pendentes
    db.session.flush()  # garante que o novo act.id está na sessão antes de recalcular
    next_pending = CRMActivity.query.filter_by(lead_id=id, is_done=False)\
                  .filter(CRMActivity.scheduled_at.isnot(None))\
                  .order_by(CRMActivity.scheduled_at).first()
    lead.next_contact_at = next_pending.scheduled_at if next_pending else None

    lead.updated_at = datetime.utcnow()
    db.session.commit()
    flash('Atividade registrada!', 'success')
    return redirect(url_for('crm.leads_show', id=id))

# ── Pipeline ──────────────────────────────────────────────────────────────────

@crm_bp.route('/pipeline')
@login_required
@crm_required
def pipeline():
    # Sincronizar valores com cotações vinculadas
    _sync_opportunity_values()

    # ── Auto-arquivamento: ganho/perdido com > 90 dias ─────────────────────────
    archive_cutoff = datetime.utcnow() - timedelta(days=90)
    to_archive = Opportunity.query.filter(
        Opportunity.stage.in_(['ganho', 'perdido']),
        Opportunity.updated_at < archive_cutoff,
        Opportunity.archived == False
    ).all()
    if to_archive:
        for o in to_archive:
            o.archived = True
        db.session.commit()

    # ── Estágios ativos — apenas não-arquivados ────────────────────────────────
    ACTIVE_STAGE_IDS = [s[0] for s in STAGES if s[0] not in ('ganho', 'perdido')]
    stages_data = []
    for sid, sname, color, prob in STAGES:
        if sid in ('ganho', 'perdido'):
            continue
        opps = Opportunity.query.filter_by(stage=sid, archived=False)\
                                .order_by(Opportunity.updated_at.desc()).all()
        total_val = sum(o.value or 0 for o in opps)
        stages_data.append({'id': sid, 'name': sname, 'color': color, 'prob': prob,
                             'opps': opps, 'count': len(opps), 'total_value': total_val})

    total_pipeline = sum(s['total_value'] for s in stages_data)

    # ── Sumário recente ganho/perdido (últimos 90 dias, não-arquivados) ────────
    won_recent  = Opportunity.query.filter_by(stage='ganho',   archived=False).all()
    lost_recent = Opportunity.query.filter_by(stage='perdido', archived=False).all()
    won_summary  = {'count': len(won_recent),  'value': sum(o.value or 0 for o in won_recent)}
    lost_summary = {'count': len(lost_recent), 'value': sum(o.value or 0 for o in lost_recent)}

    # ── Total arquivado ────────────────────────────────────────────────────────
    archived_count = Opportunity.query.filter_by(archived=True).count()

    return render_template('crm/pipeline.html',
        stages_data=stages_data, total_pipeline=total_pipeline,
        STAGES=STAGES, today=date.today(),
        won_summary=won_summary, lost_summary=lost_summary,
        archived_count=archived_count)


@crm_bp.route('/pipeline/historico')
@login_required
@crm_required
def pipeline_historico():
    """Histórico completo: oportunidades arquivadas + ganho/perdido recentes."""
    period  = request.args.get('periodo', '90')   # 30 | 90 | 180 | 365 | tudo
    stage_f = request.args.get('stage', 'todos')  # todos | ganho | perdido
    q       = request.args.get('q', '').strip()

    # Base: arquivados OU ganho/perdido não-arquivados (recentes)
    base = Opportunity.query.filter(
        db.or_(
            Opportunity.archived == True,
            Opportunity.stage.in_(['ganho', 'perdido'])
        )
    )

    if period != 'tudo':
        try:
            days   = int(period)
            cutoff = datetime.utcnow() - timedelta(days=days)
            base   = base.filter(Opportunity.updated_at >= cutoff)
        except ValueError:
            pass

    if stage_f in ('ganho', 'perdido'):
        base = base.filter(Opportunity.stage == stage_f)

    if q:
        base = base.filter(
            db.or_(
                Opportunity.title.ilike(f'%{q}%'),
            )
        )

    opps = base.order_by(Opportunity.updated_at.desc()).all()

    # Totalizadores
    won_total  = sum(o.value or 0 for o in opps if o.stage == 'ganho')
    lost_count = sum(1 for o in opps if o.stage == 'perdido')
    won_count  = sum(1 for o in opps if o.stage == 'ganho')

    return render_template('crm/pipeline_historico.html',
        opps=opps, period=period, stage_f=stage_f, q=q,
        won_total=won_total, won_count=won_count, lost_count=lost_count,
        today=date.today())

# ── Opportunities ─────────────────────────────────────────────────────────────

@crm_bp.route('/opportunities/new', methods=['GET', 'POST'])
@login_required
@crm_required
def opp_new():
    lead_id = request.args.get('lead_id', type=int) or request.form.get('lead_id', type=int)
    lead    = Lead.query.get(lead_id) if lead_id else None

    if request.method == 'POST':
        lid = request.form.get('lead_id', type=int)
        if not lid:
            flash('Selecione um lead.', 'error')
            return redirect(url_for('crm.pipeline'))
        title = request.form.get('title', '').strip()
        if not title:
            flash('Título é obrigatório.', 'error')
            return redirect(request.referrer or url_for('crm.leads_show', id=lid))
        opp = Opportunity(
            lead_id=lid,
            title=title,
            stage=request.form.get('stage', 'prospeccao'),
            value=_safe_float(request.form.get('value')),
            freight_origin=request.form.get('freight_origin', '').strip(),
            freight_destination=request.form.get('freight_destination', '').strip(),
            cargo_type=request.form.get('cargo_type', '').strip(),
            vehicle_type=request.form.get('vehicle_type', '').strip(),
            frequency=request.form.get('frequency', '').strip(),
            probability=_safe_int(request.form.get('probability')) or 10,
            expected_close_date=_parse_date(request.form.get('expected_close_date')),
            assigned_to=_safe_int(request.form.get('assigned_to')) or current_user.id,
        )
        db.session.add(opp)
        if lead:
            lead.updated_at = datetime.utcnow()
        db.session.commit()
        flash(f'Oportunidade "{opp.title}" criada!', 'success')
        return redirect(url_for('crm.leads_show', id=opp.lead_id))

    all_leads = Lead.query.order_by(Lead.company_name).all()
    return render_template('crm/opportunities/form.html',
        lead=lead, opp=None, STAGES=STAGES,
        VEHICLE_TYPES=VEHICLE_TYPES, FREQUENCIES=FREQUENCIES,
        sellers=_get_sellers(), all_leads=all_leads)


@crm_bp.route('/opportunities/<int:id>/quote')
@login_required
@crm_required
def opp_create_quote(id):
    """Redireciona para nova cotação pré-preenchida com dados da oportunidade."""
    opp = Opportunity.query.get_or_404(id)
    lead = Lead.query.get(opp.lead_id)
    params = {'opportunity_id': opp.id}
    if lead and lead.converted_client_id:
        params['prefill_client_id'] = lead.converted_client_id
    return redirect(url_for('quotes.new', **params))


@crm_bp.route('/guide')
@login_required
@crm_required
def guide():
    return render_template('crm/guide.html')


@crm_bp.route('/opportunities/<int:id>/stage', methods=['POST'])
@login_required
@crm_required
def opp_update_stage(id):
    opp = Opportunity.query.get_or_404(id)
    data = request.json or {}
    new_stage = data.get('stage') or request.form.get('stage')
    if new_stage not in STAGE_DICT:
        return jsonify({'success': False, 'message': 'Estágio inválido'}), 400
    opp.stage       = new_stage
    opp.probability = STAGE_DICT[new_stage][3]
    opp.updated_at  = datetime.utcnow()
    if new_stage == 'perdido':
        opp.lost_reason = data.get('lost_reason') or request.form.get('lost_reason', '')
    db.session.commit()
    return jsonify({'success': True, 'stage': new_stage, 'stage_name': STAGE_DICT[new_stage][1]})


@crm_bp.route('/opportunities/<int:id>/archive', methods=['POST'])
@login_required
@crm_required
def opp_archive(id):
    """Arquiva manualmente uma oportunidade ganho/perdida."""
    opp = Opportunity.query.get_or_404(id)
    if opp.stage not in ('ganho', 'perdido'):
        return jsonify({'success': False, 'message': 'Só é possível arquivar oportunidades ganhas ou perdidas'}), 400
    opp.archived   = True
    opp.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})


@crm_bp.route('/opportunities/<int:id>/restore', methods=['POST'])
@login_required
@crm_required
def opp_restore(id):
    """Restaura uma oportunidade arquivada de volta ao pipeline ativo."""
    opp = Opportunity.query.get_or_404(id)
    opp.archived   = False
    opp.stage      = 'prospeccao'
    opp.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True, 'message': f'"{opp.title}" restaurada para Prospecção.'})


@crm_bp.route('/opportunities/<int:id>/delete', methods=['POST'])
@login_required
@crm_required
def opp_delete(id):
    opp     = Opportunity.query.get_or_404(id)
    lead_id = opp.lead_id
    db.session.delete(opp)
    db.session.commit()
    flash('Oportunidade removida.', 'success')
    return redirect(url_for('crm.leads_show', id=lead_id))

# ── Activities ────────────────────────────────────────────────────────────────

@crm_bp.route('/activities')
@login_required
@crm_required
def activities_index():
    """Lista completa de atividades pendentes — visão do time comercial."""
    tipo_f    = request.args.get('tipo', '')
    periodo_f = request.args.get('periodo', '')   # atrasadas | hoje | semana | todas

    today       = date.today()
    today_start = datetime.combine(today, datetime.min.time())
    today_end   = datetime.combine(today, datetime.max.time())
    week_end    = datetime.combine(today + timedelta(days=7), datetime.max.time())

    q = CRMActivity.query.filter_by(is_done=False)

    if tipo_f:
        q = q.filter(CRMActivity.activity_type == tipo_f)

    if periodo_f == 'atrasadas':
        q = q.filter(
            CRMActivity.scheduled_at.isnot(None),
            CRMActivity.scheduled_at < today_start
        )
    elif periodo_f == 'hoje':
        q = q.filter(CRMActivity.scheduled_at.between(today_start, today_end))
    elif periodo_f == 'semana':
        q = q.filter(CRMActivity.scheduled_at.between(today_start, week_end))
    # 'todas' ou vazio → sem filtro de data

    activities = q.order_by(
        CRMActivity.scheduled_at.asc().nullslast()
    ).all()

    # Enriquecer com flag de atraso
    now = datetime.utcnow()
    for act in activities:
        act._overdue = (
            act.scheduled_at is not None and act.scheduled_at < now
        )
        act._today = (
            act.scheduled_at is not None and
            today_start <= act.scheduled_at <= today_end
        )

    total_overdue = CRMActivity.query.filter(
        CRMActivity.is_done == False,
        CRMActivity.scheduled_at.isnot(None),
        CRMActivity.scheduled_at < today_start,
    ).count()
    total_today = CRMActivity.query.filter(
        CRMActivity.is_done == False,
        CRMActivity.scheduled_at.between(today_start, today_end),
    ).count()
    total_pending = CRMActivity.query.filter_by(is_done=False).count()

    return render_template(
        'crm/activities.html',
        activities=activities,
        ACTIVITY_TYPES=ACTIVITY_TYPES,
        AT_DICT=AT_DICT,
        tipo_f=tipo_f, periodo_f=periodo_f,
        total_overdue=total_overdue,
        total_today=total_today,
        total_pending=total_pending,
        today=today,
    )


@crm_bp.route('/activities/<int:id>/done', methods=['POST'])
@login_required
@crm_required
def activity_done(id):
    act              = CRMActivity.query.get_or_404(id)
    act.is_done      = True
    act.completed_at = datetime.utcnow()
    outcome = (request.json or {}).get('outcome') or request.form.get('outcome', '')
    if outcome:
        act.outcome = outcome

    # Recalcular next_contact_at do lead
    lead = Lead.query.get(act.lead_id)
    if lead:
        pending = CRMActivity.query.filter(
            CRMActivity.lead_id == lead.id,
            CRMActivity.id != act.id,
            CRMActivity.is_done == False,
            CRMActivity.scheduled_at.isnot(None),
        ).order_by(CRMActivity.scheduled_at).first()
        lead.next_contact_at = pending.scheduled_at if pending else None
        lead.updated_at = datetime.utcnow()

    db.session.commit()

    # Se veio de um formulário HTML (não AJAX), redireciona de volta ao lead
    if request.form.get('_redirect_lead'):
        return redirect(url_for('crm.leads_show', id=act.lead_id))
    return jsonify({'success': True})


@crm_bp.route('/activities/<int:id>/reschedule', methods=['POST'])
@login_required
@crm_required
def activity_reschedule(id):
    """Reagenda uma atividade pendente (atualiza scheduled_at sem criar novo registro)."""
    act = CRMActivity.query.get_or_404(id)
    new_dt = _parse_dt(request.form.get('new_scheduled_at'))
    if not new_dt:
        flash('Data inválida para reagendamento.', 'error')
        return redirect(url_for('crm.leads_show', id=act.lead_id))

    act.scheduled_at = new_dt
    act.is_done      = False
    act.completed_at = None

    # Atualizar next_contact_at do lead
    lead = Lead.query.get(act.lead_id)
    if lead:
        pending = CRMActivity.query.filter(
            CRMActivity.lead_id == lead.id,
            CRMActivity.is_done == False,
            CRMActivity.scheduled_at.isnot(None),
        ).order_by(CRMActivity.scheduled_at).first()
        lead.next_contact_at = pending.scheduled_at if pending else None
        lead.updated_at = datetime.utcnow()

    db.session.commit()
    flash('Atividade reagendada com sucesso!', 'success')
    return redirect(url_for('crm.leads_show', id=act.lead_id))


@crm_bp.route('/activities/<int:id>/delete', methods=['POST'])
@login_required
@crm_required
def activity_delete(id):
    act     = CRMActivity.query.get_or_404(id)
    lead_id = act.lead_id
    db.session.delete(act)
    db.session.commit()
    flash('Atividade removida.', 'success')
    return redirect(request.referrer or url_for('crm.leads_show', id=lead_id))

# ── Helpers ───────────────────────────────────────────────────────────────────

def _sync_opportunity_values():
    """Sincroniza opp.value com o sale_value da cotação mais recente vinculada."""
    try:
        all_opps = Opportunity.query.filter(Opportunity.id.isnot(None)).all()
        changed = False
        for opp in all_opps:
            best = Quote.query.filter(
                Quote.opportunity_id == opp.id,
                Quote.sale_value.isnot(None),
                Quote.sale_value > 0
            ).order_by(Quote.created_at.desc()).first()
            if best and best.sale_value != opp.value:
                opp.value      = best.sale_value
                opp.updated_at = datetime.utcnow()
                changed = True
        if changed:
            db.session.commit()
    except Exception:
        db.session.rollback()


def _get_sellers():
    return User.query.filter(
        User.role.in_(['admin', 'operador', 'vendedor']),
        User.active == True
    ).order_by(User.username).all()

def _safe_float(v):
    try:
        return float(str(v).replace(',', '.').replace('R$', '').replace(' ', '')) if v else None
    except Exception:
        return None

def _safe_int(v):
    try:
        return int(v) if v else None
    except Exception:
        return None

def _parse_dt(s):
    if not s:
        return None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None

def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None

def _get_clients_json():
    """Retorna lista de clientes ativos em JSON para o formulário de lead."""
    clients = Client.query.filter(Client.active != False).order_by(Client.company_name).all()  # noqa: E712
    return json.dumps([{
        'id': c.id,
        'company_name': c.company_name,
        'cnpj': c.cnpj or '',
        'city': c.city or '',
        'state': c.state or '',
    } for c in clients])
