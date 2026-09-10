from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, Response
from flask_login import login_required, current_user
from models import Client, Freight, FreightStatusLog, Quote
from app import db
from utils.cnpj_validator import validate_cnpj
import json
import logging
import requests
import calendar
from collections import Counter
from datetime import date, timedelta
from sqlalchemy.orm import joinedload

clients_bp = Blueprint('clients', __name__, url_prefix='/clients')
logger = logging.getLogger(__name__)

@clients_bp.route('/')
@login_required
def index():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))

    search = request.args.get('search', '')
    status = request.args.get('status', 'all')
    page   = request.args.get('page', 1, type=int)

    query = Client.query

    if search:
        query = query.filter(
            Client.company_name.contains(search) |
            Client.cnpj.contains(search) |
            Client.trade_name.contains(search)
        )

    if status == 'active':
        query = query.filter_by(active=True)
    elif status == 'inactive':
        query = query.filter_by(active=False)

    pagination = query.order_by(Client.company_name).paginate(page=page, per_page=20, error_out=False)
    clients    = pagination.items

    return render_template('clients/index.html',
        clients=clients, search=search, status=status, pagination=pagination)

@clients_bp.route('/<int:id>')
@login_required
def view(id):
    client = Client.query.get_or_404(id)

    if current_user.role == 'cliente':
        if current_user.client_id != id:
            flash('Acesso negado.', 'error')
            return redirect(url_for('dashboard.index'))
    elif current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))

    today = date.today()
    month_start = today.replace(day=1)

    freights = (Freight.query
                .filter_by(client_id=id)
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
            actual = log.created_at.date() if log else f.updated_at.date() if f.updated_at else None
            if actual:
                if actual <= f.delivery_date:
                    on_time_count += 1
                else:
                    late_count += 1
            else:
                on_time_count += 1
    on_time_rate = round(on_time_count / len(delivered) * 100) if delivered else 0

    # Revenue
    total_revenue    = sum(f.agreed_price or 0 for f in freights)
    month_revenue    = sum(f.agreed_price or 0 for f in this_month)
    delivered_revenue = sum(f.agreed_price or 0 for f in delivered)

    # Top routes
    route_counter = Counter()
    for f in freights:
        if f.origin_city and f.destination_city:
            o = f"{f.origin_city}/{f.origin_state}" if f.origin_state else f.origin_city
            d = f"{f.destination_city}/{f.destination_state}" if f.destination_state else f.destination_city
            route_counter[f"{o} → {d}"] += 1
    top_routes = route_counter.most_common(8)

    # Load type distribution
    load_type_counter = Counter()
    for f in freights:
        lt = (f.quote.load_type if f.quote else None) or 'desconhecido'
        load_type_counter[lt] += 1

    # Vehicle type distribution
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
        'Ofertado':   sum(1 for f in freights if f.status == 'ofertado'),
        'Aceito':     sum(1 for f in freights if f.status == 'aceito'),
        'Em Trânsito': len(in_transit),
        'Entregue':   len(delivered),
        'Cancelado':  len(cancelled),
    }

    # Contacts
    try:
        contacts = json.loads(client.contacts) if client.contacts else []
    except Exception:
        contacts = []

    # Average ticket
    avg_ticket = round(total_revenue / len(freights), 2) if freights else 0

    # Portal users for this client
    from models import User as _User
    client_users = _User.query.filter_by(client_id=client.id).order_by(_User.username).all()

    return render_template('clients/view.html',
        client=client,
        contacts=contacts,
        freights=freights,
        client_users=client_users,
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
        delivered_revenue=delivered_revenue,
        avg_ticket=avg_ticket,
        top_routes=top_routes,
        load_type_counter=dict(load_type_counter),
        vehicle_counter=dict(vehicle_counter),
        status_dist=status_dist,
        monthly_labels=json.dumps(monthly_labels),
        monthly_volumes=json.dumps(monthly_volumes),
        monthly_revenues=json.dumps(monthly_revenues),
        today=today,
    )


@clients_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('clients.index'))
    
    if request.method == 'POST':
        try:
            # Validate CNPJ
            cnpj = request.form['cnpj'].replace('.', '').replace('/', '').replace('-', '')
            cnpj_data = validate_cnpj(cnpj)
            
            if not cnpj_data['valid']:
                flash(cnpj_data['message'], 'error')
                return render_template('clients/form.html')
            
            # Handle multiple contacts
            contacts = []
            for i in range(1, 6):  # Support up to 5 contacts
                name = request.form.get(f'contact_name_{i}')
                email = request.form.get(f'contact_email_{i}')
                phone = request.form.get(f'contact_phone_{i}')
                
                if name and email:
                    contacts.append({
                        'name': name,
                        'email': email,
                        'phone': phone,
                        'position': request.form.get(f'contact_position_{i}', '')
                    })
            
            # Create client
            client = Client()
            client.company_name = request.form['company_name']
            client.cnpj = cnpj
            client.trade_name = request.form.get('trade_name')
            client.phone = request.form['phone']
            client.email = request.form['email']
            # New address fields
            client.street = request.form.get('street')
            client.number = request.form.get('number')
            client.complement = request.form.get('complement')
            client.neighborhood = request.form.get('neighborhood')
            client.city = request.form.get('city')
            client.state = request.form.get('state')
            client.cep = request.form.get('cep')
            # Legacy address field for backward compatibility - construct from new fields
            address_parts = []
            if client.street:
                address_parts.append(client.street)
            if client.number:
                address_parts.append(f"nº {client.number}")
            if client.complement:
                address_parts.append(client.complement)
            if client.neighborhood:
                address_parts.append(client.neighborhood)
            if client.city:
                address_parts.append(client.city)
            if client.state:
                address_parts.append(client.state)
            if client.cep:
                address_parts.append(f"CEP: {client.cep}")
            
            client.address = ', '.join(address_parts) if address_parts else ''
            client.contacts = json.dumps(contacts)
            client.created_by = current_user.id
            
            db.session.add(client)
            db.session.commit()
            
            logger.info(f"Cliente criado por {current_user.username}: {client.company_name}")
            flash('Cliente cadastrado com sucesso!', 'success')
            return redirect(url_for('clients.index'))

        except Exception as e:
            db.session.rollback()
            logger.error(f"Erro ao cadastrar cliente: {type(e).__name__}: {e}", exc_info=True)
            flash(f'Erro ao cadastrar cliente: {str(e)}', 'error')
    
    return render_template('clients/form.html')

@clients_bp.route('/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit(id):
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('clients.index'))
    
    client = Client.query.get_or_404(id)
    
    if request.method == 'POST':
        try:
            old_values = f"Nome: {client.company_name}, CNPJ: {client.cnpj}"
            
            # Handle multiple contacts
            contacts = []
            for i in range(1, 6):  # Support up to 5 contacts
                name = request.form.get(f'contact_name_{i}')
                email = request.form.get(f'contact_email_{i}')
                phone = request.form.get(f'contact_phone_{i}')
                
                if name and email:
                    contacts.append({
                        'name': name,
                        'email': email,
                        'phone': phone,
                        'position': request.form.get(f'contact_position_{i}', '')
                    })
            
            # Update client data
            client.company_name = request.form['company_name']
            client.trade_name = request.form.get('trade_name')
            client.phone = request.form['phone']
            client.email = request.form['email']
            # Update address fields
            client.street = request.form.get('street')
            client.number = request.form.get('number')
            client.complement = request.form.get('complement')
            client.neighborhood = request.form.get('neighborhood')
            client.city = request.form.get('city')
            client.state = request.form.get('state')
            client.cep = request.form.get('cep')
            # Update legacy address field from new fields
            address_parts = []
            if client.street:
                address_parts.append(client.street)
            if client.number:
                address_parts.append(f"nº {client.number}")
            if client.complement:
                address_parts.append(client.complement)
            if client.neighborhood:
                address_parts.append(client.neighborhood)
            if client.city:
                address_parts.append(client.city)
            if client.state:
                address_parts.append(client.state)
            if client.cep:
                address_parts.append(f"CEP: {client.cep}")
            
            client.address = ', '.join(address_parts) if address_parts else ''
            client.contacts = json.dumps(contacts)
            
            db.session.commit()
            
            logger.info(f"Cliente atualizado por {current_user.username}: {client.company_name}")
            
            flash('Cliente atualizado com sucesso!', 'success')
            return redirect(url_for('clients.index'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao atualizar cliente: {str(e)}', 'error')
    
    # Parse contacts for form - don't modify the model, just add to template context
    try:
        parsed_contacts = json.loads(client.contacts) if client.contacts else []
    except:
        parsed_contacts = []

    from models import User
    client_users = User.query.filter_by(client_id=client.id).order_by(User.username).all()
    from datetime import datetime as _dt
    return render_template('clients/form.html', client=client, parsed_contacts=parsed_contacts,
                           client_users=client_users, current_year=_dt.now().year)

@clients_bp.route('/<int:id>/toggle-status', methods=['POST'])
@login_required
def toggle_status(id):
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'message': 'Acesso negado'})
    
    client = Client.query.get_or_404(id)
    old_status = client.active
    client.active = not client.active
    
    db.session.commit()
    
    logger.info(f"Status do cliente alterado por {current_user.username}: {client.company_name}")
    
    return jsonify({
        'success': True, 
        'new_status': client.active,
        'message': f'Cliente {"ativado" if client.active else "desativado"} com sucesso!'
    })

@clients_bp.route('/validate-cnpj')
@login_required
def validate_cnpj_route():
    cnpj = request.args.get('cnpj', '').replace('.', '').replace('/', '').replace('-', '')
    
    if len(cnpj) != 14:
        return jsonify({'valid': False, 'message': 'CNPJ deve ter 14 dígitos'})
    
    result = validate_cnpj(cnpj)
    return jsonify(result)

@clients_bp.route('/api/cnpj/<cnpj>')
@login_required
def get_cnpj_data(cnpj):
    """API endpoint to fetch CNPJ data from Receita Federal"""
    try:
        # Clean CNPJ
        clean_cnpj = ''.join(filter(str.isdigit, cnpj))
        
        if len(clean_cnpj) != 14:
            return jsonify({
                'success': False, 
                'message': 'CNPJ deve ter 14 dígitos'
            })
        
        # Validate CNPJ
        if not validate_cnpj_algorithm(clean_cnpj):
            return jsonify({
                'success': False,
                'message': 'CNPJ inválido'
            })
        
        # Fetch data from ReceitaWS API
        url = f'https://receitaws.com.br/v1/cnpj/{clean_cnpj}'
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            if data.get('status') == 'OK':
                return jsonify({
                    'success': True,
                    'data': {
                        'nome': data.get('nome', ''),
                        'fantasia': data.get('fantasia', ''),
                        'logradouro': data.get('logradouro', ''),
                        'numero': data.get('numero', ''),
                        'complemento': data.get('complemento', ''),
                        'bairro': data.get('bairro', ''),
                        'municipio': data.get('municipio', ''),
                        'uf': data.get('uf', ''),
                        'cep': data.get('cep', '').replace('-', ''),
                        'telefone': data.get('telefone', ''),
                        'email': data.get('email', ''),
                        'situacao': data.get('situacao', ''),
                        'data_situacao': data.get('data_situacao', ''),
                        'atividade_principal': data.get('atividade_principal', [])
                    }
                })
            else:
                return jsonify({
                    'success': False,
                    'message': data.get('message', 'CNPJ não encontrado')
                })
        else:
            return jsonify({
                'success': False,
                'message': 'Erro ao conectar com o serviço de consulta'
            })
            
    except requests.exceptions.Timeout:
        return jsonify({
            'success': False,
            'message': 'Timeout ao consultar CNPJ'
        })
    except requests.exceptions.RequestException:
        return jsonify({
            'success': False,
            'message': 'Erro de conexão'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': 'Erro interno do servidor'
        })

@clients_bp.route('/manage-emails')
@login_required
def manage_notification_emails():
    if current_user.role != 'cliente' or not current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))
    return render_template('clients/manage_emails.html')


@clients_bp.route('/update-notification-emails', methods=['POST'])
@login_required
def update_notification_emails():
    if current_user.role != 'cliente' or not current_user.client_id:
        return jsonify({'success': False, 'message': 'Acesso negado'})
    try:
        client = Client.query.get(current_user.client_id)
        emails = []
        for i in range(1, 5):
            e = request.form.get(f'email_{i}', '').strip().lower()
            if e and '@' in e:
                emails.append(e)
        client.notification_emails = json.dumps(emails)
        db.session.commit()
        return jsonify({'success': True, 'message': f'{len(emails)} email(s) salvos com sucesso.'})
    except Exception as exc:
        db.session.rollback()
        logging.error(f'update_notification_emails: {exc}')
        return jsonify({'success': False, 'message': 'Erro ao salvar emails.'})


@clients_bp.route('/test-notification-emails', methods=['POST'])
@login_required
def test_notification_emails():
    if current_user.role != 'cliente' or not current_user.client_id:
        return jsonify({'success': False, 'message': 'Acesso negado'})
    try:
        client = Client.query.get(current_user.client_id)
        emails = []
        for i in range(1, 5):
            e = request.form.get(f'email_{i}', '').strip().lower()
            if e and '@' in e:
                emails.append(e)
        all_emails = [client.email] + emails
        try:
            from utils.email_service import send_email
            for addr in all_emails:
                send_email(
                    to=addr,
                    subject='Teste de Notificação — EMALOG',
                    body='<p>Este é um email de teste das notificações EMALOG. Se recebeu, as configurações estão corretas.</p>'
                )
        except Exception as mail_exc:
            logging.warning(f'test_notification_emails mail error: {mail_exc}')
        return jsonify({'success': True, 'email_count': len(all_emails)})
    except Exception as exc:
        db.session.rollback()
        logging.error(f'test_notification_emails: {exc}')
        return jsonify({'success': False, 'message': 'Erro ao enviar email de teste.'})


@clients_bp.route('/api/find-or-create', methods=['POST'])
@login_required
def find_or_create_by_cnpj():
    """Busca cliente pelo CNPJ. Se não existir, cria automaticamente via Receita Federal."""
    import re as _re
    raw = (request.json or {}).get('cnpj', '') or request.form.get('cnpj', '')
    clean = _re.sub(r'\D', '', raw)

    if len(clean) != 14:
        return jsonify({'success': False, 'message': 'CNPJ inválido (deve ter 14 dígitos)'})

    if not validate_cnpj_algorithm(clean):
        return jsonify({'success': False, 'message': 'CNPJ com dígitos verificadores inválidos'})

    # ── Formatar CNPJ padrão ──────────────────────────────────────────────
    fmt = f'{clean[:2]}.{clean[2:5]}.{clean[5:8]}/{clean[8:12]}-{clean[12:]}'

    # ── Já está cadastrado? ───────────────────────────────────────────────
    existing = Client.query.filter(
        Client.cnpj.in_([clean, fmt])
    ).first()
    if not existing:
        # fallback: match direto via SQL removendo não-numéricos
        from sqlalchemy import func as _func
        existing = Client.query.filter(
            _func.regexp_replace(Client.cnpj, r'\D', '', 'g') == clean
        ).first()

    if existing:
        return jsonify({
            'success': True,
            'status': 'found',
            'message': f'Cliente já cadastrado: {existing.company_name}',
            'client': {'id': existing.id, 'name': existing.company_name}
        })

    # ── Não existe — consulta Receita Federal ─────────────────────────────
    try:
        resp = requests.get(f'https://receitaws.com.br/v1/cnpj/{clean}', timeout=10)
    except Exception:
        return jsonify({'success': False, 'message': 'Erro ao conectar com a Receita Federal'})

    if resp.status_code != 200:
        return jsonify({'success': False, 'message': 'Receita Federal não respondeu'})

    d = resp.json()
    if d.get('status') != 'OK':
        return jsonify({'success': False, 'message': d.get('message', 'CNPJ não encontrado na Receita Federal')})

    company_name = (d.get('fantasia') or d.get('nome') or 'Empresa')[:100]
    phone        = _re.sub(r'\D', '', d.get('telefone', ''))[:20] or '00000000000'
    email        = (d.get('email') or f'contato@{clean}.temp')[:120].lower()
    city         = (d.get('municipio') or '')[:100]
    state        = (d.get('uf') or '')[:2]
    address      = ' '.join(filter(None, [
        d.get('logradouro', ''), d.get('numero', ''), d.get('bairro', '')
    ]))

    try:
        new_client = Client(
            company_name = company_name,
            cnpj         = fmt,
            phone        = phone or '00000000000',
            email        = email,
            city         = city,
            state        = state,
            address      = address[:500] if address else None,
            active       = True,
            is_active    = True,
        )
        db.session.add(new_client)
        db.session.commit()
        logging.info(f'Cliente criado automaticamente via CNPJ: {fmt} — {company_name}')
        return jsonify({
            'success': True,
            'status': 'created',
            'message': f'Cliente criado automaticamente: {company_name}',
            'client': {'id': new_client.id, 'name': new_client.company_name}
        })
    except Exception as exc:
        db.session.rollback()
        logging.error(f'Erro ao criar cliente via CNPJ {fmt}: {exc}')
        return jsonify({'success': False, 'message': 'Erro ao salvar cliente no banco de dados'})


def validate_cnpj_algorithm(cnpj):
    """Validate CNPJ using Brazilian algorithm"""
    cnpj = cnpj.replace('.', '').replace('/', '').replace('-', '')
    
    if len(cnpj) != 14:
        return False
    
    # Elimina CNPJs inválidos conhecidos
    if cnpj in ['00000000000000', '11111111111111', '22222222222222', 
                '33333333333333', '44444444444444', '55555555555555',
                '66666666666666', '77777777777777', '88888888888888', '99999999999999']:
        return False
    
    # Valida primeiro dígito verificador
    soma = 0
    peso = 2
    for i in range(11, -1, -1):
        soma += int(cnpj[i]) * peso
        peso += 1
        if peso == 10:
            peso = 2
    
    digito1 = 0 if soma % 11 < 2 else 11 - (soma % 11)
    
    if digito1 != int(cnpj[12]):
        return False
    
    # Valida segundo dígito verificador
    soma = 0
    peso = 2
    for i in range(12, -1, -1):
        soma += int(cnpj[i]) * peso
        peso += 1
        if peso == 10:
            peso = 2
    
    digito2 = 0 if soma % 11 < 2 else 11 - (soma % 11)
    
    return digito2 == int(cnpj[13])
