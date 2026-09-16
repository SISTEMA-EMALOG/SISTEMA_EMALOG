"""
Seleção de canal de saída da Central de Atendimento.

Hoje existem dois caminhos para uma mensagem sair:

  evolution  Evolution API, já paga e conectada ao WhatsApp da operação.
             Envia texto livre, sem limite de modelo. É o padrão.

  twilio     Twilio. Em conta trial a API aceita apenas `to`, `from`,
             `ContentSid` e `statusCallback`, ou seja, só modelo
             pré-aprovado, sem `Body`. Texto livre só depois do upgrade.

Por que não chamar evolution_api.send_text direto
-------------------------------------------------
Aquela função tem três comportamentos que não servem para atendimento:

1. Devolve apenas True ou False e descarta o id da mensagem. Sem o id não
   há idempotência nem atualização de status de entrega.
2. Normaliza o telefone com uma regra que decide o código de país por
   startswith('55'), o que erra todo número do DDD 55. A chave da conversa
   usa a regra por comprimento, de utils/phone.py.
3. Quando as variáveis de ambiente faltam, ela LOGA "simulando envio" e
   devolve True. O atendente veria a mensagem como enviada sem nada ter
   saído. Aqui isso vira erro explícito.
"""

import logging
import os

import requests

from atendimento_conversas.utils import twilio_client

log = logging.getLogger(__name__)

TIMEOUT = 20

CANAL_EVOLUTION = 'evolution'
CANAL_TWILIO = 'twilio'


class EnvioError(RuntimeError):
    """Falha ao enviar a mensagem pelo canal escolhido."""


# ── Evolution ────────────────────────────────────────────────────────────────

def _cfg_evolution():
    return {
        'url': os.environ.get('EVOLUTION_API_URL', '').rstrip('/'),
        'key': os.environ.get('EVOLUTION_API_KEY', ''),
        'instance': os.environ.get('EVOLUTION_INSTANCE', 'emalog'),
    }


def evolution_configurada():
    cfg = _cfg_evolution()
    return bool(cfg['url'] and cfg['key'])


# Ciclo de vida da mensagem na Evolution, traduzido para o vocabulário já
# usado em whatsapp_messages.status.
_STATUS_EVOLUTION = {
    'PENDING': 'enviando',
    'SERVER_ACK': 'enviado',
    'DELIVERY_ACK': 'entregue',
    'READ': 'lido',
    'PLAYED': 'lido',
    'ERROR': 'erro',
}


def _enviar_evolution(chave, texto):
    """
    Manda texto livre pela Evolution.

    `chave` é a chave canônica da conversa, só dígitos com código de país,
    que é exatamente o formato que a Evolution espera em `number`.
    """
    cfg = _cfg_evolution()
    if not cfg['url'] or not cfg['key']:
        raise EnvioError(
            'Evolution API não configurada. Defina EVOLUTION_API_URL e '
            'EVOLUTION_API_KEY no ambiente.'
        )

    url = f"{cfg['url']}/message/sendText/{cfg['instance']}"
    try:
        r = requests.post(
            url,
            json={'number': chave, 'text': texto},
            headers={'apikey': cfg['key'], 'Content-Type': 'application/json'},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise EnvioError(f'Falha de rede ao falar com a Evolution: {exc}') from exc

    if r.status_code not in (200, 201):
        # Não logar o conteúdo da mensagem, só o diagnóstico do provedor.
        raise EnvioError(
            f'Evolution recusou o envio (HTTP {r.status_code}): {r.text[:200]}'
        )

    try:
        corpo = r.json()
    except Exception as exc:
        raise EnvioError(f'Resposta da Evolution ilegível: {exc}') from exc

    externo = (corpo.get('key') or {}).get('id') or None
    bruto = corpo.get('status') or ''
    return {
        'provider': CANAL_EVOLUTION,
        'external_id': externo,
        'status': _STATUS_EVOLUTION.get(str(bruto).upper(), 'enviado'),
        'status_bruto': bruto,
    }


def estado_evolution():
    """Estado da conexão da instância, para o diagnóstico da tela."""
    cfg = _cfg_evolution()
    if not cfg['url'] or not cfg['key']:
        return {'configurada': False, 'estado': 'nao_configurada'}
    try:
        r = requests.get(
            f"{cfg['url']}/instance/connectionState/{cfg['instance']}",
            headers={'apikey': cfg['key']}, timeout=8,
        )
        estado = ((r.json() or {}).get('instance') or {}).get('state', 'desconhecido')
        return {'configurada': True, 'estado': estado, 'instancia': cfg['instance']}
    except Exception as exc:
        return {'configurada': True, 'estado': 'erro', 'detalhe': str(exc)}


# ── Twilio ───────────────────────────────────────────────────────────────────

def _enviar_twilio(chave, texto):
    resultado = twilio_client.send_whatsapp(chave, texto)
    return {
        'provider': CANAL_TWILIO,
        'external_id': resultado.get('sid'),
        'status': twilio_client.map_status(resultado.get('status')),
        'status_bruto': resultado.get('status'),
    }


# ── Seleção ──────────────────────────────────────────────────────────────────

def canal_configurado():
    """
    Qual canal será usado, ou None se nenhum estiver pronto.

    ATENDIMENTO_CANAL força a escolha: 'evolution' ou 'twilio'. Sem ela, a
    Evolution tem preferência, porque é a que envia texto livre hoje.
    """
    escolhido = os.environ.get('ATENDIMENTO_CANAL', '').strip().lower()

    if escolhido == CANAL_TWILIO:
        return CANAL_TWILIO if twilio_client.is_configured() else None
    if escolhido == CANAL_EVOLUTION:
        return CANAL_EVOLUTION if evolution_configurada() else None

    if evolution_configurada():
        return CANAL_EVOLUTION
    if twilio_client.is_configured():
        return CANAL_TWILIO
    return None


def enviar(chave, texto):
    """
    Envia uma mensagem pelo canal ativo.

    Devolve dict com provider, external_id, status e status_bruto.
    Levanta EnvioError se nenhum canal estiver pronto ou se o envio falhar.

    NÃO grava nada no banco: quem chama persiste a WhatsAppMessage, para que
    a gravação fique na transação de quem conhece a conversa.
    """
    if not (texto or '').strip():
        raise EnvioError('Mensagem vazia.')
    if not (chave or '').strip():
        raise EnvioError('Destinatário vazio.')

    canal = canal_configurado()
    if canal == CANAL_EVOLUTION:
        return _enviar_evolution(chave, texto)
    if canal == CANAL_TWILIO:
        return _enviar_twilio(chave, texto)

    raise EnvioError(
        'Nenhum canal de WhatsApp configurado. Configure a Evolution '
        '(EVOLUTION_API_URL e EVOLUTION_API_KEY) ou a Twilio (TWILIO_*).'
    )


def diagnostico():
    """Resumo dos canais, para o endpoint de status e para a tela."""
    return {
        'canal_ativo': canal_configurado(),
        'evolution': estado_evolution(),
        'twilio': {
            'configurada': twilio_client.is_configured(),
            'remetente': twilio_client.whatsapp_number() or None,
        },
    }
