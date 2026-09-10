"""Transactional rules shared by the contracting Kanban and bid APIs."""
import math
from datetime import datetime

from app import db
from models import AuditLog, Driver, DriverBid, Freight, FreightStatusLog, Payment


KANBAN_STAGES = (
    'awaiting_response',
    'conversation',
    'interested',
    'validating',
    'contracted',
    'closed',
)


def stage_for_bid(bid):
    if bid.kanban_stage in KANBAN_STAGES:
        return bid.kanban_stage
    return {
        'sent': 'awaiting_response',
        'no_price': 'conversation',
        'responded': 'interested',
        'accepted': 'contracted',
        'declined': 'closed',
        'refused': 'closed',
    }.get(bid.status, 'awaiting_response')


def _append_stage_history(bid, old_stage, new_stage, user_id=None, reason=None):
    entries = list(bid.kanban_history or [])
    entries.append({
        'from': old_stage,
        'stage': new_stage,
        'label': f'{old_stage} → {new_stage}',
        'reason': reason or '',
        'user_id': user_id,
        'timestamp': datetime.utcnow().isoformat(),
    })
    bid.kanban_history = entries[-100:]
    bid.stage_changed_at = datetime.utcnow()


def _validate_price(value):
    try:
        price = float(str(value).replace(',', '.'))
    except (TypeError, ValueError):
        raise ValueError('Informe um valor válido para o motorista.')
    if not math.isfinite(price) or price <= 0:
        raise ValueError('O valor do motorista deve ser maior que zero.')
    return price


def _create_payment_schedule(freight, driver, user_id):
    today = datetime.now().date()
    pickup = freight.pickup_date or today
    delivery = freight.delivery_date or pickup
    value_70 = round(float(freight.driver_cost) * .70, 2)
    value_30 = round(float(freight.driver_cost) - value_70, 2)
    rows = [
        ('carregamento_70', Payment(
            freight_id=freight.id, driver_id=driver.id,
            payment_type='carregamento_70', amount=value_70,
            description=f'70% carregamento — Frete {freight.freight_number}',
            status='pendente', milestone='carregamento',
            due_date=pickup, payment_date=pickup, created_by=user_id,
        )),
        ('finalizacao_30', Payment(
            freight_id=freight.id, driver_id=driver.id,
            payment_type='finalizacao_30', amount=value_30,
            description=f'30% finalização — Frete {freight.freight_number}',
            status='pendente', milestone='finalizacao',
            due_date=delivery, payment_date=delivery, created_by=user_id,
        )),
    ]
    for payment_type, payment in rows:
        exists = Payment.query.filter_by(
            freight_id=freight.id, driver_id=driver.id,
            payment_type=payment_type,
        ).filter(Payment.status != 'cancelado').first()
        if not exists:
            db.session.add(payment)


def contract_bid(bid, user_id, price=None):
    """Assign one driver atomically and synchronize freight, driver and finance."""
    # Serialize every assignment for the same freight. A second operator waits
    # for the first transaction and then observes the already assigned driver.
    bid = DriverBid.query.filter_by(id=bid.id).with_for_update().one()
    freight = Freight.query.filter_by(id=bid.freight_id).with_for_update().one()
    driver = Driver.query.filter_by(id=bid.driver_id).with_for_update().one()
    if not freight or not driver:
        raise ValueError('A candidatura não possui frete ou motorista válido.')
    if freight.status in ('entregue', 'cancelado'):
        raise ValueError('Frete finalizado não pode receber motorista.')
    if freight.assigned_driver_id and freight.assigned_driver_id != driver.id:
        raise ValueError('Este frete já está atribuído a outro motorista.')
    if not driver.active or not driver.is_active:
        raise ValueError('O motorista está inativo.')
    if driver.availability_status == 'em_frete' and freight.assigned_driver_id != driver.id:
        raise ValueError('O motorista já está vinculado a outro frete.')

    agreed = _validate_price(
        price if price not in (None, '') else (bid.driver_price or freight.driver_cost)
    )
    old_freight_status = freight.status
    old_driver_id = freight.assigned_driver_id
    old_stage = stage_for_bid(bid)

    freight.assigned_driver_id = driver.id
    freight.driver_cost = agreed
    freight.status = 'aceito'
    driver.availability_status = 'em_frete'
    bid.driver_price = agreed
    bid.status = 'accepted'
    bid.kanban_stage = 'contracted'
    bid.accepted_at = datetime.utcnow()
    _append_stage_history(bid, old_stage, 'contracted', user_id)

    other_bids = DriverBid.query.filter(
        DriverBid.freight_id == freight.id,
        DriverBid.id != bid.id,
        DriverBid.status.in_(['sent', 'responded', 'no_price']),
    ).all()
    for other in other_bids:
        other_old = stage_for_bid(other)
        other.status = 'declined'
        other.kanban_stage = 'closed'
        other.closed_reason = 'Outro motorista foi contratado para a demanda.'
        _append_stage_history(other, other_old, 'closed', user_id, other.closed_reason)

    _create_payment_schedule(freight, driver, user_id)
    db.session.add(FreightStatusLog(
        freight_id=freight.id,
        old_status=old_freight_status,
        new_status='aceito',
        notes=f'Motorista {driver.name} contratado pela Central de Contratação. Custo: R$ {agreed:.2f}',
        changed_by=user_id,
    ))
    db.session.add(AuditLog(
        user_id=user_id, action='UPDATE', table_name='freights', record_id=freight.id,
        old_values=f'Status: {old_freight_status}; motorista: {old_driver_id or "não atribuído"}',
        new_values=f'Motorista: {driver.name} (ID {driver.id}); custo: R$ {agreed:.2f}; status: aceito',
    ))
    return other_bids


def withdraw_contracted_bid(bid, user_id, reason):
    bid = DriverBid.query.filter_by(id=bid.id).with_for_update().one()
    freight = Freight.query.filter_by(id=bid.freight_id).with_for_update().one()
    driver = Driver.query.filter_by(id=bid.driver_id).with_for_update().one()
    if freight.assigned_driver_id != driver.id:
        raise ValueError('Esta contratação não é a atribuição atual do frete.')
    if freight.status in ('entregue', 'cancelado'):
        raise ValueError('Não é possível reabrir um frete finalizado.')

    reason = (reason or 'Desistência registrada na Central de Contratação.').strip()[:300]
    old_status = freight.status
    for payment in Payment.query.filter_by(
        freight_id=freight.id, driver_id=driver.id, status='pendente'
    ).all():
        payment.status = 'cancelado'
    freight.assigned_driver_id = None
    freight.driver_cost = None
    freight.status = 'ofertado'
    driver.availability_status = 'disponivel'
    bid.status = 'declined'
    bid.kanban_stage = 'closed'
    bid.closed_reason = reason
    _append_stage_history(bid, 'contracted', 'closed', user_id, reason)
    db.session.add(FreightStatusLog(
        freight_id=freight.id, old_status=old_status, new_status='ofertado',
        notes=f'Desistência de {driver.name}: {reason}', changed_by=user_id,
    ))
    db.session.add(AuditLog(
        user_id=user_id, action='UPDATE', table_name='freights', record_id=freight.id,
        old_values=f'Motorista: {driver.name}; status: {old_status}',
        new_values=f'Motorista removido; status: ofertado; motivo: {reason}',
    ))


def move_bid(bid, new_stage, user_id, reason=None, price=None):
    if new_stage not in KANBAN_STAGES:
        raise ValueError('Etapa de contratação inválida.')
    old_stage = stage_for_bid(bid)
    if old_stage == new_stage:
        return []
    if old_stage == 'contracted':
        if new_stage == 'closed':
            withdraw_contracted_bid(bid, user_id, reason)
            return []
        raise ValueError('Uma contratação concluída só pode ser encerrada por desistência.')
    if new_stage == 'contracted':
        return contract_bid(bid, user_id, price)

    bid.kanban_stage = new_stage
    if new_stage == 'closed':
        bid.status = 'refused' if bid.status == 'refused' else 'declined'
        bid.closed_reason = (reason or 'Encerrado pela equipe de contratação.')[:300]
    elif new_stage == 'interested':
        bid.status = 'responded'
    elif new_stage == 'conversation':
        bid.status = 'no_price'
    elif new_stage == 'awaiting_response':
        bid.status = 'sent'
    _append_stage_history(bid, old_stage, new_stage, user_id, reason)
    return []