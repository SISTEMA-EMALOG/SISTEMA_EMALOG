from datetime import date, datetime

from flask import Blueprint, jsonify, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app import db, socketio
from models import Client, Driver, DriverBid, EmaSession, Freight, WhatsAppMessage, User
from utils.contracting_service import KANBAN_STAGES, move_bid, stage_for_bid


contracting_bp = Blueprint('contracting', __name__, url_prefix='/contracting')

STAGE_META = [
    ('awaiting_response', 'Aguardando resposta', 'waiting'),
    ('conversation', 'Conversando', 'conversation'),
    ('interested', 'Interessados', 'interest'),
    ('validating', 'Em validação', 'validation'),
    ('contracted', 'Contratados', 'success'),
    ('closed', 'Encerrados', 'muted'),
]


def _staff_only():
    return current_user.role in ('admin', 'operador')


def _iso(value):
    return value.isoformat() if value else None


def _last_session(driver_id):
    return EmaSession.query.filter_by(driver_id=driver_id).order_by(
        EmaSession.updated_at.desc(), EmaSession.created_at.desc()
    ).first()


def _last_bid(driver_id):
    return DriverBid.query.filter_by(driver_id=driver_id).order_by(
        DriverBid.updated_at.desc(), DriverBid.created_at.desc()
    ).first()


def _active_bid(driver_id):
    return DriverBid.query.filter_by(driver_id=driver_id).filter(
        DriverBid.status.in_(['sent', 'responded', 'no_price', 'accepted'])
    ).order_by(DriverBid.updated_at.desc(), DriverBid.created_at.desc()).first()


def _forbidden():
    return jsonify({'error': 'Acesso negado'}), 403


def _card(bid):
    d, f = bid.driver, bid.freight
    return {
        'id': bid.id,
        'stage': stage_for_bid(bid),
        'driver': {
            'id': d.id, 'name': d.name, 'phone': d.phone,
            'truck_type': d.truck_type, 'plate': d.vehicle_plate,
            'city': d.city, 'state': d.state,
            'availability': d.availability_status,
            'validated': bool(d.validated),
        },
        'freight': {
            'id': f.id, 'number': f.freight_number,
            'origin': f.origin, 'destination': f.destination,
            'pickup_date': f.pickup_date.isoformat() if f.pickup_date else None,
            'product': f.product, 'weight': f.weight,
            'driver_cost': f.driver_cost,
            'client_name': f.client.company_name if f.client else '—',
            'stops': f.stops,
        },
        'driver_price': float(bid.driver_price) if bid.driver_price is not None else None,
        'last_message': bid.driver_message,
        'sent_at': bid.sent_at.isoformat() if bid.sent_at else None,
        'responded_at': bid.responded_at.isoformat() if bid.responded_at else None,
        'internal_notes': bid.internal_notes or '',
        'stage_changed_at': bid.stage_changed_at.isoformat() if bid.stage_changed_at else None,
    }


@contracting_bp.route('/')
@login_required
def index():
    if not _staff_only():
        return _forbidden()
    all_bids = DriverBid.query.all()
    stats = {
        'total': len({b.freight_id for b in all_bids if stage_for_bid(b) != 'closed'}),
        'pending': sum(stage_for_bid(b) == 'awaiting_response' for b in all_bids),
        'accepted': sum(stage_for_bid(b) == 'contracted' for b in all_bids),
        'today': sum(bool(b.stage_changed_at and b.stage_changed_at.date() == date.today()) for b in all_bids),
    }
    clients = Client.query.filter_by(active=True).order_by(Client.company_name).all()
    truck_types = [r[0] for r in db.session.query(Driver.truck_type).filter(
        Driver.truck_type.isnot(None), Driver.truck_type != ''
    ).distinct().order_by(Driver.truck_type).all()]
    return render_template('contracting/index.html', stats=stats, clients=clients, truck_types=truck_types)


@contracting_bp.route('/api/board')
@login_required
def board():
    if not _staff_only():
        return _forbidden()
    query = DriverBid.query.options(
        joinedload(DriverBid.driver),
        joinedload(DriverBid.freight).joinedload(Freight.client),
    )
    search = request.args.get('search', '').strip()
    client_id = request.args.get('client_id', type=int)
    truck_type = request.args.get('truck_type', '').strip()
    if search:
        like = f'%{search}%'
        query = query.join(DriverBid.driver).join(DriverBid.freight).filter(or_(
            Driver.name.ilike(like), Driver.phone.ilike(like),
            Driver.vehicle_plate.ilike(like), Driver.city.ilike(like),
            Freight.freight_number.ilike(like), Freight.origin.ilike(like),
            Freight.destination.ilike(like),
        ))
    if client_id:
        query = query.filter(DriverBid.freight.has(Freight.client_id == client_id))
    if truck_type:
        query = query.filter(DriverBid.driver.has(Driver.truck_type == truck_type))
    bids = query.order_by(DriverBid.stage_changed_at.desc(), DriverBid.created_at.desc()).all()
    grouped = {stage: [] for stage in KANBAN_STAGES}
    for bid in bids:
        grouped[stage_for_bid(bid)].append(_card(bid))
    return jsonify({'columns': [
        {'id': stage, 'label': label, 'tone': tone, 'cards': grouped[stage]}
        for stage, label, tone in STAGE_META
    ]})


@contracting_bp.route('/api/bids/<int:bid_id>')
@login_required
def detail(bid_id):
    if not _staff_only():
        return _forbidden()
    bid = DriverBid.query.options(
        joinedload(DriverBid.driver),
        joinedload(DriverBid.freight).joinedload(Freight.client),
    ).get_or_404(bid_id)
    payload = _card(bid)
    messages = [{
        'label': 'Mensagem da EMA' if item.get('role') == 'ema' else 'Mensagem do motorista',
        'action': item.get('role'), 'reason': item.get('text', ''),
        'timestamp': item.get('ts'),
    } for item in (bid.history or [])]
    payload['history'] = list(bid.kanban_history or []) + messages
    payload['closed_reason'] = bid.closed_reason
    return jsonify(payload)


@contracting_bp.route('/api/bids/<int:bid_id>/move', methods=['POST'])
@login_required
def move(bid_id):
    if not _staff_only():
        return _forbidden()
    bid = DriverBid.query.get_or_404(bid_id)
    data = request.get_json(silent=True) or {}
    try:
        declined = move_bid(
            bid, str(data.get('stage', '')).strip(),
            current_user.id, data.get('reason'), data.get('price'),
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'Não foi possível movimentar a contratação.'}), 500

    if bid.kanban_stage == 'contracted':
        from utils.evolution_api import send_text
        send_text(
            bid.driver.phone,
            f"Olá {bid.driver.name.split()[0]}! Sua contratação para o frete "
            f"{bid.freight.freight_number} foi confirmada. A equipe EMALOG enviará "
            "as orientações de coleta.",
        )
        db.session.add(WhatsAppMessage(
            driver_id=bid.driver.id, freight_id=bid.freight_id,
            phone_number=bid.driver.phone,
            message_content=f"Contratação confirmada para o frete {bid.freight.freight_number}.",
            direction='outbound', source='freight', status='enviado',
            created_by=current_user.id,
        ))
        for other in declined:
            declined_text = 'Obrigado pelo interesse. Outro motorista foi contratado para esta demanda.'
            send_text(other.driver.phone, declined_text)
            db.session.add(WhatsAppMessage(
                driver_id=other.driver.id, freight_id=other.freight_id,
                phone_number=other.driver.phone, message_content=declined_text,
                direction='outbound', source='freight', status='enviado',
                created_by=current_user.id,
            ))
        db.session.commit()
    socketio.emit('contracting_update', {'bid_id': bid.id, 'stage': bid.kanban_stage}, room='operators')
    return jsonify({'ok': True, 'card': _card(bid)})


@contracting_bp.route('/api/bids/<int:bid_id>/note', methods=['POST'])
@login_required
def note(bid_id):
    if not _staff_only():
        return _forbidden()
    bid = DriverBid.query.get_or_404(bid_id)
    note_text = str((request.get_json(silent=True) or {}).get('note', '')).strip()
    if len(note_text) > 5000:
        return jsonify({'error': 'A nota deve ter no máximo 5.000 caracteres.'}), 400
    bid.internal_notes = note_text
    db.session.commit()
    socketio.emit('contracting_update', {'bid_id': bid.id}, room='operators')
    return jsonify({'ok': True})


@contracting_bp.route('/api/conversations')
@login_required
def conversations():
    if not _staff_only():
        return _forbidden()
    search = request.args.get('search', '').strip()
    drivers = Driver.query.filter(or_(
        Driver.driver_bids.any(),
        Driver.ema_sessions.any(),
        Driver.whatsapp_messages.any(),
    ))
    if search:
        like = f'%{search}%'
        drivers = drivers.filter(or_(Driver.name.ilike(like), Driver.phone.ilike(like)))
    rows = []
    for driver in drivers.all():
        bid, session = _last_bid(driver.id), _last_session(driver.id)
        wa = WhatsAppMessage.query.filter_by(driver_id=driver.id).order_by(
            WhatsAppMessage.sent_at.desc(), WhatsAppMessage.id.desc()
        ).first()
        candidates = []
        if bid and bid.history:
            item = bid.history[-1]
            candidates.append((item.get('ts') or '', item.get('text') or '', 'freight'))
        if session and session.history:
            item = session.history[-1]
            candidates.append((item.get('ts') or '', item.get('text') or '', 'ema'))
        if wa:
            candidates.append((_iso(wa.sent_at) or '', wa.message_content, wa.source))
        if not candidates:
            continue
        last_at, last_message, source = max(candidates, key=lambda item: item[0])
        unread = WhatsAppMessage.query.filter_by(
            driver_id=driver.id, direction='inbound'
        ).filter(WhatsAppMessage.status != 'lido').count()
        rows.append({
            'driver_id': driver.id, 'name': driver.name, 'phone': driver.phone,
            'avatar_initial': (driver.name or '?')[0].upper(),
            'last_message': last_message, 'last_at': last_at,
            'unread_count': unread, 'mode': driver.whatsapp_mode or 'auto',
            'source': source,
            'freight_number': bid.freight.freight_number if bid and bid.freight else None,
            'stage': stage_for_bid(bid) if bid else None,
        })
    rows.sort(key=lambda item: item['last_at'] or '', reverse=True)
    return jsonify({'conversations': rows})


@contracting_bp.route('/api/conversations/<int:driver_id>')
@login_required
def conversation_detail(driver_id):
    if not _staff_only():
        return _forbidden()
    driver = Driver.query.filter_by(id=driver_id).with_for_update().first_or_404()
    bid, session = _last_bid(driver.id), _last_session(driver.id)
    messages = []
    for candidate, source in ((bid, 'freight'), (session, 'ema')):
        if not candidate:
            continue
        for index, item in enumerate(candidate.history or []):
            role = item.get('role', 'ema')
            messages.append({
                'id': f'{source}-{candidate.id}-{index}',
                'direction': 'in' if role == 'driver' else 'out',
                'role': role, 'text': item.get('text', ''),
                'timestamp': item.get('ts'), 'source': source, 'status': 'enviado',
            })
    wa_rows = WhatsAppMessage.query.filter_by(driver_id=driver.id).order_by(
        WhatsAppMessage.sent_at.asc(), WhatsAppMessage.id.asc()
    ).all()
    for msg in wa_rows:
        messages.append({
            'id': f'wa-{msg.id}', 'direction': 'in' if msg.direction == 'inbound' else 'out',
            'role': 'driver' if msg.direction == 'inbound' else ('operator' if msg.created_by else 'ema'),
            'text': msg.message_content, 'timestamp': _iso(msg.sent_at),
            'source': msg.source, 'status': msg.status,
        })
        if msg.direction == 'inbound':
            msg.status = 'lido'
    db.session.commit()
    messages.sort(key=lambda item: item['timestamp'] or '')
    deduped = []
    last_seen = {}
    for message in messages:
        key = (message['direction'], message['text'].strip())
        try:
            stamp = datetime.fromisoformat((message['timestamp'] or '').replace('Z', '+00:00')).timestamp()
        except (TypeError, ValueError):
            stamp = None
        previous = last_seen.get(key)
        if stamp is not None and previous is not None and abs(stamp - previous) <= 10:
            continue
        if stamp is not None:
            last_seen[key] = stamp
        deduped.append(message)
    messages = deduped
    assignee = User.query.get(driver.whatsapp_assigned_to) if driver.whatsapp_assigned_to else None
    return jsonify({
        'driver': {
            'id': driver.id, 'name': driver.name, 'phone': driver.phone,
            'truck_type': driver.truck_type, 'plate': driver.vehicle_plate,
            'city': driver.city, 'state': driver.state,
            'validated': bool(driver.validated),
        },
        'mode': driver.whatsapp_mode or 'auto',
        'assigned_to': assignee.username if assignee else None,
        'messages': messages,
        'context': {
            'bid': _card(bid) if bid else None,
            'freight': _card(bid)['freight'] if bid else None,
            'ema_session': {
                'id': session.id, 'status': session.status,
                'current_field': session.current_field
            } if session else None,
        },
    })


@contracting_bp.route('/api/conversations/<int:driver_id>/send', methods=['POST'])
@login_required
def conversation_send(driver_id):
    if not _staff_only():
        return _forbidden()
    driver = Driver.query.get_or_404(driver_id)
    if driver.whatsapp_mode != 'manual':
        return jsonify({'error': 'Assuma a conversa antes de enviar mensagens.'}), 409
    if driver.whatsapp_assigned_to != current_user.id:
        return jsonify({'error': 'Esta conversa está atribuída a outro operador.'}), 409
    text_value = str((request.get_json(silent=True) or {}).get('message', '')).strip()
    if not text_value or len(text_value) > 2000:
        return jsonify({'error': 'A mensagem deve ter entre 1 e 2.000 caracteres.'}), 400
    from utils.evolution_api import send_text
    ok = send_text(driver.phone, text_value)
    bid = _active_bid(driver.id)
    msg = WhatsAppMessage(
        driver_id=driver.id, freight_id=bid.freight_id if bid else None,
        phone_number=driver.phone, message_content=text_value,
        direction='outbound', source='operator', created_by=current_user.id,
        status='enviado' if ok else 'erro',
    )
    db.session.add(msg)
    if bid:
        history = list(bid.history or [])
        history.append({'role': 'operator', 'text': text_value, 'ts': datetime.utcnow().isoformat()})
        bid.history = history[-300:]
    db.session.commit()
    socketio.emit('contracting_conversation_update', {'driver_id': driver.id}, room='operators')
    return jsonify({'ok': ok, 'message_id': msg.id}), (200 if ok else 502)


@contracting_bp.route('/api/conversations/<int:driver_id>/mode', methods=['POST'])
@login_required
def conversation_mode(driver_id):
    if not _staff_only():
        return _forbidden()
    driver = Driver.query.get_or_404(driver_id)
    mode = str((request.get_json(silent=True) or {}).get('mode', '')).strip()
    if mode not in ('auto', 'manual'):
        return jsonify({'error': 'Modo inválido.'}), 400
    if mode == 'manual' and driver.whatsapp_mode == 'manual' and \
            driver.whatsapp_assigned_to not in (None, current_user.id):
        return jsonify({'error': 'Conversa já assumida por outro operador.'}), 409
    if mode == 'auto' and driver.whatsapp_mode == 'manual' and \
            driver.whatsapp_assigned_to not in (None, current_user.id) and current_user.role != 'admin':
        return jsonify({'error': 'Somente o responsável ou um administrador pode devolver a conversa.'}), 403
    driver.whatsapp_mode = mode
    driver.whatsapp_assigned_to = current_user.id if mode == 'manual' else None
    db.session.commit()
    socketio.emit('contracting_conversation_update', {'driver_id': driver.id}, room='operators')
    return jsonify({'ok': True, 'mode': mode})


@contracting_bp.route('/api/ema/whatsapp/qr')
@login_required
def ema_whatsapp_qr():
    if not _staff_only():
        return _forbidden()
    from utils.evolution_api import get_qr_code
    result = get_qr_code()
    return jsonify(result), (200 if result.get('ok') else 502)


@contracting_bp.route('/api/ema/whatsapp/status')
@login_required
def ema_whatsapp_status():
    if not _staff_only():
        return _forbidden()
    from utils.evolution_api import get_connection_status
    return jsonify(get_connection_status())


@contracting_bp.route('/api/ema/webhook/configure', methods=['POST'])
@login_required
def configure_ema_webhook():
    if not _staff_only():
        return _forbidden()
    from routes.ema_agent import _webhook_token
    from utils.evolution_api import set_webhook
    token = _webhook_token()
    webhook_url = url_for('ema.webhook', _external=True, token=token)
    result = set_webhook(webhook_url)
    return jsonify(result), (200 if result.get('ok') else 502)


@contracting_bp.route('/api/ema-sessions')
@login_required
def ema_sessions():
    if not _staff_only():
        return _forbidden()
    from utils.ema_agent import driver_completion_pct, FIELD_MAP
    sessions = EmaSession.query.options(joinedload(EmaSession.driver)).order_by(
        EmaSession.updated_at.desc(), EmaSession.created_at.desc()
    ).all()
    rows = []
    for session in sessions:
        driver = session.driver
        rows.append({
            'id': session.id, 'driver_id': driver.id,
            'driver_name': driver.name, 'phone': driver.phone,
            'status': session.status,
            'current_field': FIELD_MAP.get(session.current_field, {}).get('label', session.current_field),
            'revalidation': bool(session.revalidation),
            'updated_at': _iso(session.updated_at or session.created_at),
            'error_msg': session.error_msg or session.abandoned_reason,
            'progress': driver_completion_pct(driver),
        })
    stats = {
        'total': len(rows),
        'active': sum(r['status'] in ('active', 'awaiting_file', 'awaiting_confirmation') for r in rows),
        'completed': sum(r['status'] == 'completed' for r in rows),
        'error': sum(r['status'] in ('error', 'abandoned') for r in rows),
    }
    return jsonify({'stats': stats, 'sessions': rows})