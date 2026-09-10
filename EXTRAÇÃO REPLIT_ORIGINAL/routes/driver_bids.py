import logging
from datetime import datetime

from flask import (Blueprint, jsonify, request, render_template,
                   redirect, url_for, flash)
from flask_login import login_required, current_user
from app import db
from models import Driver, DriverBid, WhatsAppMessage

bids_bp = Blueprint('bids', __name__, url_prefix='/bids')
logger  = logging.getLogger(__name__)


def _staff_only():
    return current_user.role in ('admin', 'operador')


# ── API: list bids for a freight ───────────────────────────────────────────

@bids_bp.route('/freight/<int:freight_id>')
@login_required
def list_bids(freight_id):
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403
    bids = (DriverBid.query
            .filter_by(freight_id=freight_id)
            .order_by(DriverBid.created_at.desc())
            .all())
    return jsonify([_bid_to_dict(b) for b in bids])


# ── API: eligible drivers for a freight ───────────────────────────────────

@bids_bp.route('/freight/<int:freight_id>/eligible-drivers')
@login_required
def eligible_drivers(freight_id):
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403
    from models import Freight
    from utils.driver_bid_agent import find_eligible_drivers
    freight = Freight.query.get_or_404(freight_id)
    rows    = find_eligible_drivers(freight)
    return jsonify([{
        'id':           r['driver'].id,
        'name':         r['driver'].name,
        'phone':        r['driver'].phone,
        'truck_type':   r['driver'].truck_type,
        'plate':        r['driver'].vehicle_plate or '—',
        'availability': r['driver'].availability_status,
        'is_available': r['is_available'],
        'type_match':   r['type_match'],
        'is_veteran':   r['is_veteran'],
        'has_bid':      r['has_bid'],
        'priority':     r['priority'],
    } for r in rows])


# ── Send bid requests to selected drivers ─────────────────────────────────

@bids_bp.route('/freight/<int:freight_id>/send', methods=['POST'])
@login_required
def send_bids(freight_id):
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403

    from models import Freight
    from utils.driver_bid_agent import build_bid_message
    from utils.evolution_api import send_text

    freight    = Freight.query.get_or_404(freight_id)
    driver_ids = (request.json or {}).get('driver_ids', [])
    if not driver_ids:
        return jsonify({'error': 'Nenhum motorista selecionado'}), 400

    sent   = 0
    errors = []
    for did in driver_ids:
        driver = Driver.query.get(did)
        if not driver:
            continue
        from models import EmaSession
        active_ema = EmaSession.query.filter_by(driver_id=did).filter(
            EmaSession.status.in_(['pending', 'active', 'awaiting_file', 'awaiting_confirmation'])
        ).first()
        if active_ema:
            errors.append(f"{driver.name}: finalize ou assuma a conversa de cadastro EMA antes da oferta")
            continue
        # Avoid duplicate active bids
        existing = DriverBid.query.filter_by(driver_id=did).filter(
            DriverBid.status.in_(['sent', 'responded', 'no_price'])
        ).first()
        if existing:
            errors.append(f"{driver.name}: já possui uma consulta ativa")
            continue

        msg = build_bid_message(freight, driver.name)
        ok  = send_text(driver.phone, msg)

        bid = DriverBid(
            freight_id = freight_id,
            driver_id  = did,
            status     = 'sent',
            kanban_stage = 'awaiting_response',
            stage_changed_at = datetime.utcnow(),
            history    = [{'role': 'ema', 'text': msg, 'ts': datetime.utcnow().isoformat()}],
            sent_at    = datetime.utcnow(),
        )
        db.session.add(bid)
        db.session.add(WhatsAppMessage(
            freight_id=freight.id, driver_id=driver.id,
            phone_number=driver.phone, message_content=msg,
            direction='outbound', source='freight',
            status='enviado' if ok else 'erro',
            created_by=current_user.id,
        ))
        if not ok:
            bid.status = 'failed'
            bid.kanban_stage = 'closed'
            bid.closed_reason = 'Falha ao enviar consulta pelo WhatsApp.'
            errors.append(f"{driver.name}: falha no envio pelo WhatsApp")
        else:
            sent += 1

    db.session.commit()
    from app import socketio
    socketio.emit('contracting_update', {'freight_id': freight.id}, room='operators')
    socketio.emit('contracting_conversation_update', {
        'driver_ids': [int(did) for did in driver_ids]
    }, room='operators')
    return jsonify({'sent': sent, 'errors': errors})


# ── Accept a bid → assign driver with negotiated price ────────────────────

@bids_bp.route('/<int:bid_id>/accept', methods=['POST'])
@login_required
def accept_bid(bid_id):
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403

    bid = DriverBid.query.get_or_404(bid_id)
    freight, driver = bid.freight, bid.driver
    data = request.get_json(silent=True) or {}
    try:
        from utils.contracting_service import contract_bid
        other_bids = contract_bid(bid, current_user.id, data.get('price'))
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        logger.exception('[Bids] Erro ao aceitar motorista')
        return jsonify({'error': f'Não foi possível contratar o motorista: {exc}'}), 500

    from app import socketio
    socketio.emit('contracting_update', {'bid_id': bid.id, 'stage': 'contracted'}, room='operators')

    from utils.evolution_api import send_text
    for ob in other_bids:
        send_text(ob.driver.phone,
                  f"Olá {ob.driver.name.split()[0]}! Obrigado pelo interesse.\n"
                  "Para este frete já escolhemos outro motorista. Em breve teremos novas oportunidades!\n_EMALOG_")

    # Notify winning driver
    from utils.evolution_api import send_text as _send
    _send(driver.phone,
          f"🎉 Parabéns, {driver.name.split()[0]}! Seu valor de "
          f"R$ {float(bid.driver_price):,.2f} foi aceito!\n\n"
          f"Frete #{freight.freight_number}: {freight.origin} → {freight.destination}\n"
          f"Aguarde contato da equipe EMALOG para detalhes.\n\n_EMALOG_")

    return jsonify({
        'ok': True,
        'driver_cost': float(bid.driver_price),
        'freight_status': freight.status
    })


def _create_payments(freight, driver):
    """Create 70%/30% payment records for an accepted freight."""
    try:
        from models import Payment
        cost = float(freight.driver_cost or 0)
        if cost <= 0:
            return
        existing = Payment.query.filter_by(freight_id=freight.id).count()
        if existing:
            return
        p70 = Payment(
            freight_id=freight.id, driver_id=driver.id,
            amount=round(cost * 0.70, 2), payment_type='carregamento',
            status='pendente', due_date=getattr(freight, 'pickup_date', None)
        )
        p30 = Payment(
            freight_id=freight.id, driver_id=driver.id,
            amount=round(cost * 0.30, 2), payment_type='entrega',
            status='pendente', due_date=getattr(freight, 'delivery_date', None)
        )
        db.session.add_all([p70, p30])
    except Exception as e:
        logger.warning(f"[Bids] Erro ao criar pagamentos: {e}")


# ── Decline a bid ──────────────────────────────────────────────────────────

@bids_bp.route('/<int:bid_id>/decline', methods=['POST'])
@login_required
def decline_bid(bid_id):
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403

    bid = DriverBid.query.get_or_404(bid_id)
    from utils.contracting_service import move_bid
    old_stage = bid.kanban_stage
    move_bid(bid, 'closed', current_user.id, 'Encerrado manualmente pela equipe.')
    bid.status = 'declined'
    db.session.commit()
    from app import socketio
    socketio.emit('contracting_update', {'bid_id': bid.id, 'stage': 'closed'}, room='operators')

    from utils.evolution_api import send_text
    send_text(bid.driver.phone,
              f"Olá {bid.driver.name.split()[0]}! Obrigado pelo interesse.\n"
              f"Para este frete escolhemos outro motorista. Em breve haverá novas oportunidades! 🚛\n_EMALOG_")
    return jsonify({'ok': True})


# ── Helper ─────────────────────────────────────────────────────────────────

def _bid_to_dict(b: DriverBid) -> dict:
    return {
        'id':           b.id,
        'driver_id':    b.driver_id,
        'driver_name':  b.driver.name if b.driver else '—',
        'driver_phone': b.driver.phone if b.driver else '—',
        'truck_type':   b.driver.truck_type if b.driver else '—',
        'status':       b.status,
        'driver_price': float(b.driver_price) if b.driver_price else None,
        'driver_message': b.driver_message,
        'sent_at':      b.sent_at.isoformat() if b.sent_at else None,
        'responded_at': b.responded_at.isoformat() if b.responded_at else None,
    }


# ── Find active bid by phone (used by webhook router) ─────────────────────

def find_active_bid_by_phone(phone: str) -> DriverBid | None:
    from utils.evolution_api import normalize_phone
    phone_n = normalize_phone(phone)
    active = DriverBid.query.filter(
        DriverBid.status.in_(['sent', 'no_price'])
    ).order_by(DriverBid.created_at.desc()).all()
    matches = [
        bid for bid in active
        if bid.driver and normalize_phone(bid.driver.phone) == phone_n
    ]
    if len(matches) > 1:
        logger.error("[Bids] Telefone possui múltiplas ofertas ativas: %s",
                     [bid.id for bid in matches])
        return None
    return matches[0] if matches else None
