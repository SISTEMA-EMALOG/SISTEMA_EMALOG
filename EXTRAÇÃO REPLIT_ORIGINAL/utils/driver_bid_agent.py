"""
Driver Bid Agent — manages price consultation conversations with drivers via WhatsApp.
Uses Groq to extract prices from natural language responses.
"""
import os
import re
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_groq = None


def _get_groq():
    global _groq
    if _groq is None:
        from groq import Groq
        _groq = Groq(api_key=os.environ.get('GROQ_API_KEY', ''))
    return _groq


def build_bid_message(freight, driver_name: str) -> str:
    """Build the price consultation WhatsApp message for a freight."""
    origin      = freight.origin or (f"{freight.origin_city}/{freight.origin_state}"
                                     if freight.origin_city else '—')
    destination = freight.destination or (f"{freight.destination_city}/{freight.destination_state}"
                                          if freight.destination_city else '—')
    product     = freight.product or 'Carga geral'
    weight      = f"{freight.weight:,.0f} kg" if freight.weight else '—'

    # Get vehicle type from linked quote if available
    vehicle_type = '—'
    if freight.quote:
        vehicle_type = freight.quote.vehicle_type or '—'

    # Get collection date
    collect_date = ''
    pickup = getattr(freight, 'pickup_date', None)
    if pickup:
        try:
            collect_date = pickup.strftime('%d/%m/%Y')
        except Exception:
            collect_date = str(pickup)

    msg = (
        f"Olá, {driver_name.split()[0]}! 👋\n\n"
        f"Sou da equipe *EMALOG* e temos uma oportunidade de frete para você:\n\n"
        f"📦 *Carga:* {product} — {weight}\n"
        f"📍 *Origem:* {origin}\n"
        f"🏁 *Destino:* {destination}\n"
        f"🚛 *Veículo:* {vehicle_type}\n"
    )
    stops = getattr(freight, 'stops', []) or []
    if len(stops) > 2:
        msg += "\n🗺️ *Roteiro completo:*\n"
        for index, stop in enumerate(stops, 1):
            kind = 'Coleta' if stop.get('type') == 'pickup' else 'Entrega'
            place = stop.get('address') or ', '.join(filter(None, [
                stop.get('city'), stop.get('state')
            ])) or 'Local não informado'
            msg += f"{index}. {kind}: {place}\n"
    if collect_date:
        msg += f"📅 *Coleta:* {collect_date}\n"
    msg += (
        f"\nQual seria o seu *valor* para fazer esse frete?\n\n"
        f"Responda diretamente aqui com seu preço. 😊\n\n"
        f"_EMALOG — Logística_"
    )
    return msg


def process_bid_response(bid, text: str, apply: bool = True) -> dict:
    """
    Use Groq to extract price from driver's message.
    Returns {'price': float|None, 'refused': bool, 'reply': str}
    Updates bid in-place but does NOT commit.
    """
    system_prompt = (
        "Você é um assistente de logística da EMALOG que analisa respostas de motoristas "
        "em consultas de preço de frete via WhatsApp.\n\n"
        "RESPONDA SEMPRE com JSON válido no formato exato:\n"
        "{\n"
        '  "price": null_ou_numero_float,\n'
        '  "refused": false_ou_true,\n'
        '  "reply": "mensagem curta e amigável em português para enviar ao motorista"\n'
        "}\n\n"
        "Regras:\n"
        "- Extraia o valor que o motorista quer receber (ex: '1200 reais' → 1200.0)\n"
        "- Se o motorista disse preço em qualquer formato, coloque em 'price'\n"
        "- Se o motorista recusou, indisponível, não pode fazer: 'refused': true, 'price': null\n"
        "- Se a mensagem não tem preço claro, 'price': null e pergunte novamente\n"
        "- Se recebeu um preço: confirme o valor e diga que vai repassar à equipe\n"
        "- Se recusou: agradeça e diga que entrará em contato futuramente\n"
        "- Mensagens curtas, amigáveis, assine como 'EMALOG'\n"
        "- Não invente preços. Se tiver dúvida, price: null"
    )

    history = bid.history or []
    # Build history text for context
    hist_lines = []
    for h in history[-6:]:
        role = 'EMALOG' if h.get('role') == 'ema' else 'Motorista'
        hist_lines.append(f"{role}: {h.get('text','')}")
    hist_text = '\n'.join(hist_lines)

    user_content = f"Histórico:\n{hist_text}\n\nÚltima mensagem do motorista:\n{text}" if hist_text else text

    try:
        client = _get_groq()
        resp = client.chat.completions.create(
            model='qwen/qwen3.8-27b',
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_content},
            ],
            max_tokens=512,
            temperature=0.2,
        )
        raw = (resp.choices[0].message.content or '').strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.M)
        raw = re.sub(r'```\s*$', '', raw, flags=re.M).strip()
        parsed = json.loads(raw)
    except Exception as e:
        logger.error(f"[BidAgent] Groq error: {e}")
        return {
            'price': None, 'refused': False,
            'reply': "Recebi sua mensagem! Pode me confirmar o valor que deseja para esse frete? 🙏\n_EMALOG_"
        }

    price   = parsed.get('price')
    refused = bool(parsed.get('refused', False))
    reply   = parsed.get('reply', '')
    result = {'price': price, 'refused': refused, 'reply': reply}
    if apply:
        apply_bid_result(bid, text, result)
    return result


def apply_bid_result(bid, text: str, result: dict) -> None:
    """Apply a previously inferred driver response to a still-active bid."""
    price = result.get('price')
    refused = bool(result.get('refused', False))
    reply = result.get('reply', '')

    # Update bid record
    now = datetime.utcnow()
    bid.driver_message = text

    if price is not None:
        try:
            bid.driver_price = float(price)
            bid.status = 'responded'
            bid.kanban_stage = 'interested'
        except (ValueError, TypeError):
            bid.status = 'no_price'
            bid.kanban_stage = 'conversation'
    elif refused:
        bid.status = 'refused'
        bid.kanban_stage = 'closed'
        bid.closed_reason = 'Motorista recusou a demanda pelo WhatsApp.'
    else:
        bid.status = 'no_price'
        bid.kanban_stage = 'conversation'

    if bid.status in ('responded', 'refused', 'no_price') and not bid.responded_at:
        bid.responded_at = now
    bid.stage_changed_at = now
    stage_history = list(bid.kanban_history or [])
    stage_history.append({
        'stage': bid.kanban_stage,
        'label': 'Resposta recebida pelo WhatsApp',
        'reason': text[:300],
        'timestamp': now.isoformat(),
    })
    bid.kanban_history = stage_history[-100:]

    # Append to history
    history = list(bid.history or [])
    history.append({'role': 'driver', 'text': text, 'ts': now.isoformat()})
    if reply:
        history.append({'role': 'ema', 'text': reply, 'ts': now.isoformat()})
    bid.history = history
    bid.updated_at = now

def find_eligible_drivers(freight):
    """
    Return drivers eligible for a freight, ranked by relevance.
    Priority: 1) same route history  2) matching truck type + available  3) others available
    """
    from models import Driver, Freight as FreightModel, DriverBid

    # Already contacted drivers for this freight
    already_bid = {b.driver_id for b in freight.driver_bids}

    # Get required truck type from quote
    required_type = None
    if freight.quote:
        required_type = (freight.quote.vehicle_type or '').lower()

    all_drivers = Driver.query.filter_by(active=True, is_active=True).all()

    # Find drivers who've done the same route before
    route_veterans = set()
    if freight.origin_city and freight.destination_city:
        past = FreightModel.query.filter(
            FreightModel.origin_city == freight.origin_city,
            FreightModel.destination_city == freight.destination_city,
            FreightModel.assigned_driver_id.isnot(None)
        ).all()
        route_veterans = {f.assigned_driver_id for f in past}

    result = []
    for d in all_drivers:
        is_available = d.availability_status == 'disponivel'
        type_match   = (not required_type) or (d.truck_type or '').lower() == required_type
        is_veteran   = d.id in route_veterans
        has_bid      = d.id in already_bid

        result.append({
            'driver':       d,
            'type_match':   type_match,
            'is_available': is_available,
            'is_veteran':   is_veteran,
            'has_bid':      has_bid,
            'priority':     (3 if is_veteran else 0) + (2 if type_match else 0) + (1 if is_available else 0)
        })

    result.sort(key=lambda x: -x['priority'])
    return result
