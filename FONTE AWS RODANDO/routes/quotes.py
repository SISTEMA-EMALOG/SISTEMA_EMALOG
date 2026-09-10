
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, make_response
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload
from models import db, Quote, Client, Driver, User, AuditLog, Lead, Opportunity
from datetime import datetime, timedelta
import json
import logging
from utils.excel_handler import export_quotes_to_excel
from utils.pdf_generator import generate_quote_pdf
from utils.email_service import send_quote_email
import os

quotes_bp = Blueprint('quotes', __name__, url_prefix='/quotes')

import re as _re_mod

def _validate_ms_stops(stops_raw):
    """Validate and sanitize multi-stop payload.

    Returns (ok: bool, error_msg: str | None, stops: list, total_weight: float)

    Validates:
    - stops_raw is a non-empty list
    - At least one 'coleta' and one 'entrega' stop
    - Each stop has type in ['coleta', 'entrega'], non-empty CEP (8 digits), non-empty number, non-empty date
    - cargo_items entries are dicts with numeric qty/weight
    - total weight > 0
    """
    if not isinstance(stops_raw, list) or not stops_raw:
        return False, 'Lista de paradas inválida ou vazia.', [], 0.0

    cleaned = []
    total_weight = 0.0

    for i, stop in enumerate(stops_raw, 1):
        if not isinstance(stop, dict):
            return False, f'Parada {i}: formato inválido.', [], 0.0

        stype = str(stop.get('type', '')).strip().lower()
        if stype not in ('coleta', 'entrega'):
            return False, f'Parada {i}: tipo deve ser "coleta" ou "entrega".', [], 0.0

        cep_digits = _re_mod.sub(r'\D', '', str(stop.get('cep', '') or ''))
        if len(cep_digits) != 8:
            return False, f'Parada {i} ({stype}): CEP inválido — informe 8 dígitos.', [], 0.0

        if not str(stop.get('number', '') or '').strip():
            return False, f'Parada {i} ({stype}): número do endereço obrigatório.', [], 0.0

        _date_raw = str(stop.get('date', '') or '').strip()
        if not _date_raw:
            return False, f'Parada {i} ({stype}): data obrigatória.', [], 0.0
        try:
            from datetime import datetime as _dt
            _dt.strptime(_date_raw, '%Y-%m-%d')
        except ValueError:
            return False, f'Parada {i} ({stype}): data inválida "{_date_raw}" — use o formato AAAA-MM-DD.', [], 0.0

        # Sanitize and validate cargo_items
        import math as _math
        clean_items = []
        for j, item in enumerate(stop.get('cargo_items', []), 1):
            if not isinstance(item, dict):
                continue
            try:
                qty = float(item.get('qty', 1) or 1)
                wt  = float(item.get('weight', 0) or 0)
                ln  = float(item.get('length', 0) or 0) or None
                wd  = float(item.get('width',  0) or 0) or None
                ht  = float(item.get('height', 0) or 0) or None
            except (TypeError, ValueError):
                return False, f'Parada {i} ({stype}), item {j}: valores numéricos inválidos.', [], 0.0
            for _v, _lbl in [(qty,'qty'),(wt,'peso'),(ln,'comp'),(wd,'larg'),(ht,'alt')]:
                if _v is not None and (_math.isnan(_v) or _math.isinf(_v)):
                    return False, f'Parada {i} ({stype}), item {j}: {_lbl} com valor inválido (NaN/infinito).', [], 0.0
            if qty <= 0:
                return False, f'Parada {i} ({stype}), item {j}: quantidade deve ser maior que zero.', [], 0.0
            if wt < 0:
                return False, f'Parada {i} ({stype}), item {j}: peso não pode ser negativo.', [], 0.0
            for _dv, _dl in [(ln,'comprimento'),(wd,'largura'),(ht,'altura')]:
                if _dv is not None and _dv < 0:
                    return False, f'Parada {i} ({stype}), item {j}: {_dl} não pode ser negativo.', [], 0.0
            total_weight += qty * wt
            clean_items.append({
                'desc': str(item.get('desc', '') or ''),
                'qty': qty, 'weight': wt,
                'length': ln, 'width': wd, 'height': ht,
            })

        cleaned.append({
            'type': stype,
            'cnpj': str(stop.get('cnpj', '') or ''),
            'company': str(stop.get('company', '') or ''),
            'cep': cep_digits[:5] + '-' + cep_digits[5:],
            'number': str(stop.get('number', '') or '').strip(),
            'street': str(stop.get('street', '') or ''),
            'complement': str(stop.get('complement', '') or ''),
            'neighborhood': str(stop.get('neighborhood', '') or ''),
            'city': str(stop.get('city', '') or ''),
            'state': str(stop.get('state', '') or '').upper()[:2],
            'date': str(stop.get('date', '') or '').strip(),
            'notes': str(stop.get('notes', '') or ''),
            'cargo_items': clean_items,
        })

    coletas  = [s for s in cleaned if s['type'] == 'coleta']
    entregas = [s for s in cleaned if s['type'] == 'entrega']
    if not coletas:
        return False, 'Adicione ao menos uma parada de coleta.', [], 0.0
    if not entregas:
        return False, 'Adicione ao menos uma parada de entrega.', [], 0.0
    if total_weight <= 0:
        return False, 'Peso total da carga deve ser maior que zero. Informe o peso em ao menos um item.', [], 0.0

    return True, None, cleaned, total_weight


def _build_freight_from_quote(q, fn, fc_id):
    """Build a Freight object from a Quote, handling multi-stop robustly.

    Falls back to single-stop fields if stops_json is missing/corrupt.
    Never raises — approval callers rely on this.
    """
    from models import Freight
    if q.is_multi_stop and q.stops_json:
        try:
            _ms = json.loads(q.stops_json)
            if not isinstance(_ms, list) or not _ms:
                raise ValueError('stops_json empty or not a list')
            _cols = [s for s in _ms if isinstance(s, dict) and s.get('type') == 'coleta']
            _ents = [s for s in _ms if isinstance(s, dict) and s.get('type') == 'entrega']
            _fc = _cols[0] if _cols else next((s for s in _ms if isinstance(s, dict)), None)
            _le = _ents[-1] if _ents else next((s for s in reversed(_ms) if isinstance(s, dict)), None)
            if not isinstance(_fc, dict) or not isinstance(_le, dict):
                raise ValueError('First/last stop is not a dict')
            o_str = f"{_fc.get('city','')}/{_fc.get('state','')}" if _fc.get('city') else q.origin_cep or "N/I"
            d_str = f"{_le.get('city','')}/{_le.get('state','')}" if _le.get('city') else q.destination_cep or "N/I"
            _descs = [item.get('desc', '') for s in _ms
                      for item in s.get('cargo_items', [])
                      if isinstance(item, dict) and item.get('desc')]
            p_str = ("; ".join(_descs[:3]) + (f" +{len(_descs)-3}" if len(_descs) > 3 else "")) if _descs else "Carga geral"
            return Freight(
                freight_number=fn, quote_id=q.id, client_id=q.client_id,
                created_by=fc_id, stops_json=q.stops_json,
                origin=o_str, destination=d_str, product=p_str,
                weight=q.load_weight or 0.0, agreed_price=q.sale_value,
                driver_cost=q.driver_cost,
                origin_city=_fc.get('city', ''), origin_state=_fc.get('state', ''),
                destination_city=_le.get('city', ''), destination_state=_le.get('state', ''),
                pickup_date=q.pickup_date, status='ofertado',
            )
        except Exception as _ms_err:
            logging.warning(f'Multi-stop freight build failed ({_ms_err}); falling back to single-stop fields')
    # Single-stop or fallback
    o_parts = [p for p in [q.origin_city, q.origin_state] if p]
    o_str = "/".join(o_parts) if o_parts else q.origin_cep or "N/I"
    d_parts = [p for p in [q.destination_city, q.destination_state] if p]
    d_str = "/".join(d_parts) if d_parts else q.destination_cep or "N/I"
    p_parts = [p for p in [q.load_type, q.vehicle_type] if p]
    p_str = " - ".join(p_parts) if p_parts else "Carga geral"
    return Freight(
        freight_number=fn, quote_id=q.id, client_id=q.client_id,
        created_by=fc_id,
        origin=o_str, destination=d_str, product=p_str,
        weight=q.load_weight or 0.0, agreed_price=q.sale_value,
        driver_cost=q.driver_cost,
        origin_city=q.origin_city, origin_state=q.origin_state,
        destination_city=q.destination_city, destination_state=q.destination_state,
        pickup_date=q.pickup_date, status='ofertado',
    )


@quotes_bp.route('/')
@login_required
def index():
    """Lista todas as cotações"""
    page = request.args.get('page', 1, type=int)
    per_page = 20
    search = request.args.get('search', '')
    status = request.args.get('status', '')
    client_id = request.args.get('client_id', '')

    query = Quote.query.options(joinedload(Quote.client))

    if current_user.role == 'cliente':
        query = query.filter_by(client_id=current_user.client_id)

    if status:
        query = query.filter_by(status=status)

    if client_id and current_user.role in ['admin', 'operador', 'vendedor']:
        query = query.filter_by(client_id=int(client_id))

    if search:
        query = query.filter(Quote.quote_number.contains(search))

    query = query.order_by(Quote.created_at.desc())

    quotes = query.paginate(page=page, per_page=per_page, error_out=False)

    clients = []
    if current_user.role in ['admin', 'operador', 'vendedor']:
        clients = Client.query.filter_by(active=True).order_by(Client.company_name).all()

    return render_template('quotes/index.html',
                           quotes=quotes, clients=clients,
                           search=search, status=status, selected_client=client_id)

@quotes_bp.route('/new')
@login_required
def new():
    """Formulário para nova cotação"""
    clients = []
    if current_user.role in ['admin', 'operador', 'vendedor']:
        clients = Client.query.filter_by(active=True).order_by(Client.company_name).all()
    elif current_user.role == 'cliente':
        if current_user.client_id:
            client = Client.query.get(current_user.client_id)
            if client:
                clients = [client]

    # CRM pre-fill — se vier de uma oportunidade
    crm_opp = None
    opportunity_id = request.args.get('opportunity_id', type=int)
    if opportunity_id:
        from models import Opportunity
        crm_opp = Opportunity.query.get(opportunity_id)

    # Pré-selecionar cliente do lead CRM
    prefill_client = None
    prefill_client_id = request.args.get('prefill_client_id', type=int)
    if prefill_client_id:
        prefill_client = Client.query.get(prefill_client_id)

    return render_template('quotes/form.html',
        clients=clients,
        today=datetime.now().strftime('%Y-%m-%d'),
        crm_opp=crm_opp,
        prefill_client=prefill_client,
    )

@quotes_bp.route('/new', methods=['POST'])
@login_required
def create():
    """Criar nova cotação"""
    try:
        # Log dos dados recebidos para debug
        logging.info(f"Dados do formulário recebidos: {dict(request.form)}")
        
        # Determinar cliente
        if current_user.role == 'cliente':
            client_id = current_user.client_id
            if not client_id:
                flash('Erro: Usuário cliente não está associado a nenhum cliente. Entre em contato com o administrador.', 'error')
                return redirect(url_for('quotes.new'))
            
            # Verificar se o cliente existe e está ativo
            client = Client.query.filter_by(id=client_id, active=True).first()
            if not client:
                flash('Erro: Cliente não encontrado ou inativo. Entre em contato com o administrador.', 'error')
                return redirect(url_for('quotes.new'))
        else:
            client_id = request.form.get('client_id')
            if not client_id or not client_id.strip():
                flash('Cliente é obrigatório.', 'error')
                return redirect(url_for('quotes.new'))
            
            try:
                client_id = int(client_id)
            except (ValueError, TypeError):
                flash('Cliente selecionado é inválido.', 'error')
                return redirect(url_for('quotes.new'))
            
            # Verificar se o cliente existe e está ativo
            client = Client.query.filter_by(id=client_id, active=True).first()
            if not client:
                flash('Cliente selecionado não encontrado ou inativo.', 'error')
                return redirect(url_for('quotes.new'))

        # Multi-stop detection — parse stops_data JSON blob sent by the form JS
        is_multi_stop = request.form.get('is_multi_stop') == '1'
        ms_stops = []
        ms_stops_json_str = None
        if is_multi_stop:
            try:
                raw_stops = json.loads(request.form.get('stops_data', '[]'))
            except Exception:
                flash('Dados de paradas inválidos (JSON mal formado). Tente novamente.', 'error')
                return redirect(url_for('quotes.new'))
            _ok, _err, ms_stops, _ms_wt = _validate_ms_stops(raw_stops)
            if not _ok:
                flash(_err, 'error')
                return redirect(url_for('quotes.new'))
            ms_stops_json_str = json.dumps(ms_stops, ensure_ascii=False)

        # Validar campos obrigatórios
        if is_multi_stop:
            required_fields = {
                'vehicle_type': 'Tipo de veículo',
                'load_type': 'Tipo de carga',
            }
        else:
            required_fields = {
                'origin_cep': 'CEP de origem',
                'origin_number': 'Número de origem',
                'destination_cep': 'CEP de destino',
                'destination_number': 'Número de destino',
                'pickup_date': 'Data de coleta',
                'vehicle_type': 'Tipo de veículo',
                'load_type': 'Tipo de carga',
                'load_weight': 'Peso da carga'
            }
        
        missing_fields = []
        for field, label in required_fields.items():
            value = request.form.get(field)
            if not value or (isinstance(value, str) and not value.strip()):
                missing_fields.append(label)
                logging.warning(f"Campo obrigatório ausente: {field} = '{value}'")
        
        if missing_fields:
            flash(f'Campos obrigatórios não preenchidos: {", ".join(missing_fields)}', 'error')
            return redirect(url_for('quotes.new'))

        # Gerar número da cotação (usando modelo das cotações existentes)
        quote_count = Quote.query.count() + 1
        quote_number = f"COT-{datetime.now().strftime('%Y%m%d')}-{quote_count:04d}"

        # ── Processar itens da carga ─────────────────────────────────────────
        import json as _json_mod
        def _parse_cargo_items(form):
            descs   = form.getlist('cargo_item_desc[]')
            qtys    = form.getlist('cargo_item_qty[]')
            lengths = form.getlist('cargo_item_length[]')
            widths  = form.getlist('cargo_item_width[]')
            heights = form.getlist('cargo_item_height[]')
            weights = form.getlist('cargo_item_weight[]')
            items = []
            total = 0.0
            for i in range(len(descs)):
                try:
                    qty = float(qtys[i]) if i < len(qtys) and qtys[i] else 1.0
                    wt  = float(weights[i].replace(',', '.')) if i < len(weights) and weights[i] else 0.0
                    ln  = float(lengths[i]) if i < len(lengths) and lengths[i] else None
                    wd  = float(widths[i])  if i < len(widths)  and widths[i]  else None
                    ht  = float(heights[i]) if i < len(heights) and heights[i] else None
                except (ValueError, TypeError):
                    qty, wt, ln, wd, ht = 1.0, 0.0, None, None, None
                items.append({'desc': descs[i].strip() if i < len(descs) else '',
                               'qty': qty, 'weight': wt,
                               'length': ln, 'width': wd, 'height': ht})
                total += qty * wt
            return items, total

        if is_multi_stop:
            # Multi-stop: soma pesos de todos os itens de todas as paradas
            # e agrega todos os itens para cargo_items_json (resumo global)
            ms_coletas  = [s for s in ms_stops if s.get('type') == 'coleta']
            ms_entregas = [s for s in ms_stops if s.get('type') == 'entrega']
            first_coleta = ms_coletas[0]
            last_entrega  = ms_entregas[-1]

            all_cargo = []
            computed_weight = 0.0
            for s in ms_stops:
                for item in s.get('cargo_items', []):
                    try:
                        qty = float(item.get('qty', 1))
                        wt  = float(item.get('weight', 0))
                        computed_weight += qty * wt
                        all_cargo.append(item)
                    except (TypeError, ValueError):
                        pass

            cargo_items_list = all_cargo
            cargo_items_str  = _json_mod.dumps(all_cargo, ensure_ascii=False) if all_cargo else None

            # Peso total
            load_weight_str = request.form.get('load_weight', '').strip()
            if computed_weight > 0:
                load_weight = computed_weight
            elif load_weight_str:
                try:
                    load_weight = float(load_weight_str.replace(',', '.'))
                except (ValueError, TypeError):
                    load_weight = 0.0
            else:
                load_weight = 0.0

            if load_weight <= 0:
                flash('Informe o peso em ao menos um item de carga nas paradas.', 'error')
                return redirect(url_for('quotes.new'))

            # Derivar data de coleta da primeira parada de coleta
            pickup_date_str = first_coleta.get('date', '')
            try:
                pickup_date = datetime.strptime(pickup_date_str, '%Y-%m-%d').date() if pickup_date_str else datetime.now().date()
            except ValueError:
                pickup_date = datetime.now().date()

            # Derivar campos de origem/destino das paradas
            origin_cep           = first_coleta.get('cep') or '00000-000'
            origin_street        = first_coleta.get('street', '')
            origin_number        = first_coleta.get('number') or 's/n'
            origin_complement    = first_coleta.get('complement', '')
            origin_neighborhood  = first_coleta.get('neighborhood', '')
            origin_city          = first_coleta.get('city', '')
            origin_state         = first_coleta.get('state', '')
            origin_cnpj          = first_coleta.get('cnpj', '')
            origin_company       = first_coleta.get('company', '')
            destination_cep          = last_entrega.get('cep') or '00000-000'
            destination_street       = last_entrega.get('street', '')
            destination_number       = last_entrega.get('number') or 's/n'
            destination_complement   = last_entrega.get('complement', '')
            destination_neighborhood = last_entrega.get('neighborhood', '')
            destination_city         = last_entrega.get('city', '')
            destination_state        = last_entrega.get('state', '')
            dest_cnpj                = last_entrega.get('cnpj', '')
            dest_company             = last_entrega.get('company', '')
        else:
            # Single-stop: fluxo original
            pickup_date = None
            if request.form.get('pickup_date'):
                try:
                    pickup_date = datetime.strptime(request.form.get('pickup_date'), '%Y-%m-%d').date()
                except ValueError:
                    flash('Data de coleta inválida.', 'error')
                    return redirect(url_for('quotes.new'))

            cargo_items_list, computed_weight = _parse_cargo_items(request.form)
            cargo_items_str = _json_mod.dumps(cargo_items_list, ensure_ascii=False)

            load_weight_str = request.form.get('load_weight', '').strip()
            if computed_weight > 0:
                load_weight = computed_weight
            elif load_weight_str:
                try:
                    load_weight = float(load_weight_str.replace(',', '.'))
                except (ValueError, TypeError):
                    load_weight = 0.0
            else:
                load_weight = 0.0

            if load_weight <= 0:
                flash('Peso da carga é obrigatório. Informe o peso em ao menos um item.', 'error')
                return redirect(url_for('quotes.new'))

            origin_cep           = request.form.get('origin_cep', '').strip()
            origin_street        = request.form.get('origin_street', '').strip()
            origin_number        = request.form.get('origin_number', '').strip()
            origin_complement    = request.form.get('origin_complement', '').strip()
            origin_neighborhood  = request.form.get('origin_neighborhood', '').strip()
            origin_city          = request.form.get('origin_city', '').strip()
            origin_state         = request.form.get('origin_state', '').strip()
            destination_cep          = request.form.get('destination_cep', '').strip()
            destination_street       = request.form.get('destination_street', '').strip()
            destination_number       = request.form.get('destination_number', '').strip()
            destination_complement   = request.form.get('destination_complement', '').strip()
            destination_neighborhood = request.form.get('destination_neighborhood', '').strip()
            destination_city         = request.form.get('destination_city', '').strip()
            destination_state        = request.form.get('destination_state', '').strip()
            origin_cnpj  = request.form.get('origin_cnpj', '').strip()
            origin_company = request.form.get('origin_company', '').strip()
            dest_cnpj    = request.form.get('destination_cnpj', '').strip()
            dest_company = request.form.get('destination_company', '').strip()

        # Processar valores numéricos opcionais
        def safe_float(value, default=None):
            if not value or not str(value).strip():
                return default
            try:
                return float(str(value).replace(',', '.'))
            except:
                return default

        # CNPJ do cliente solicitante (normalizado) — CNPJs iguais a este não geram lead
        import re as _re
        from models import Lead, Client as _Client
        _client_obj  = _Client.query.get(int(client_id))
        _client_cnpj = _re.sub(r'\D', '', _client_obj.cnpj or '') if _client_obj and _client_obj.cnpj else ''

        # Quando o cliente cria a cotação, leads e oportunidades CRM devem ser
        # atribuídos ao primeiro admin/operador ativo, não ao usuário cliente.
        if current_user.role == 'cliente':
            from models import User as _UserLead
            _lead_owner = _UserLead.query.filter(
                _UserLead.role.in_(['admin', 'operador', 'vendedor']),
                _UserLead.active == True
            ).order_by(_UserLead.id).first()
            _lead_owner_id = _lead_owner.id if _lead_owner else 1
        else:
            _lead_owner_id = current_user.id

        # ── Lote permanente de leads operacionais ─────────────────────────────
        from models import LeadBatch as _LeadBatch
        _OP_BATCH_ID = 'OPERACIONAL'
        _op_batch = _LeadBatch.query.filter_by(batch_id=_OP_BATCH_ID).first()
        if not _op_batch:
            _op_batch = _LeadBatch(
                batch_id    = _OP_BATCH_ID,
                name        = 'Leads Operacionais',
                source      = 'operacional',
                assigned_to = _lead_owner_id,
                total_leads = 0,
                created_by  = _lead_owner_id,
                notes       = 'Lote permanente — gerado automaticamente a partir de empresas de coleta/entrega nos fretes.',
            )
            db.session.add(_op_batch)

        def _auto_lead(cnpj, company, city, state, source_label):
            """Cria ou atualiza um lead operacional (estrela) a partir do CNPJ
            de origem/destino da cotação, se diferente do cliente solicitante."""
            if not cnpj or not company:
                return
            raw_cnpj = _re.sub(r'\D', '', cnpj)
            if len(raw_cnpj) != 14:
                return

            # Regra principal: mesmo CNPJ do cliente solicitante → não gera lead
            if _client_cnpj and raw_cnpj == _client_cnpj:
                logging.info(f'Lead auto ignorado ({source_label}): CNPJ igual ao cliente solicitante ({cnpj})')
                return

            # Já existe como Cliente cadastrado → não cria lead desnecessário
            existing_client = _Client.query.filter(
                db.func.regexp_replace(_Client.cnpj, r'\D', '', 'g') == raw_cnpj
            ).first()
            if existing_client:
                logging.info(f'Lead auto ignorado ({source_label}): CNPJ já é cliente cadastrado ({cnpj})')
                return

            # Contexto para notas/referência
            client_name  = _client_obj.company_name if _client_obj else 'cliente'
            freight_ref  = f'Cotação via {client_name} ({source_label})'

            # Já existe como Lead → atualiza contador e contexto, mantém estrela
            existing_lead = Lead.query.filter(
                db.func.regexp_replace(Lead.cnpj, r'\D', '', 'g') == raw_cnpj
            ).first()
            if existing_lead:
                existing_lead.operational_count  = (existing_lead.operational_count or 0) + 1
                existing_lead.last_freight_ref   = freight_ref
                existing_lead.is_hot             = True
                existing_lead.import_batch       = _OP_BATCH_ID
                existing_lead.updated_at         = datetime.utcnow()
                logging.info(f'Lead operacional atualizado: {existing_lead.company_name} (aparição #{existing_lead.operational_count})')
                return

            # Novo lead operacional com estrela
            lead = Lead(
                company_name       = company,
                cnpj               = cnpj,
                city               = city,
                state              = state,
                source             = 'operacional',
                status             = 'novo',
                is_hot             = True,
                operational_count  = 1,
                last_freight_ref   = freight_ref,
                import_batch       = _OP_BATCH_ID,
                notes              = f'Lead gerado automaticamente — {freight_ref}.',
                assigned_to        = _lead_owner_id,
            )
            db.session.add(lead)
            _op_batch.total_leads = (_op_batch.total_leads or 0) + 1
            logging.info(f'Lead operacional criado: {company} ({cnpj}) — {source_label}')

        # Gerar leads automáticos para CNPJs de origem/destino
        # Multi-stop: gerar para cada parada; single-stop: comportamento original
        if is_multi_stop:
            for ms_s in ms_stops:
                _auto_lead(ms_s.get('cnpj',''), ms_s.get('company',''),
                           ms_s.get('city',''), ms_s.get('state',''),
                           'coleta' if ms_s.get('type')=='coleta' else 'entrega')
        else:
            _auto_lead(origin_cnpj, origin_company, origin_city, origin_state, 'origem')
            _auto_lead(dest_cnpj, dest_company, destination_city, destination_state, 'destino')

        # Criar cotação
        quote = Quote(
            quote_number=quote_number,
            client_id=int(client_id),
            created_by=current_user.id,

            # Multi-stop
            is_multi_stop=is_multi_stop,
            stops_json=ms_stops_json_str,

            # CNPJ Origem / Destino
            origin_cnpj=origin_cnpj,
            origin_company=origin_company,
            destination_cnpj=dest_cnpj,
            destination_company=dest_company,

            # Origem
            origin_cep=origin_cep,
            origin_street=origin_street,
            origin_number=origin_number,
            origin_complement=origin_complement,
            origin_neighborhood=origin_neighborhood,
            origin_city=origin_city,
            origin_state=origin_state,
            pickup_date=pickup_date,
            urgent_pickup=bool(request.form.get('urgent_pickup')),

            # Destino
            destination_cep=destination_cep,
            destination_street=destination_street,
            destination_number=destination_number,
            destination_complement=destination_complement,
            destination_neighborhood=destination_neighborhood,
            destination_city=destination_city,
            destination_state=destination_state,
            delivery_deadline=safe_float(request.form.get('delivery_deadline')),
            vehicle_type=request.form.get('vehicle_type', '').strip(),

            # Carga
            load_type=request.form.get('load_type', '').strip(),
            load_weight=load_weight,
            load_volume=safe_float(request.form.get('load_volume')),
            load_height=safe_float(request.form.get('load_height')),
            load_length=safe_float(request.form.get('load_length')),
            load_width=safe_float(request.form.get('load_width')),
            invoice_value=safe_float(request.form.get('invoice_value')),
            cargo_items_json=cargo_items_str if cargo_items_list else None,

            # Preços
            driver_cost=safe_float(request.form.get('driver_cost'), 0.0),
            sale_value=safe_float(request.form.get('sale_value'), 0.0),

            # Informações adicionais
            additional_info=request.form.get('additional_info', '').strip() or None,
            valid_until=datetime.strptime(request.form.get('valid_until'), '%Y-%m-%d').date() if request.form.get('valid_until') and request.form.get('valid_until').strip() else (datetime.now().date() + timedelta(days=7)),
            status='pendente',

            # Endereços alternativos
            pickup_address_notes=request.form.get('pickup_address_notes', '').strip() or None,
            delivery_address_notes=request.form.get('delivery_address_notes', '').strip() or None,
        )

        # Vinculação ao CRM (opcional) — sincroniza valor e avança estágio da oportunidade
        try:
            opp_id = int(request.form.get('opportunity_id', ''))
            if opp_id:
                quote.opportunity_id = opp_id
                opp = Opportunity.query.get(opp_id)
                if opp:
                    if quote.sale_value:
                        opp.value = quote.sale_value
                    # Avança para "Cotação Enviada" se ainda em Prospecção
                    if opp.stage == 'prospeccao':
                        opp.stage       = 'proposta'
                        opp.probability = 50
                    opp.updated_at = datetime.now()
        except (ValueError, TypeError):
            pass

        db.session.add(quote)
        db.session.flush()  # Para obter o ID antes do commit

        # Auto-link CRM: toda cotação entra no pipeline, inclusive as do portal do cliente.
        # Quando o próprio cliente cria, atribuímos ao primeiro admin/operador disponível
        # para que a oportunidade apareça na fila do time comercial.
        if not quote.opportunity_id:
            try:
                if current_user.role == 'cliente':
                    from models import User as _User
                    _crm_owner = _User.query.filter(
                        _User.role.in_(['admin', 'operador', 'vendedor']),
                        _User.active == True
                    ).order_by(_User.id).first()
                    _crm_owner_id = _crm_owner.id if _crm_owner else 1
                else:
                    _crm_owner_id = current_user.id
                _auto_crm_opportunity(quote, _client_obj, _crm_owner_id)
            except Exception as _crm_e:
                logging.warning(f'CRM auto-link falhou: {_crm_e}')

        # Log de auditoria
        audit = AuditLog(
            user_id=current_user.id,
            action='create',
            table_name='quotes',
            record_id=quote.id,
            old_values=None,
            new_values=json.dumps({
                'quote_number': quote.quote_number,
                'client_id': quote.client_id,
                'status': quote.status
            })
        )
        db.session.add(audit)

        db.session.commit()

        # Enviar notificação para operadores via SocketIO
        try:
            from app import socketio
            from models import User
            
            logging.info(f"Enviando notificação de nova cotação: {quote.quote_number}")
            
            # Buscar operadores ativos
            operators = User.query.filter(
                User.role.in_(['admin', 'operador', 'vendedor']),
                User.active == True
            ).all()
            
            notification_data = {
                'quote_id': quote.id,
                'quote_number': quote.quote_number,
                'client_name': quote.client.company_name if quote.client else 'Cliente não encontrado',
                'created_at': quote.created_at.strftime('%d/%m/%Y %H:%M'),
                'message': f'Nova cotação {quote.quote_number} criada'
            }
            
            # Enviar para cada operador
            for operator in operators:
                room = f'user_{operator.id}'
                socketio.emit('new_quote_notification', notification_data, room=room)
                logging.info(f"Notificação enviada para {operator.username} na sala {room}")
            
            logging.info(f"Notificação de nova cotação enviada para {len(operators)} operadores")
            
        except Exception as e:
            logging.error(f"Erro ao enviar notificação de nova cotação: {e}")

        flash('Cotação criada com sucesso!', 'success')
        return redirect(url_for('quotes.view', id=quote.id))

    except Exception as e:
        db.session.rollback()
        import traceback
        error_details = traceback.format_exc()
        logging.error(f"Erro ao criar cotação: {e}")
        logging.error(f"Detalhes do erro: {error_details}")
        flash(f'Erro ao criar cotação: {str(e)}', 'error')
        return redirect(url_for('quotes.new'))

@quotes_bp.route('/<int:id>')
@quotes_bp.route('/<int:id>/view')
@login_required
def view(id):
    """Visualizar cotação específica"""
    quote = Quote.query.get_or_404(id)
    
    # Verificar permissão
    if current_user.role == 'cliente' and quote.client_id != current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))
    
    return render_template('quotes/view.html', quote=quote, datetime=datetime, timedelta=timedelta)

@quotes_bp.route('/<int:id>/set-price', methods=['POST'])
@login_required
def set_price(id):
    """Definir preço da cotação (apenas admin/operador)"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))

    quote = Quote.query.get_or_404(id)
    
    if quote.status != 'pendente':
        flash('Cotação já foi precificada.', 'error')
        return redirect(url_for('quotes.view', id=id))

    try:
        # Atualizar cotação
        quote.driver_cost = float(request.form.get('driver_cost', 0))
        quote.sale_value = float(request.form.get('sale_value'))
        
        # Tratar valid_until - se não fornecido, usar 7 dias a partir de hoje
        valid_until_str = request.form.get('valid_until')
        if valid_until_str and valid_until_str.strip():
            quote.valid_until = datetime.strptime(valid_until_str, '%Y-%m-%d').date()
        else:
            # Se não fornecido, definir para 7 dias a partir de hoje
            quote.valid_until = (datetime.now() + timedelta(days=7)).date()
        
        quote.status = 'cotada'
        quote.quoted_at = datetime.now()
        quote.quoted_by = current_user.id

        db.session.commit()

        # Log de auditoria
        audit = AuditLog(
            user_id=current_user.id,
            action='update',
            table_name='quotes',
            record_id=quote.id,
            old_values=json.dumps({'status': 'pendente'}),
            new_values=json.dumps({
                'status': 'cotada',
                'sale_value': float(quote.sale_value),
                'valid_until': quote.valid_until.strftime('%Y-%m-%d')
            })
        )
        db.session.add(audit)
        db.session.commit()

        # Enviar notificação para o cliente via SocketIO
        try:
            from app import socketio
            from models import User
            
            logging.info(f"🔔 Enviando notificação de cotação precificada: {quote.quote_number}")
            
            # Buscar usuários do cliente
            client_users = User.query.filter(
                User.client_id == quote.client_id,
                User.role == 'cliente',
                User.active == True
            ).all()
            
            if client_users:
                notification_data = {
                    'quote_id': quote.id,
                    'quote_number': quote.quote_number,
                    'sale_value': float(quote.sale_value),
                    'valid_until': quote.valid_until.strftime('%d/%m/%Y'),
                    'client_name': quote.client.company_name,
                    'message': f'Cotação {quote.quote_number} foi avaliada! Valor: R$ {quote.sale_value:,.2f}'
                }
                
                # Enviar para cada usuário do cliente
                for user in client_users:
                    room = f'user_{user.id}'
                    socketio.emit('quote_priced_notification', notification_data, room=room)
                    logging.info(f"Notificação de cotação precificada enviada para {user.username} na sala {room}")
                
                logging.info(f"✅ Notificação de cotação precificada enviada para {len(client_users)} usuários do cliente")
            else:
                logging.warning(f"⚠️ Nenhum usuário ativo encontrado para o cliente da cotação {quote.quote_number}")
                
        except Exception as e:
            logging.error(f"❌ Erro ao enviar notificação de cotação precificada: {e}")

        # Enviar email para o cliente
        try:
            from utils.email_service import send_quote_priced_email
            logging.info(f"📧 Tentando enviar email para cotação: {quote.quote_number}")
            send_quote_priced_email(quote)
            logging.info(f"✅ Email enviado com sucesso para cotação: {quote.quote_number}")
        except Exception as e:
            logging.error(f"❌ Erro ao enviar email de cotação avaliada: {e}")

        flash('Preços definidos! Cotação enviada para aprovação do cliente.', 'success')
        return redirect(url_for('quotes.view', id=id))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao definir preços: {e}")
        flash('Erro ao definir preços. Tente novamente.', 'error')
        return redirect(url_for('quotes.view', id=id))

@quotes_bp.route('/<int:id>/approve-client', methods=['POST'])
@login_required
def approve_client(id):
    """Aprovar cotação pelo cliente"""
    quote = Quote.query.get_or_404(id)
    
    # Verificar permissão
    if current_user.role != 'cliente' or quote.client_id != current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))
    
    if quote.status not in ['cotada', 'negociacao']:
        flash('Cotação não está disponível para aprovação.', 'error')
        return redirect(url_for('quotes.view', id=id))

    try:
        # Se há valor negociado e foi aceito, usar esse valor
        if request.form.get('accept_negotiated_value') and quote.operator_response_value:
            quote.sale_value = quote.operator_response_value

        old_status = quote.status
        quote.status = 'aprovada'
        quote.approved_at = datetime.now()
        quote.approved_by = current_user.id

        # Log de auditoria
        audit = AuditLog(
            user_id=current_user.id,
            action='update',
            table_name='quotes',
            record_id=quote.id,
            old_values=json.dumps({'status': old_status}),
            new_values=json.dumps({'status': 'aprovada'})
        )
        db.session.add(audit)

        # Criar frete — tudo na mesma transação para garantir consistência
        from models import Freight
        freight_count  = Freight.query.count() + 1
        freight_number = f"FRT-{datetime.now().strftime('%Y%m%d')}-{freight_count:04d}"

        freight = _build_freight_from_quote(quote, freight_number, current_user.id)
        db.session.add(freight)

        # Commit único: status + audit + frete
        db.session.commit()

        # Atualizar oportunidade CRM + fechar atividades de follow-up
        try:
            from models import CRMActivity
            if quote.opportunity_id:
                opp = Opportunity.query.get(quote.opportunity_id)
                if opp and opp.stage not in ('ganho', 'perdido'):
                    opp.stage = 'ganho'
                    opp.probability = 100
                    opp.updated_at = datetime.utcnow()
                # Fechar todas as atividades de follow-up pendentes
                CRMActivity.query.filter_by(
                    opportunity_id=quote.opportunity_id, is_done=False
                ).update({'is_done': True, 'completed_at': datetime.utcnow()})
                db.session.commit()
                logging.info(f"Oportunidade CRM #{quote.opportunity_id} → GANHO, atividades concluídas")
            else:
                _crm_mark_ganho(quote, current_user.id)
        except Exception as e:
            logging.warning(f"Não foi possível atualizar oportunidade CRM: {e}")

        # Notificar operadores
        try:
            from app import socketio
            from models import User
            operators = User.query.filter(
                User.role.in_(['admin', 'operador', 'vendedor']), User.active == True
            ).all()
            notification_data = {
                'quote_id': quote.id,
                'quote_number': quote.quote_number,
                'freight_id': freight.id,
                'freight_number': freight.freight_number,
                'client_name': quote.client.company_name,
                'message': f'Cotação {quote.quote_number} foi aprovada! Frete {freight.freight_number} criado.',
            }
            for operator in operators:
                socketio.emit('quote_approved_notification', notification_data, room=f'user_{operator.id}')
            logging.info(f"Notificação de aprovação enviada para {len(operators)} operadores")
        except Exception as e:
            logging.error(f"Erro ao enviar notificação de aprovação: {e}")

        flash(f'Cotação aprovada! Frete {freight.freight_number} criado com sucesso.', 'success')
        return redirect(url_for('freight.view', id=freight.id))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao aprovar cotação: {e}")
        flash('Erro ao aprovar cotação. Tente novamente.', 'error')
        return redirect(url_for('quotes.view', id=id))

@quotes_bp.route('/<int:id>/approve-manual', methods=['POST'])
@login_required
def approve_manual(id):
    """Aprovar cotação manualmente pelo operador/admin em nome do cliente"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))

    quote = Quote.query.get_or_404(id)

    if quote.status not in ['cotada', 'negociacao', 'pendente']:
        flash('Cotação não pode ser aprovada neste status.', 'error')
        return redirect(url_for('quotes.view', id=id))

    if not quote.sale_value or quote.sale_value <= 0:
        flash('Defina o valor de venda antes de aprovar.', 'error')
        return redirect(url_for('quotes.view', id=id))

    channel = request.form.get('confirmation_channel', 'Não informado')
    obs     = request.form.get('obs', '').strip()

    try:
        old_status        = quote.status
        quote.status      = 'aprovada'
        quote.approved_at = datetime.now()
        quote.approved_by = current_user.id
        if obs:
            note = f"[Aprovação manual — {channel}] {obs}"
            quote.additional_info = (quote.additional_info + '\n' + note) if quote.additional_info else note

        # Log de auditoria
        audit = AuditLog(
            user_id=current_user.id,
            action='update',
            table_name='quotes',
            record_id=quote.id,
            old_values=json.dumps({'status': old_status}),
            new_values=json.dumps({'status': 'aprovada', 'canal': channel})
        )
        db.session.add(audit)

        # Criar frete — tudo na mesma transação para garantir consistência
        from models import Freight
        freight_count  = Freight.query.count() + 1
        freight_number = f"FRT-{datetime.now().strftime('%Y%m%d')}-{freight_count:04d}"

        freight = _build_freight_from_quote(quote, freight_number, current_user.id)
        db.session.add(freight)

        # Commit único: status + audit + frete
        db.session.commit()

        # Atualizar oportunidade CRM + fechar atividades de follow-up
        try:
            from models import CRMActivity
            if quote.opportunity_id:
                opp = Opportunity.query.get(quote.opportunity_id)
                if opp and opp.stage not in ('ganho', 'perdido'):
                    opp.stage = 'ganho'
                    opp.probability = 100
                    opp.updated_at = datetime.utcnow()
                # Fechar todas as atividades de follow-up pendentes
                CRMActivity.query.filter_by(
                    opportunity_id=quote.opportunity_id, is_done=False
                ).update({'is_done': True, 'completed_at': datetime.utcnow()})
                db.session.commit()
                logging.info(f"Oportunidade CRM #{quote.opportunity_id} → GANHO, atividades concluídas (aprovação manual)")
            else:
                _crm_mark_ganho(quote, current_user.id)
        except Exception as e:
            logging.warning(f"Não foi possível atualizar oportunidade CRM: {e}")

        try:
            from app import socketio
            from models import User
            operators = User.query.filter(User.role.in_(['admin', 'operador', 'vendedor']), User.active == True).all()
            notification_data = {
                'quote_id': quote.id,
                'quote_number': quote.quote_number,
                'freight_id': freight.id,
                'freight_number': freight.freight_number,
                'client_name': quote.client.company_name,
                'message': f'Cotação {quote.quote_number} aprovada manualmente ({channel}). Frete {freight.freight_number} criado.',
            }
            for op in operators:
                socketio.emit('quote_approved_notification', notification_data, room=f'user_{op.id}')
        except Exception as e:
            logging.error(f"Erro ao enviar notificação de aprovação manual: {e}")

        flash(f'Cotação aprovada! Frete {freight.freight_number} criado com sucesso.', 'success')
        return redirect(url_for('freight.view', id=freight.id))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao aprovar cotação manualmente: {e}")
        flash('Erro ao aprovar cotação. Tente novamente.', 'error')
        return redirect(url_for('quotes.view', id=id))


def _crm_mark_perdido(quote, user_id, reason=''):
    """Marca oportunidade CRM como perdida e fecha atividades de follow-up."""
    try:
        from models import CRMActivity, Opportunity as _Opp
        if quote.opportunity_id:
            opp = _Opp.query.get(quote.opportunity_id)
            if opp and opp.stage not in ('perdido',):
                opp.stage = 'perdido'
                opp.probability = 0
                opp.updated_at = datetime.utcnow()
                if reason:
                    opp.notes = (opp.notes or '') + f'\n[Oportunidade Perdida] {reason}'
            CRMActivity.query.filter_by(
                opportunity_id=quote.opportunity_id, is_done=False
            ).update({'is_done': True, 'completed_at': datetime.utcnow()})
            db.session.flush()
            logging.info(f"CRM: oportunidade #{quote.opportunity_id} → PERDIDO (cotação {quote.quote_number})")
    except Exception as _e:
        logging.warning(f"CRM mark perdido falhou: {_e}")


@quotes_bp.route('/<int:id>/reject-operator', methods=['POST'])
@login_required
def reject_operator(id):
    """Marcar cotação como não aprovada (operador/admin) — oportunidade vai para Perdido no CRM."""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))

    quote = Quote.query.get_or_404(id)

    if quote.status in ['aprovada', 'rejeitada']:
        flash('Esta cotação já foi finalizada.', 'error')
        return redirect(url_for('quotes.view', id=id))

    old_status = quote.status
    reason = request.form.get('rejection_reason', '').strip()

    try:
        quote.status = 'rejeitada'
        quote.rejected_at = datetime.utcnow()
        quote.rejected_by = current_user.id
        quote.rejection_reason = reason or 'Não aprovado pelo operador'

        audit = AuditLog(
            user_id=current_user.id,
            action='update',
            table_name='quotes',
            record_id=quote.id,
            old_values=json.dumps({'status': old_status}),
            new_values=json.dumps({'status': 'rejeitada', 'rejection_reason': quote.rejection_reason})
        )
        db.session.add(audit)
        db.session.flush()

        _crm_mark_perdido(quote, current_user.id, reason or 'Não aprovado pelo operador')

        db.session.commit()
        flash('Cotação marcada como não aprovada. Oportunidade encerrada no CRM.', 'info')
        return redirect(url_for('quotes.view', id=id))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao rejeitar cotação (operador): {e}")
        flash('Erro ao processar. Tente novamente.', 'error')
        return redirect(url_for('quotes.view', id=id))


@quotes_bp.route('/<int:id>/reject-client', methods=['POST'])
@login_required
def reject_client(id):
    """Rejeitar cotação pelo cliente"""
    quote = Quote.query.get_or_404(id)
    
    # Verificar permissão
    if current_user.role != 'cliente' or quote.client_id != current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))
    
    if quote.status in ['aprovada', 'rejeitada']:
        flash('Esta cotação já foi finalizada.', 'error')
        return redirect(url_for('quotes.view', id=id))

    try:
        old_status = quote.status
        quote.status = 'rejeitada'
        quote.rejected_at = datetime.now()
        quote.rejected_by = current_user.id
        quote.rejection_reason = request.form.get('rejection_reason', 'Recusado pelo cliente')

        audit = AuditLog(
            user_id=current_user.id,
            action='update',
            table_name='quotes',
            record_id=quote.id,
            old_values=json.dumps({'status': old_status}),
            new_values=json.dumps({
                'status': 'rejeitada',
                'rejection_reason': quote.rejection_reason
            })
        )
        db.session.add(audit)
        db.session.flush()

        _crm_mark_perdido(quote, current_user.id, quote.rejection_reason)

        db.session.commit()
        flash('Cotação rejeitada. A equipe comercial foi notificada.', 'info')
        return redirect(url_for('quotes.view', id=id))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao rejeitar cotação: {e}")
        flash('Erro ao rejeitar cotação. Tente novamente.', 'error')
        return redirect(url_for('quotes.view', id=id))

@quotes_bp.route('/<int:id>/edit')
@login_required
def edit(id):
    """Editar cotação (apenas pendentes)"""
    quote = Quote.query.get_or_404(id)
    
    # Verificar permissão
    if current_user.role == 'cliente' and quote.client_id != current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))
    
    if quote.status != 'pendente':
        flash('Apenas cotações pendentes podem ser editadas.', 'error')
        return redirect(url_for('quotes.view', id=id))
    
    clients = []
    if current_user.role in ['admin', 'operador', 'vendedor']:
        clients = Client.query.filter_by(active=True).order_by(Client.company_name).all()
    elif current_user.role == 'cliente':
        # Para clientes, carregar apenas seu próprio cliente
        if current_user.client_id:
            client = Client.query.get(current_user.client_id)
            if client:
                clients = [client]
    
    return render_template('quotes/form.html', quote=quote, clients=clients, today=datetime.now().strftime('%Y-%m-%d'))

@quotes_bp.route('/<int:id>/update', methods=['POST'])
@login_required
def update(id):
    """Atualizar cotação"""
    quote = Quote.query.get_or_404(id)
    
    # Verificar permissão
    if current_user.role == 'cliente' and quote.client_id != current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))
    
    if quote.status != 'pendente':
        flash('Apenas cotações pendentes podem ser editadas.', 'error')
        return redirect(url_for('quotes.view', id=id))

    try:
        # Salvar valores antigos para auditoria
        old_values = {
            'origin_cep': quote.origin_cep,
            'destination_cep': quote.destination_cep,
            'load_weight': float(quote.load_weight),
            'load_type': quote.load_type
        }

        # Multi-stop detection
        import json as _jmod
        is_multi_stop_upd = request.form.get('is_multi_stop') == '1'
        ms_stops_upd = []
        ms_stops_json_upd = None
        if is_multi_stop_upd:
            try:
                _raw_upd = _jmod.loads(request.form.get('stops_data', '[]'))
            except Exception:
                flash('Dados de paradas inválidos (JSON mal formado). Tente novamente.', 'error')
                return redirect(url_for('quotes.edit', id=id))
            _ok_upd, _err_upd, ms_stops_upd, _ms_wt_upd = _validate_ms_stops(_raw_upd)
            if not _ok_upd:
                flash(_err_upd, 'error')
                return redirect(url_for('quotes.edit', id=id))
            ms_stops_json_upd = _jmod.dumps(ms_stops_upd, ensure_ascii=False)

        quote.is_multi_stop = is_multi_stop_upd
        quote.stops_json    = ms_stops_json_upd

        if is_multi_stop_upd:
            first_col = [s for s in ms_stops_upd if s.get('type')=='coleta'][0]
            last_ent  = [s for s in ms_stops_upd if s.get('type')=='entrega'][-1]
            quote.origin_cep           = first_col.get('cep') or '00000-000'
            quote.origin_street        = first_col.get('street','')
            quote.origin_number        = first_col.get('number') or 's/n'
            quote.origin_complement    = first_col.get('complement','')
            quote.origin_neighborhood  = first_col.get('neighborhood','')
            quote.origin_city          = first_col.get('city','')
            quote.origin_state         = first_col.get('state','')
            quote.origin_cnpj          = first_col.get('cnpj','')
            quote.origin_company       = first_col.get('company','')
            quote.destination_cep          = last_ent.get('cep') or '00000-000'
            quote.destination_street       = last_ent.get('street','')
            quote.destination_number       = last_ent.get('number') or 's/n'
            quote.destination_complement   = last_ent.get('complement','')
            quote.destination_neighborhood = last_ent.get('neighborhood','')
            quote.destination_city         = last_ent.get('city','')
            quote.destination_state        = last_ent.get('state','')
            quote.destination_cnpj         = last_ent.get('cnpj','')
            quote.destination_company      = last_ent.get('company','')
            pd_str = first_col.get('date','')
            try:
                quote.pickup_date = datetime.strptime(pd_str, '%Y-%m-%d').date() if pd_str else datetime.now().date()
            except ValueError:
                quote.pickup_date = datetime.now().date()
            # Sum weights
            all_cargo_upd = []
            total_wt_upd = 0.0
            for s in ms_stops_upd:
                for item in s.get('cargo_items', []):
                    try:
                        qty = float(item.get('qty',1)); wt = float(item.get('weight',0))
                        total_wt_upd += qty * wt
                        all_cargo_upd.append(item)
                    except (TypeError, ValueError):
                        pass
            quote.load_weight     = total_wt_upd if total_wt_upd > 0 else float(request.form.get('load_weight', 0) or 0)
            quote.cargo_items_json = _jmod.dumps(all_cargo_upd, ensure_ascii=False) if all_cargo_upd else None
        else:
            # Single-stop: fluxo original
            quote.origin_cep = request.form.get('origin_cep')
            quote.origin_street = request.form.get('origin_street')
            quote.origin_number = request.form.get('origin_number')
            quote.origin_complement = request.form.get('origin_complement')
            quote.origin_neighborhood = request.form.get('origin_neighborhood')
            quote.origin_city = request.form.get('origin_city')
            quote.origin_state = request.form.get('origin_state')
            quote.pickup_date = datetime.strptime(request.form.get('pickup_date'), '%Y-%m-%d').date() if request.form.get('pickup_date') else None
            quote.urgent_pickup = bool(request.form.get('urgent_pickup'))
            quote.destination_cep = request.form.get('destination_cep')
            quote.destination_street = request.form.get('destination_street')
            quote.destination_number = request.form.get('destination_number')
            quote.destination_complement = request.form.get('destination_complement')
            quote.destination_neighborhood = request.form.get('destination_neighborhood')
            quote.destination_city = request.form.get('destination_city')
            quote.destination_state = request.form.get('destination_state')
            quote.delivery_deadline = int(request.form.get('delivery_deadline')) if request.form.get('delivery_deadline') else None
            quote.origin_cnpj     = request.form.get('origin_cnpj', '').strip()
            quote.origin_company  = request.form.get('origin_company', '').strip()
            quote.destination_cnpj    = request.form.get('destination_cnpj', '').strip()
            quote.destination_company = request.form.get('destination_company', '').strip()
            def _parse_items_edit(form):
                descs=form.getlist('cargo_item_desc[]'); qtys=form.getlist('cargo_item_qty[]')
                lengths=form.getlist('cargo_item_length[]'); widths=form.getlist('cargo_item_width[]')
                heights=form.getlist('cargo_item_height[]'); weights=form.getlist('cargo_item_weight[]')
                items=[]; total=0.0
                for i in range(len(descs)):
                    try:
                        qty=float(qtys[i]) if i<len(qtys) and qtys[i] else 1.0
                        wt=float(weights[i].replace(',','.')) if i<len(weights) and weights[i] else 0.0
                        ln=float(lengths[i]) if i<len(lengths) and lengths[i] else None
                        wd=float(widths[i]) if i<len(widths) and widths[i] else None
                        ht=float(heights[i]) if i<len(heights) and heights[i] else None
                    except (ValueError,TypeError): qty,wt,ln,wd,ht=1.0,0.0,None,None,None
                    items.append({'desc':descs[i].strip() if i<len(descs) else '','qty':qty,'weight':wt,'length':ln,'width':wd,'height':ht})
                    total+=qty*wt
                return items,total
            _edit_items, _edit_weight = _parse_items_edit(request.form)
            if _edit_items:
                quote.cargo_items_json = _jmod.dumps(_edit_items, ensure_ascii=False)
                quote.load_weight = _edit_weight if _edit_weight > 0 else float(request.form.get('load_weight', 0))
            else:
                quote.cargo_items_json = None
                quote.load_weight = float(request.form.get('load_weight', 0))

        quote.vehicle_type = request.form.get('vehicle_type')
        quote.load_type    = request.form.get('load_type')
        quote.load_volume  = float(request.form.get('load_volume', 0)) if request.form.get('load_volume') else None
        quote.load_height  = float(request.form.get('load_height', 0)) if request.form.get('load_height') else None
        quote.load_length  = float(request.form.get('load_length', 0)) if request.form.get('load_length') else None
        quote.load_width   = float(request.form.get('load_width', 0)) if request.form.get('load_width') else None
        quote.invoice_value = float(request.form.get('invoice_value', 0)) if request.form.get('invoice_value') else None

        # Preços (apenas se não for cliente)
        if current_user.role != 'cliente':
            quote.driver_cost = float(request.form.get('driver_cost', 0))
            quote.sale_value  = float(request.form.get('sale_value', 0))

        # Informações adicionais
        quote.additional_info        = request.form.get('additional_info')
        quote.pickup_address_notes   = request.form.get('pickup_address_notes', '').strip() or None
        quote.delivery_address_notes = request.form.get('delivery_address_notes', '').strip() or None
        if not is_multi_stop_upd:
            quote.urgent_pickup = bool(request.form.get('urgent_pickup'))
        if request.form.get('valid_until') and request.form.get('valid_until').strip():
            quote.valid_until = datetime.strptime(request.form.get('valid_until'), '%Y-%m-%d').date()
        elif current_user.role == 'cliente' and not quote.valid_until:
            quote.valid_until = datetime.now().date() + timedelta(days=7)
        quote.updated_at = datetime.now()

        # Sincronizar valor da oportunidade vinculada com o novo sale_value da cotação
        if quote.opportunity_id and current_user.role != 'cliente':
            try:
                from models import Opportunity
                opp = Opportunity.query.get(quote.opportunity_id)
                if opp and quote.sale_value:
                    opp.value      = quote.sale_value
                    opp.updated_at = datetime.now()
                    # Avança para "Cotação Enviada" se ainda em Prospecção
                    if opp.stage == 'prospeccao':
                        opp.stage       = 'proposta'
                        opp.probability = 50
            except Exception:
                pass

        db.session.commit()

        # Log de auditoria
        audit = AuditLog(
            user_id=current_user.id,
            action='update',
            table_name='quotes',
            record_id=quote.id,
            old_values=json.dumps(old_values),
            new_values=json.dumps({
                'origin_cep': quote.origin_cep,
                'destination_cep': quote.destination_cep,
                'load_weight': float(quote.load_weight),
                'load_type': quote.load_type
            })
        )
        db.session.add(audit)
        db.session.commit()

        flash('Cotação atualizada com sucesso!', 'success')
        return redirect(url_for('quotes.view', id=id))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao atualizar cotação: {e}")
        flash('Erro ao atualizar cotação. Tente novamente.', 'error')
        return redirect(url_for('quotes.edit', id=id))

@quotes_bp.route('/<int:id>/duplicate', methods=['POST'])
@login_required
def duplicate(id):
    """Duplicar cotação como rascunho pendente"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))

    original = Quote.query.get_or_404(id)

    # Gerar número único para a cópia
    from sqlalchemy import func as _sql_func
    _max = db.session.query(_sql_func.max(Quote.id)).scalar() or 0
    new_number = f"COT-{datetime.now().strftime('%Y%m%d')}-{(_max + 1):04d}"

    try:
        new_quote = Quote(
            quote_number=new_number,
            client_id=original.client_id,
            created_by=current_user.id,

            origin_cnpj=original.origin_cnpj,
            origin_company=original.origin_company,
            origin_cep=original.origin_cep,
            origin_street=original.origin_street,
            origin_number=original.origin_number,
            origin_complement=original.origin_complement,
            origin_neighborhood=original.origin_neighborhood,
            origin_city=original.origin_city,
            origin_state=original.origin_state,
            pickup_date=original.pickup_date,
            urgent_pickup=original.urgent_pickup,
            pickup_address_notes=original.pickup_address_notes,

            destination_cnpj=original.destination_cnpj,
            destination_company=original.destination_company,
            destination_cep=original.destination_cep,
            destination_street=original.destination_street,
            destination_number=original.destination_number,
            destination_complement=original.destination_complement,
            destination_neighborhood=original.destination_neighborhood,
            destination_city=original.destination_city,
            destination_state=original.destination_state,
            delivery_deadline=original.delivery_deadline,
            delivery_address_notes=original.delivery_address_notes,

            vehicle_type=original.vehicle_type,
            load_type=original.load_type,
            load_weight=original.load_weight,
            load_volume=original.load_volume,
            load_height=original.load_height,
            load_length=original.load_length,
            load_width=original.load_width,
            invoice_value=original.invoice_value,
            cargo_items_json=original.cargo_items_json,

            driver_cost=original.driver_cost,
            sale_value=original.sale_value,
            additional_info=original.additional_info,
            valid_until=original.valid_until or (datetime.now().date() + timedelta(days=7)),
            status='pendente',

            # Multi-stop: preservar rota completa da original
            is_multi_stop=original.is_multi_stop,
            stops_json=original.stops_json,
        )
        db.session.add(new_quote)
        db.session.commit()
        flash(f'Cotação duplicada com sucesso! Novo número: {new_number}', 'success')
        return redirect(url_for('quotes.edit', id=new_quote.id))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao duplicar cotação: {e}")
        flash('Erro ao duplicar cotação. Tente novamente.', 'error')
        return redirect(url_for('quotes.view', id=id))


@quotes_bp.route('/<int:id>/delete', methods=['POST'])
@login_required
def delete(id):
    """Deletar cotação (admin e operador)"""
    if current_user.role not in ['admin', 'operador']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))

    quote = Quote.query.get_or_404(id)
    
    if quote.status in ['aprovada', 'aprovado_cliente', 'aprovado']:
        flash('Cotações aprovadas não podem ser apagadas.', 'error')
        return redirect(url_for('quotes.view', id=id))

    try:
        # Log de auditoria
        audit = AuditLog(
            user_id=current_user.id,
            action='delete',
            table_name='quotes',
            record_id=quote.id,
            old_values=json.dumps({
                'quote_number': quote.quote_number,
                'status': quote.status
            }),
            new_values=None
        )
        db.session.add(audit)

        db.session.delete(quote)
        db.session.commit()

        flash('Cotação deletada com sucesso.', 'success')
        return redirect(url_for('quotes.index'))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao deletar cotação: {e}")
        flash('Erro ao deletar cotação. Tente novamente.', 'error')
        return redirect(url_for('quotes.view', id=id))

@quotes_bp.route('/export')
@login_required
def export():
    """Exportar cotações para Excel"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))

    try:
        # Query base
        query = Quote.query

        # Filtros opcionais
        status = request.args.get('status')
        if status:
            query = query.filter_by(status=status)

        client_id = request.args.get('client_id')
        if client_id:
            query = query.filter_by(client_id=client_id)

        # Período
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        if start_date:
            start_date = datetime.strptime(start_date, '%Y-%m-%d')
            query = query.filter(Quote.created_at >= start_date)
        
        if end_date:
            end_date = datetime.strptime(end_date, '%Y-%m-%d')
            query = query.filter(Quote.created_at <= end_date)

        quotes = query.order_by(Quote.created_at.desc()).all()

        # Gerar arquivo Excel
        filename = export_quotes_to_excel(quotes)
        
        return send_file(filename, as_attachment=True, download_name=f'cotacoes_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx')

    except Exception as e:
        logging.error(f"Erro ao exportar cotações: {e}")
        flash('Erro ao exportar cotações. Tente novamente.', 'error')
        return redirect(url_for('quotes.index'))

@quotes_bp.route('/<int:id>/pdf')
@login_required
def download_pdf(id):
    """Download da cotação em PDF"""
    quote = Quote.query.get_or_404(id)
    
    # Verificar permissão
    if current_user.role == 'cliente' and quote.client_id != current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))

    try:
        # Gerar PDF com o usuário atual
        pdf_content = generate_quote_pdf(quote, current_user)
        
        # Criar resposta com o PDF
        response = make_response(pdf_content)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename=cotacao_{quote.quote_number}.pdf'
        
        return response

    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        logging.error(f"Erro ao gerar PDF da cotação: {e}")
        logging.error(f"Detalhes do erro: {error_details}")
        flash('Erro ao gerar PDF. Tente novamente.', 'error')
        return redirect(url_for('quotes.view', id=id))

@quotes_bp.route('/<int:id>/send-email', methods=['POST'])
@login_required
def send_email(id):
    """Enviar cotação por email"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))

    quote = Quote.query.get_or_404(id)

    try:
        send_quote_email(quote)
        flash('Email enviado com sucesso!', 'success')

    except Exception as e:
        logging.error(f"Erro ao enviar email da cotação: {e}")
        flash('Erro ao enviar email. Tente novamente.', 'error')

    return redirect(url_for('quotes.view', id=id))

@quotes_bp.route('/force-notification/<int:quote_id>')
@login_required
def force_notification(quote_id):
    """Forçar envio de notificação para cotação específica"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'error': 'Acesso negado'}), 403
    quote = Quote.query.get_or_404(quote_id)
    
    try:
        # Importar função de notificação
        from routes.notifications import notify_quote_priced
        
        logging.info(f"🚀 FORÇANDO notificação para cotação: {quote.quote_number}")
        
        # Enviar notificação forçada
        success = notify_quote_priced(quote)
        
        if success:
            flash(f'Notificação enviada com sucesso para cotação {quote.quote_number}!', 'success')
        else:
            flash(f'Falha ao enviar notificação para cotação {quote.quote_number}', 'error')
            
    except Exception as e:
        logging.error(f"Erro ao forçar notificação: {e}")
        flash(f'Erro ao enviar notificação: {str(e)}', 'error')
    
    return redirect(url_for('quotes.view', id=quote_id))

@quotes_bp.route('/test-notification/<int:quote_id>')
@login_required 
def test_notification(quote_id):
    """Testar notificação para cotação específica (apenas admin/operador)"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'}), 403
        
    quote = Quote.query.get_or_404(quote_id)
    
    try:
        from app import socketio
        from models import User
        
        logging.info(f"🧪 TESTE: Enviando notificação de cotação precificada: {quote.quote_number}")
        
        # Buscar usuários do cliente
        client_users = User.query.filter(
            User.client_id == quote.client_id,
            User.role == 'cliente',
            User.active == True
        ).all()
        
        if not client_users:
            return jsonify({
                'success': False,
                'error': f'Nenhum usuário cliente ativo encontrado para cliente ID {quote.client_id}'
            }), 400
        
        notification_data = {
            'quote_id': quote.id,
            'quote_number': quote.quote_number,
            'sale_value': float(quote.sale_value) if quote.sale_value else 1500.00,
            'valid_until': quote.valid_until.strftime('%d/%m/%Y') if quote.valid_until else '31/12/2025',
            'client_name': quote.client.company_name,
            'message': f'TESTE: Cotação {quote.quote_number} foi avaliada!'
        }
        
        # Enviar para cada usuário do cliente
        sent_count = 0
        for user in client_users:
            room = f'user_{user.id}'
            socketio.emit('quote_priced_notification', notification_data, room=room)
            logging.info(f"🧪 TESTE: Notificação enviada para {user.username} na sala {room}")
            sent_count += 1
        
        return jsonify({
            'success': True,
            'message': f'Notificação de teste enviada para {sent_count} usuários',
            'client_users': [{'id': u.id, 'username': u.username} for u in client_users],
            'notification_data': notification_data
        })
        
    except Exception as e:
        logging.error(f"❌ Erro no teste de notificação: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@quotes_bp.route('/api/monthly-stats')
@login_required
def api_monthly_stats():
    """API para estatísticas mensais de cotações"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'}), 403
    try:
        # Obter o mês selecionado do parâmetro
        month_param = request.args.get('month', '')
        
        if not month_param:
            # Se não informado, usar o mês atual
            current_date = datetime.now()
            year = current_date.year
            month = current_date.month
        else:
            # Parse do formato YYYY-MM
            try:
                year, month = map(int, month_param.split('-'))
            except (ValueError, TypeError):
                return jsonify({'success': False, 'error': 'Formato de mês inválido'}), 400

        # Calcular início e fim do mês
        start_date = datetime(year, month, 1)
        if month == 12:
            end_date = datetime(year + 1, 1, 1) - timedelta(seconds=1)
        else:
            end_date = datetime(year, month + 1, 1) - timedelta(seconds=1)

        # Query base
        query = Quote.query.filter(
            Quote.created_at >= start_date,
            Quote.created_at <= end_date
        )

        # Filtrar por cliente se necessário
        if current_user.role == 'cliente':
            query = query.filter_by(client_id=current_user.client_id)

        # Calcular estatísticas
        total_quotes = query.count()
        
        # Contar por status
        pending = query.filter_by(status='pendente').count()
        awaiting_client = query.filter(Quote.status.in_(['cotada', 'negociacao'])).count()
        approved = query.filter(Quote.status.in_(['aprovada', 'aprovado_cliente'])).count()
        
        # Calcular valor total (apenas cotações com valor)
        total_value = db.session.query(db.func.sum(Quote.sale_value)).filter(
            Quote.created_at >= start_date,
            Quote.created_at <= end_date,
            Quote.sale_value.isnot(None)
        )
        
        if current_user.role == 'cliente':
            total_value = total_value.filter_by(client_id=current_user.client_id)
            
        total_value = total_value.scalar() or 0

        # Nome do mês para exibição
        month_display = start_date.strftime('%B de %Y').title()
        
        # Traduzir mês para português
        months_pt = {
            'January': 'Janeiro',
            'February': 'Fevereiro', 
            'March': 'Março',
            'April': 'Abril',
            'May': 'Maio',
            'June': 'Junho',
            'July': 'Julho',
            'August': 'Agosto',
            'September': 'Setembro',
            'October': 'Outubro',
            'November': 'Novembro',
            'December': 'Dezembro'
        }
        
        for en, pt in months_pt.items():
            month_display = month_display.replace(en, pt)

        return jsonify({
            'success': True,
            'stats': {
                'total_quotes': total_quotes,
                'total_value': float(total_value),
                'pending': pending,
                'awaiting_client': awaiting_client,
                'approved': approved
            },
            'month_display': month_display
        })

    except Exception as e:
        logging.error(f"Erro ao carregar estatísticas mensais: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@quotes_bp.route('/api/dashboard-data')
@login_required
def api_dashboard_data():
    """API para dados do dashboard de cotações"""
    query = Quote.query
    
    if current_user.role == 'cliente':
        query = query.filter_by(client_id=current_user.client_id)

    # Contar por status
    status_counts = {}
    for status in ['pendente', 'cotada', 'aprovada', 'rejeitada']:
        status_counts[status] = query.filter_by(status=status).count()

    # Cotações recentes (últimos 30 dias)
    thirty_days_ago = datetime.now() - timedelta(days=30)
    recent_quotes = query.filter(Quote.created_at >= thirty_days_ago).count()

    # Valor total de cotações aprovadas este mês
    first_day_month = datetime.now().replace(day=1)
    approved_value = db.session.query(db.func.sum(Quote.sale_value)).filter(
        Quote.status == 'aprovada',
        Quote.approved_at >= first_day_month
    ).scalar() or 0

    if current_user.role == 'cliente':
        approved_value = db.session.query(db.func.sum(Quote.sale_value)).filter(
            Quote.status == 'aprovada',
            Quote.client_id == current_user.client_id,
            Quote.approved_at >= first_day_month
        ).scalar() or 0

    return jsonify({
        'status_counts': status_counts,
        'recent_quotes': recent_quotes,
        'approved_value': float(approved_value),
        'total_quotes': query.count()
    })

@quotes_bp.route('/test-notifications')
@login_required
def test_notifications():
    """Testar sistema de notificações"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'}), 403
    try:
        from app import socketio
        from models import User
        
        # Dados de teste
        test_data = {
            'quote_id': 999,
            'quote_number': 'TESTE-' + str(datetime.now().strftime('%Y%m%d%H%M%S')),
            'client_name': 'Teste Cliente LTDA',
            'counter_value': 1500.00,
            'original_value': 1800.00,
            'counter_message': 'Teste de contra-proposta',
            'negotiation_round': 1,
            'timestamp': datetime.now().isoformat(),
            'message': 'Teste de notificação de contra-proposta'
        }
        
        results = []
        
        if current_user.role == 'cliente':
            # Simular notificação de resposta do operador para cliente
            response_data = {
                'quote_id': 999,
                'quote_number': test_data['quote_number'],
                'new_value': 1650.00,
                'original_counter': 1500.00,
                'is_final': False,
                'operator_message': 'Nossa contra-proposta',
                'operator_name': 'Operador Teste',
                'negotiation_round': 2,
                'timestamp': datetime.now().isoformat(),
                'message': 'Teste de resposta de negociação'
            }
            
            # Enviar para o próprio cliente
            room = f'user_{current_user.id}'
            socketio.emit('quote_negotiation_response_notification', response_data, room=room)
            results.append(f'Notificação de resposta enviada para cliente na sala {room}')
            
        else:
            # Simular notificação de contra-proposta de cliente para operador
            # Enviar para operadores
            operators = User.query.filter(
                User.role.in_(['admin', 'operador', 'vendedor']),
                User.active == True
            ).all()
            
            for operator in operators:
                room = f'user_{operator.id}'
                socketio.emit('quote_counter_proposal_notification', test_data, room=room)
                results.append(f'Notificação de contra-proposta enviada para {operator.username} na sala {room}')
            
            # Também enviar para sala de operadores
            socketio.emit('quote_counter_proposal_notification', test_data, room='operators')
            results.append('Notificação enviada para sala de operadores')
        
        return jsonify({
            'success': True,
            'message': 'Notificações de teste enviadas',
            'results': results,
            'user': {
                'id': current_user.id,
                'username': current_user.username,
                'role': current_user.role,
                'client_id': current_user.client_id
            }
        })
        
    except Exception as e:
        logging.error(f"Erro no teste de notificações: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@quotes_bp.route('/<int:id>/counter-proposal', methods=['POST'])
@login_required
def counter_proposal(id):
    """Cliente faz contra-proposta de valor"""
    quote = Quote.query.get_or_404(id)
    
    # Verificar permissão
    if current_user.role != 'cliente' or quote.client_id != current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))
    
    if quote.status not in ['cotada', 'negociacao']:
        flash('Cotação não está disponível para contra-proposta.', 'error')
        return redirect(url_for('quotes.view', id=id))

    try:
        counter_value = float(request.form.get('counter_value'))
        counter_message = request.form.get('counter_message', '').strip()
        
        if counter_value <= 0:
            flash('Valor da contra-proposta deve ser maior que zero.', 'error')
            return redirect(url_for('quotes.view', id=id))
        
        # Atualizar cotação
        quote.client_counter_value = counter_value
        quote.client_counter_message = counter_message
        quote.client_counter_at = datetime.now()
        quote.status = 'negociacao'
        quote.negotiation_round += 1
        
        # Salvar no histórico
        from models import QuoteNegotiation
        negotiation = QuoteNegotiation(
            quote_id=quote.id,
            round_number=quote.negotiation_round,
            proposer_type='client',
            proposer_id=current_user.id,
            proposed_value=counter_value,
            message=counter_message
        )
        
        db.session.add(negotiation)
        db.session.commit()

        # Log de auditoria
        audit = AuditLog(
            user_id=current_user.id,
            action='update',
            table_name='quotes',
            record_id=quote.id,
            old_values=json.dumps({'status': 'cotada'}),
            new_values=json.dumps({
                'status': 'negociacao',
                'client_counter_value': float(counter_value),
                'negotiation_round': quote.negotiation_round
            })
        )
        db.session.add(audit)
        db.session.commit()

        # Notificar operadores sobre contra-proposta
        try:
            from app import socketio
            from models import User
            
            logging.info(f"📩 INICIANDO notificação de contra-proposta: {quote.quote_number}")
            logging.info(f"💰 Valor contra-proposta: R$ {counter_value:,.2f} (original: R$ {quote.sale_value:,.2f})")
            
            operators = User.query.filter(
                User.role.in_(['admin', 'operador', 'vendedor']),
                User.active == True
            ).all()
            
            logging.info(f"👥 Operadores encontrados: {len(operators)}")
            for op in operators:
                logging.info(f"   - {op.username} (ID: {op.id}, Role: {op.role})")
            
            if not operators:
                logging.warning(f"⚠️ NENHUM operador ativo encontrado para notificar contra-proposta da cotação {quote.quote_number}")
                flash('Contra-proposta enviada, mas nenhum operador ativo foi encontrado para notificar.', 'warning')
                return redirect(url_for('quotes.view', id=id))
            
            notification_data = {
                'quote_id': quote.id,
                'quote_number': quote.quote_number,
                'client_name': quote.client.company_name,
                'counter_value': float(counter_value),
                'original_value': float(quote.sale_value),
                'counter_message': counter_message,
                'negotiation_round': quote.negotiation_round,
                'timestamp': datetime.now().isoformat(),
                'message': f'🔄 Cliente {quote.client.company_name} fez contra-proposta de R$ {counter_value:,.2f} na cotação {quote.quote_number}'
            }
            
            logging.info(f"📦 Dados da notificação: {notification_data}")
            
            successful_sends = 0
            failed_sends = 0
            for operator in operators:
                try:
                    room = f'user_{operator.id}'
                    logging.info(f"📤 ENVIANDO para {operator.username} na sala {room}...")
                    
                    # Emitir para a sala específica do usuário
                    socketio.emit('quote_counter_proposal_notification', notification_data, room=room)
                    
                    # Também emitir para sala de operadores
                    socketio.emit('quote_counter_proposal_notification', notification_data, room='operators')
                    
                    logging.info(f"✅ SUCESSO: Notificação enviada para {operator.username}")
                    successful_sends += 1
                    
                except Exception as e:
                    logging.error(f"❌ FALHA ao enviar para {operator.username}: {e}")
                    failed_sends += 1
            
            logging.info(f"📊 RESULTADO: {successful_sends} sucessos / {failed_sends} falhas / {len(operators)} total")
            
            # Verificar se SocketIO está funcionando
            try:
                socketio.emit('test_connectivity', {'message': 'teste de conectividade', 'timestamp': datetime.now().isoformat()})
                logging.info("🧪 Teste de conectividade enviado")
            except Exception as e:
                logging.error(f"❌ Erro no teste de conectividade: {e}")
            
        except Exception as e:
            logging.error(f"❌ ERRO CRÍTICO ao enviar notificação de contra-proposta: {e}")
            import traceback
            logging.error(f"📋 Stack trace: {traceback.format_exc()}")

        flash(f'Contra-proposta de R$ {counter_value:,.2f} enviada com sucesso!', 'success')
        return redirect(url_for('quotes.view', id=id))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao enviar contra-proposta: {e}")
        flash('Erro ao enviar contra-proposta. Tente novamente.', 'error')
        return redirect(url_for('quotes.view', id=id))

@quotes_bp.route('/<int:id>/respond-negotiation', methods=['POST'])
@login_required
def respond_negotiation(id):
    """Operador responde à negociação"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('quotes.index'))

    quote = Quote.query.get_or_404(id)
    
    if quote.status != 'negociacao':
        flash('Cotação não está em negociação.', 'error')
        return redirect(url_for('quotes.view', id=id))

    try:
        action = request.form.get('action')  # accept, counter, final
        
        if action == 'accept':
            # Aceitar contra-proposta do cliente
            quote.sale_value = quote.client_counter_value
            quote.status = 'cotada'
            quote.operator_response_at = datetime.now()
            
            # Salvar no histórico
            from models import QuoteNegotiation
            negotiation = QuoteNegotiation(
                quote_id=quote.id,
                round_number=quote.negotiation_round,
                proposer_type='operator',
                proposer_id=current_user.id,
                proposed_value=quote.client_counter_value,
                message='Contra-proposta aceita pelo operador'
            )
            db.session.add(negotiation)
            
            flash('Contra-proposta aceita! Valor atualizado na cotação.', 'success')
            
        elif action == 'counter':
            # Fazer nova contra-proposta
            counter_value = float(request.form.get('operator_counter_value'))
            counter_message = request.form.get('operator_message', '').strip()
            
            if counter_value <= 0:
                flash('Valor deve ser maior que zero.', 'error')
                return redirect(url_for('quotes.view', id=id))
            
            quote.operator_response_value = counter_value
            quote.operator_response_message = counter_message
            quote.operator_response_at = datetime.now()
            quote.negotiation_round += 1
            # Status continua como 'negociacao'
            
            # Salvar no histórico
            from models import QuoteNegotiation
            negotiation = QuoteNegotiation(
                quote_id=quote.id,
                round_number=quote.negotiation_round,
                proposer_type='operator',
                proposer_id=current_user.id,
                proposed_value=counter_value,
                message=counter_message
            )
            db.session.add(negotiation)
            
            flash(f'Nova proposta de R$ {counter_value:,.2f} enviada ao cliente.', 'success')
            
        elif action == 'final':
            # Proposta final do operador
            final_value = float(request.form.get('final_value'))
            final_message = request.form.get('final_message', '').strip()
            
            if final_value <= 0:
                flash('Valor deve ser maior que zero.', 'error')
                return redirect(url_for('quotes.view', id=id))
            
            quote.sale_value = final_value
            quote.operator_response_value = final_value
            quote.operator_response_message = final_message + " (PROPOSTA FINAL)"
            quote.operator_response_at = datetime.now()
            quote.status = 'cotada'
            quote.negotiation_round += 1
            
            # Salvar no histórico
            from models import QuoteNegotiation
            negotiation = QuoteNegotiation(
                quote_id=quote.id,
                round_number=quote.negotiation_round,
                proposer_type='operator',
                proposer_id=current_user.id,
                proposed_value=final_value,
                message=final_message + " (PROPOSTA FINAL)"
            )
            db.session.add(negotiation)
            
            flash(f'Proposta final de R$ {final_value:,.2f} definida.', 'success')

        db.session.commit()

        # Notificar cliente sobre resposta do operador
        if action in ['counter', 'final']:
            try:
                from app import socketio
                from models import User
                
                logging.info(f"📤 Enviando notificação de resposta do operador: {quote.quote_number}")
                
                client_users = User.query.filter(
                    User.client_id == quote.client_id,
                    User.role == 'cliente',
                    User.active == True
                ).all()
                
                if not client_users:
                    logging.warning(f"Nenhum usuário cliente ativo encontrado para notificar resposta da cotação {quote.quote_number}")
                else:
                    notification_data = {
                        'quote_id': quote.id,
                        'quote_number': quote.quote_number,
                        'new_value': float(quote.operator_response_value),
                        'original_counter': float(quote.client_counter_value),
                        'is_final': action == 'final',
                        'operator_message': quote.operator_response_message,
                        'operator_name': current_user.username,
                        'negotiation_round': quote.negotiation_round,
                        'timestamp': datetime.now().isoformat(),
                        'message': f'🔄 {"Proposta FINAL" if action == "final" else "Nova proposta"} do operador para cotação {quote.quote_number}: R$ {quote.operator_response_value:,.2f}'
                    }
                    
                    successful_sends = 0
                    for user in client_users:
                        try:
                            room = f'user_{user.id}'
                            socketio.emit('quote_negotiation_response_notification', notification_data, room=room)
                            logging.info(f"📤 Notificação de resposta enviada para {user.username} na sala {room}")
                            successful_sends += 1
                        except Exception as e:
                            logging.error(f"Erro ao enviar notificação para cliente {user.username}: {e}")
                    
                    logging.info(f"📊 Notificação de resposta enviada para {successful_sends}/{len(client_users)} usuários do cliente")
                
            except Exception as e:
                logging.error(f"Erro ao enviar notificação de resposta: {e}")

        return redirect(url_for('quotes.view', id=id))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao responder negociação: {e}")
        flash('Erro ao processar resposta. Tente novamente.', 'error')
        return redirect(url_for('quotes.view', id=id))


# ── CRM Auto-link ─────────────────────────────────────────────────────────────

def _auto_crm_opportunity(quote, client, assigned_user_id):
    """
    Vincula toda cotação ao pipeline CRM — operador, admin ou portal do cliente.

    Regra unificada:
      • Cotação pendente / cotada / negociação  → stage "Cotação Enviada" (proposta)
      • Cotação aprovada                         → stage "Ganho"

    Para clientes com lead pré-existente: usa o lead encontrado.
    Para clientes diretos/frequentes (sem lead): cria Lead(status='convertido')
    representando o cliente no CRM — não é um lead especulativo, é o registro
    do cliente convertido que originou a cotação.
    """
    import re as _re

    if not client:
        return

    # 1. Procurar lead pré-existente vinculado a este cliente
    lead = Lead.query.filter_by(converted_client_id=client.id).first()

    if not lead and client.cnpj:
        digits = _re.sub(r'\D', '', client.cnpj)
        if len(digits) == 14:
            lead = Lead.query.filter(
                db.func.regexp_replace(Lead.cnpj, r'\D', '', 'g') == digits
            ).first()

    # 2. Se não há lead → cliente direto → criar Lead convertido
    if not lead:
        lead = Lead(
            company_name=client.company_name,
            cnpj=client.cnpj,
            contact_phone=client.phone,
            contact_email=client.email,
            city=client.city,
            state=client.state,
            source='cliente_direto',
            status='convertido',
            converted_client_id=client.id,
            assigned_to=assigned_user_id,
            notes='Lead criado automaticamente via cotação direta',
        )
        db.session.add(lead)
        db.session.flush()
        logging.info(f'CRM: lead convertido criado para cliente direto "{client.company_name}"')

    # 3. Determinar estágio com base no status atual da cotação
    stage = 'ganho' if quote.status == 'aprovada' else 'proposta'
    prob  = 100 if stage == 'ganho' else 50

    # 4. Construir título e rota
    origin = ''
    dest   = ''
    if quote.origin_city:
        origin = f"{quote.origin_city}, {quote.origin_state}" if quote.origin_state else quote.origin_city
    if quote.destination_city:
        dest = f"{quote.destination_city}, {quote.destination_state}" if quote.destination_state else quote.destination_city

    title = f"Cotação {quote.quote_number}"
    if origin and dest:
        title += f" — {origin} → {dest}"
    elif origin:
        title += f" — Origem: {origin}"

    opp = Opportunity(
        lead_id=lead.id,
        title=title,
        stage=stage,
        probability=prob,
        value=quote.sale_value or None,
        freight_origin=origin,
        freight_destination=dest,
        vehicle_type=quote.vehicle_type or '',
        assigned_to=assigned_user_id,
    )
    db.session.add(opp)
    db.session.flush()

    quote.opportunity_id = opp.id
    logging.info(
        f'CRM auto-link: cotação {quote.quote_number} → '
        f'oportunidade #{opp.id} [{stage}] (lead: {lead.company_name})'
    )

    # 5. Criar atividade de follow-up quando cotação aguarda aprovação
    if stage == 'proposta':
        from models import CRMActivity
        from datetime import timedelta
        follow_up = CRMActivity(
            lead_id        = lead.id,
            opportunity_id = opp.id,
            activity_type  = 'tarefa',
            title          = f"Follow-up — {quote.quote_number} aguarda aprovação do cliente",
            notes          = (
                f"Cotação {quote.quote_number} enviada para {lead.company_name}. "
                f"Verificar se cliente recebeu e tem interesse em aprovar."
            ),
            scheduled_at   = datetime.utcnow() + timedelta(days=2),
            is_done        = False,
            created_by     = assigned_user_id,
        )
        db.session.add(follow_up)
        logging.info(f'CRM: atividade de follow-up criada para cotação {quote.quote_number}')


def _crm_mark_ganho(quote, assigned_user_id):
    """
    Registra um negócio GANHO no CRM para cotações aprovadas de clientes diretos.

    Clientes frequentes não passam pelo funil comercial (sem lead), mas quando
    a cotação é aprovada o negócio está fechado. Esta função cria um Lead
    com status='convertido' + Opportunity em stage='ganho' para que o valor
    apareça corretamente no pipeline de receita.

    Só é chamada quando quote.opportunity_id é None (cotação sem vínculo CRM).
    """
    import re as _re

    if not quote or not quote.client_id:
        return

    client = Client.query.get(quote.client_id)
    if not client:
        return

    try:
        # Verificar se já existe lead para este cliente
        lead = Lead.query.filter_by(converted_client_id=client.id).first()
        if not lead and client.cnpj:
            digits = _re.sub(r'\D', '', client.cnpj)
            if len(digits) == 14:
                lead = Lead.query.filter(
                    db.func.regexp_replace(Lead.cnpj, r'\D', '', 'g') == digits
                ).first()

        if not lead:
            # Criar lead representando cliente convertido (negócio fechado)
            lead = Lead(
                company_name=client.company_name,
                cnpj=client.cnpj,
                contact_phone=client.phone,
                contact_email=client.email,
                city=client.city,
                state=client.state,
                source='cliente_direto',
                status='convertido',
                converted_client_id=client.id,
                assigned_to=assigned_user_id,
                notes='Lead criado automaticamente ao aprovar cotação de cliente direto',
            )
            db.session.add(lead)
            db.session.flush()
            logging.info(f'CRM: lead convertido criado para "{client.company_name}"')

        # Montar título e rota da oportunidade
        origin = ''
        dest   = ''
        if quote.origin_city:
            origin = f"{quote.origin_city}, {quote.origin_state}" if quote.origin_state else quote.origin_city
        if quote.destination_city:
            dest = f"{quote.destination_city}, {quote.destination_state}" if quote.destination_state else quote.destination_city

        title = f"Cotação {quote.quote_number}"
        if origin and dest:
            title += f" — {origin} → {dest}"
        elif origin:
            title += f" — Origem: {origin}"

        opp = Opportunity(
            lead_id=lead.id,
            title=title,
            stage='ganho',
            probability=100,
            value=quote.sale_value or None,
            freight_origin=origin,
            freight_destination=dest,
            vehicle_type=quote.vehicle_type or '',
            assigned_to=assigned_user_id,
        )
        db.session.add(opp)
        db.session.flush()

        quote.opportunity_id = opp.id
        db.session.commit()
        logging.info(
            f'CRM ganho: cotação {quote.quote_number} → oportunidade #{opp.id} '
            f'(cliente direto: {client.company_name})'
        )
    except Exception as _e:
        db.session.rollback()
        logging.warning(f'CRM mark_ganho falhou para cotação {quote.quote_number}: {_e}')
