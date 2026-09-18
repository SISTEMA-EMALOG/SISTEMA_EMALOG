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


# ── Resposta direta a uma oferta de preço fechado ──────────────────────────
#
# A oferta enviada pela tela de seleção de motoristas fixa o valor e pede
# "responda ACEITO ou RECUSO". Até 17/09/2026 ninguém lia essa resposta: ela
# caía no extrator de preço abaixo, que não acha número nenhum e devolve o
# card para "Conversando". Resolver aqui também torna a resposta imune a
# `GROQ_API_KEY` ausente ou fora do ar.

_RESPOSTA_ACEITE = re.compile(r'\b(aceito|aceita|aceitar|aceite)\b', re.IGNORECASE)
_RESPOSTA_RECUSA = re.compile(r'\b(recuso|recusa|recusar|recuse)\b', re.IGNORECASE)
_TEM_NUMERO = re.compile(r'\d')


def _brl(valor: float) -> str:
    """Formata em real brasileiro: 2500.0 -> 'R$ 2.500,00'."""
    return f'R$ {valor:,.2f}'.replace(',', '#').replace('.', ',').replace('#', '.')


def _valor_ofertado(bid):
    """Valor que a oferta prometeu ao motorista, ou None se não houver."""
    freight = getattr(bid, 'freight', None)
    if freight is None:
        return None
    quote = getattr(freight, 'quote', None)
    for valor in (freight.driver_cost,
                  quote.driver_cost if quote else None,
                  freight.agreed_price):
        if valor:
            try:
                return float(valor)
            except (TypeError, ValueError):
                continue
    return None


def _resposta_direta(bid, text: str) -> dict | None:
    """Interpreta um ACEITO/RECUSO limpo, sem passar pelo Groq.

    Devolve None quando a mensagem não é um dos dois — inclusive quando ela
    traz número ("aceito por 3000"), que é contraproposta e precisa do
    extrator de preço.
    """
    limpo = (text or '').strip()
    if not limpo or _TEM_NUMERO.search(limpo):
        return None

    aceite = bool(_RESPOSTA_ACEITE.search(limpo))
    recusa = bool(_RESPOSTA_RECUSA.search(limpo))
    if aceite == recusa:  # nenhuma das duas, ou as duas na mesma frase
        return None

    nome = (getattr(bid, 'driver', None).name.split()[0]
            if getattr(bid, 'driver', None) and bid.driver.name else 'motorista')

    if recusa:
        return {
            'price': None, 'refused': True,
            'reply': (f'Tudo bem, {nome}! Obrigado pelo retorno rápido. '
                      f'Assim que surgir outro frete no seu perfil eu te aviso. 🚚\n\n_EMALOG_'),
        }

    valor = _valor_ofertado(bid)
    if valor is None:
        # Aceitou, mas a oferta não tem valor fechado: vira conversa para a
        # equipe combinar o preço.
        return {
            'price': None, 'refused': False,
            'reply': (f'Que bom, {nome}! Só me confirma o valor que você quer '
                      f'para esse frete que eu repasso à equipe. 🙏\n\n_EMALOG_'),
        }

    return {
        'price': valor, 'refused': False,
        'reply': (f'Show, {nome}! Aceite registrado por {_brl(valor)}. '
                  f'Nossa equipe confirma a contratação e passa os detalhes da '
                  f'coleta em seguida. 🚚\n\n_EMALOG_'),
    }


def process_bid_response(bid, text: str, apply: bool = True) -> dict:
    """
    Interpreta a resposta do motorista.

    Um ACEITO ou RECUSO limpo é resolvido por `_resposta_direta`, sem rede.
    Qualquer outra coisa vai para o Groq, que extrai o preço da frase.
    Returns {'price': float|None, 'refused': bool, 'reply': str}
    Updates bid in-place but does NOT commit.
    """
    direta = _resposta_direta(bid, text)
    if direta is not None:
        logger.info(f"[BidAgent] Resposta direta na bid {bid.id}: "
                    f"{'recusa' if direta['refused'] else 'aceite'}")
        if apply:
            apply_bid_result(bid, text, direta)
        return direta

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

from matching_motorista.matching import find_eligible_drivers  # noqa: F401 — mantém compatibilidade de import para quem já importa daqui
