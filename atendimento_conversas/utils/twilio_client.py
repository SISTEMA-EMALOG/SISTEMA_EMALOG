"""
Camada Twilio da Central de Atendimento: envio e validação de webhook.

Por que NÃO usa o SDK oficial da Twilio
---------------------------------------
Duas razões concretas deste projeto.

1. O registro de blueprints em infraestrutura_critica/app.py envolve cada
   módulo num `except Exception: print(...)`. Um ImportError de dependência
   faltando não derruba nada: o módulo simplesmente não registra, em
   silêncio, e a aba some sem explicação. Uma dependência a menos é um modo
   de falha a menos.

2. A API REST da Twilio para mandar mensagem é um POST com autenticação
   básica, e `requests` já é dependência do projeto. A validação de
   assinatura é um HMAC-SHA1 de dez linhas com a biblioteca padrão. O SDK
   não agregaria nada aqui.

Credenciais sempre por variável de ambiente, declaradas em
.ebextensions/session-secret.config. Nunca no código.
"""

import base64
import hashlib
import hmac
import logging
import os
from urllib.parse import urlsplit, urlunsplit

import requests

log = logging.getLogger(__name__)

API_BASE = 'https://api.twilio.com/2010-04-01'
TIMEOUT = 15


# ── Configuração ─────────────────────────────────────────────────────────────

def account_sid():
    return os.environ.get('TWILIO_ACCOUNT_SID', '').strip()


def auth_token():
    return os.environ.get('TWILIO_AUTH_TOKEN', '').strip()


def whatsapp_number():
    """Número remetente, no formato 'whatsapp:+55...'."""
    numero = os.environ.get('TWILIO_WHATSAPP_NUMBER', '').strip()
    if numero and not numero.startswith('whatsapp:'):
        numero = f'whatsapp:{numero}'
    return numero


def is_configured():
    """True quando dá para enviar. O placeholder do .ebextensions não conta."""
    sid = account_sid()
    return bool(
        sid
        and sid.startswith('AC')
        and auth_token()
        and whatsapp_number()
        and 'PREENCHER' not in sid
    )


# ── Validação da assinatura do webhook ───────────────────────────────────────

def compute_signature(url, params, token=None):
    """
    Reproduz a assinatura da Twilio.

    Algoritmo oficial: concatena a URL completa com cada par chave+valor do
    formulário, ordenado por chave, e tira HMAC-SHA1 com o auth token,
    codificado em base64.
    """
    token = token if token is not None else auth_token()
    base = url
    for chave in sorted(params.keys()):
        base += chave + str(params[chave])
    assinatura = hmac.new(
        token.encode('utf-8'),
        base.encode('utf-8'),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(assinatura).decode('utf-8')


def validate_signature(url, params, signature, token=None):
    """
    Confere o cabeçalho X-Twilio-Signature. Comparação em tempo constante.

    Sem isto, qualquer um que descubra a URL posta mensagem falsa no sistema.
    """
    if not signature:
        return False
    token = token if token is not None else auth_token()
    if not token:
        return False
    try:
        esperada = compute_signature(url, params, token)
    except Exception as exc:
        log.warning(f"⚠️ Twilio: falha ao calcular assinatura: {exc}")
        return False
    return hmac.compare_digest(esperada, signature)


def webhook_url(request):
    """
    A URL que a Twilio usou para assinar esta requisição.

    A app roda atrás do balanceador do Elastic Beanstalk. O ProxyFix está
    ligado em app.py com x_proto e x_host, então request.url normalmente já
    reflete o esquema e o host públicos. Ainda assim a reconstrução pode
    divergir, por host alternativo ou porta.

    TWILIO_WEBHOOK_URL fixa o endereço público. Dela são aproveitados apenas
    o ESQUEMA e o HOST: o caminho vem sempre da requisição real. Sem isso,
    fixar a URL de entrada faria o callback de status validar contra o
    caminho errado e recusar tudo com 403, porque os dois endpoints passam
    por aqui.
    """
    fixa = os.environ.get('TWILIO_WEBHOOK_URL', '').strip()
    if not fixa or 'PREENCHER' in fixa:
        return request.url

    try:
        partes = urlsplit(fixa)
        if not partes.scheme or not partes.netloc:
            return request.url
    except Exception:
        return request.url

    atual = urlsplit(request.url)
    return urlunsplit((
        partes.scheme, partes.netloc, atual.path, atual.query, '',
    ))


# ── Envio ────────────────────────────────────────────────────────────────────

class TwilioError(RuntimeError):
    """Falha ao falar com a Twilio."""


def to_whatsapp_address(numero):
    """Converte uma chave canônica de dígitos no endereço que a Twilio espera."""
    numero = str(numero or '').strip()
    if not numero:
        return ''
    if numero.startswith('whatsapp:'):
        return numero
    if not numero.startswith('+'):
        numero = '+' + numero.lstrip('+')
    return f'whatsapp:{numero}'


def send_whatsapp(to, body, status_callback=None):
    """
    Envia uma mensagem de WhatsApp pela Twilio.

    `to` aceita a chave canônica ('5511999998888') ou o endereço completo.
    Devolve dict com 'sid' e 'status'. Levanta TwilioError em falha.

    Esta função NÃO grava nada no banco. Quem chama é responsável por
    persistir a WhatsAppMessage, para que a gravação e o envio fiquem na
    mesma transação de quem conhece a conversa.
    """
    if not is_configured():
        raise TwilioError(
            'Twilio não configurada. Defina TWILIO_ACCOUNT_SID, '
            'TWILIO_AUTH_TOKEN e TWILIO_WHATSAPP_NUMBER no ambiente.'
        )

    destino = to_whatsapp_address(to)
    if not destino:
        raise TwilioError('Destinatário vazio.')

    if not (body or '').strip():
        raise TwilioError('Mensagem vazia.')

    url = f'{API_BASE}/Accounts/{account_sid()}/Messages.json'
    dados = {
        'From': whatsapp_number(),
        'To': destino,
        'Body': body,
    }
    if status_callback:
        dados['StatusCallback'] = status_callback

    try:
        resp = requests.post(
            url,
            data=dados,
            auth=(account_sid(), auth_token()),
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise TwilioError(f'Falha de rede ao falar com a Twilio: {exc}') from exc

    if resp.status_code >= 400:
        # Não logar o corpo da mensagem; só o diagnóstico da Twilio.
        detalhe = ''
        try:
            corpo = resp.json()
            detalhe = f"{corpo.get('code')} {corpo.get('message')}"
        except Exception:
            detalhe = resp.text[:200]
        raise TwilioError(f'Twilio recusou o envio (HTTP {resp.status_code}): {detalhe}')

    try:
        corpo = resp.json()
    except Exception as exc:
        raise TwilioError(f'Resposta da Twilio ilegível: {exc}') from exc

    return {
        'sid': corpo.get('sid'),
        'status': corpo.get('status'),
        'to': corpo.get('to'),
    }


# ── Leitura do payload de entrada ────────────────────────────────────────────

# Mapeia o status da Twilio para o vocabulário já usado em
# whatsapp_messages.status. Ver models.py: enviando, enviado, entregue,
# lido, erro — mais recebido/processando/processado, escritos pelo EMA.
TWILIO_STATUS = {
    'queued':      'enviando',
    'accepted':    'enviando',
    'scheduled':   'enviando',
    'sending':     'enviando',
    'sent':        'enviado',
    'delivered':   'entregue',
    'read':        'lido',
    'received':    'recebido',
    'failed':      'erro',
    'undelivered': 'erro',
    'canceled':    'erro',
}


def map_status(status_twilio):
    """Traduz o status da Twilio para o vocabulário interno."""
    return TWILIO_STATUS.get((status_twilio or '').lower(), 'enviando')


def parse_inbound(form):
    """
    Extrai o que interessa de um webhook de mensagem recebida.

    A Twilio manda application/x-www-form-urlencoded. Campos relevantes:
    MessageSid, From ('whatsapp:+55...'), Body, ProfileName, NumMedia e os
    pares MediaUrl{N}/MediaContentType{N}.

    Devolve dict. Os anexos vêm listados mas só são baixados na Fase 3.
    """
    try:
        num_media = int(form.get('NumMedia') or 0)
    except (TypeError, ValueError):
        num_media = 0

    midias = []
    for i in range(num_media):
        url = form.get(f'MediaUrl{i}')
        if url:
            midias.append({
                'url': url,
                'content_type': form.get(f'MediaContentType{i}') or '',
            })

    return {
        'message_sid': form.get('MessageSid') or form.get('SmsMessageSid') or '',
        'from_raw': form.get('From') or '',
        'to_raw': form.get('To') or '',
        'body': form.get('Body') or '',
        'profile_name': form.get('ProfileName') or '',
        'num_media': num_media,
        'media': midias,
    }
