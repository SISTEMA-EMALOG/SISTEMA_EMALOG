import os
import logging
import requests

logger = logging.getLogger(__name__)


def _cfg():
    return {
        'url':      os.environ.get('EVOLUTION_API_URL', 'https://evolution-evolution.iqutxq.easypanel.host/').rstrip('/'),
        'key':      os.environ.get('EVOLUTION_API_KEY', 'DDBC7EA2010A-440D-B3CB-349F1D24FFE7'),
        'instance': os.environ.get('EVOLUTION_INSTANCE', 'outra'),
    }


def _headers(cfg):
    return {'apikey': cfg['key'], 'Content-Type': 'application/json'}


def normalize_phone(phone: str) -> str:
    digits = ''.join(c for c in phone if c.isdigit())
    if not digits.startswith('55'):
        digits = '55' + digits
    return digits


def send_text(phone: str, message: str) -> bool:
    """Send a WhatsApp text message. Returns True on success, False on failure."""
    cfg = _cfg()
    phone_n = normalize_phone(phone)
    if not cfg['url'] or not cfg['key']:
        logger.warning(f"[EMA] Evolution API não configurada — simulando envio para {phone_n}")
        logger.info(f"[EMA SIMULADO] {phone_n}: {message[:120]}")
        return True
    try:
        url = f"{cfg['url']}/message/sendText/{cfg['instance']}"
        r = requests.post(url, json={'number': phone_n, 'text': message},
                          headers=_headers(cfg), timeout=15)
        if r.status_code in (200, 201):
            resp = r.json()
            msg_id = resp.get('key', {}).get('id', 'unknown')
            status = resp.get('status', 'unknown')
            logger.info(f"[EMA] Texto enviado → {phone_n} | id={msg_id} status={status}")
            return True
        else:
            logger.error(f"[EMA] Erro send_text HTTP {r.status_code}: {r.text[:300]}")
            return False
    except Exception as e:
        logger.error(f"[EMA] Erro send_text {phone}: {e}")
        return False


def check_number_on_whatsapp(phone: str) -> dict:
    """Check if a phone number has WhatsApp registered.
    Returns {'exists': bool, 'jid': str, 'error': str|None}
    """
    cfg = _cfg()
    phone_n = normalize_phone(phone)
    if not cfg['url'] or not cfg['key']:
        return {'exists': None, 'jid': phone_n, 'error': 'API não configurada'}
    try:
        url = f"{cfg['url']}/chat/whatsappNumbers/{cfg['instance']}"
        r = requests.post(url, json={'numbers': [phone_n]}, headers=_headers(cfg), timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                item = data[0]
                return {
                    'exists': item.get('exists', False),
                    'jid':    item.get('jid', f'{phone_n}@s.whatsapp.net'),
                    'name':   item.get('name', ''),
                    'error':  None,
                }
        return {'exists': False, 'jid': phone_n, 'error': f'HTTP {r.status_code}'}
    except Exception as e:
        logger.error(f"[EMA] check_number_on_whatsapp {phone}: {e}")
        return {'exists': None, 'jid': phone_n, 'error': str(e)}


def get_recent_messages(phone: str, limit: int = 10) -> list:
    """Get recent messages in a conversation (both sent and received)."""
    cfg = _cfg()
    phone_n = normalize_phone(phone)
    jid = f"{phone_n}@s.whatsapp.net"
    if not cfg['url'] or not cfg['key']:
        return []
    try:
        url = f"{cfg['url']}/chat/findMessages/{cfg['instance']}"
        r = requests.post(url,
            json={'where': {'key': {'remoteJid': jid}}, 'limit': limit},
            headers=_headers(cfg), timeout=10)
        if r.status_code == 200:
            data = r.json()
            records = data.get('messages', {}).get('records', [])
            return records
        return []
    except Exception as e:
        logger.error(f"[EMA] get_recent_messages {phone}: {e}")
        return []


def get_connection_status() -> dict:
    cfg = _cfg()
    if not cfg['url']:
        return {'configured': False, 'state': 'not_configured'}
    try:
        url = f"{cfg['url']}/instance/connectionState/{cfg['instance']}"
        r = requests.get(url, headers=_headers(cfg), timeout=5)
        data = r.json()
        state = (data.get('instance') or {}).get('state', 'unknown')
        return {'configured': True, 'state': state, 'instance': cfg['instance']}
    except Exception as e:
        return {'configured': True, 'state': 'error', 'error': str(e)}


def restart_instance() -> dict:
    """Soft-restart the WhatsApp Baileys connection (without QR scan).
    Uses POST /instance/restart. Returns {'ok': bool, 'message': str}
    """
    cfg = _cfg()
    if not cfg['url'] or not cfg['key']:
        return {'ok': False, 'message': 'API não configurada'}
    try:
        from urllib.parse import quote
        instance_enc = quote(cfg['instance'])
        url = f"{cfg['url']}/instance/restart/{instance_enc}"
        r = requests.post(url, headers=_headers(cfg), timeout=15)
        if r.status_code in (200, 201):
            data  = r.json()
            state = (data.get('instance') or {}).get('state', 'unknown')
            logger.info(f"[EMA] Instância reiniciada: {cfg['instance']} → state={state}")
            return {'ok': True, 'message': f"Instância reiniciada. Estado: {state}", 'state': state}
        logger.error(f"[EMA] restart_instance HTTP {r.status_code}: {r.text[:200]}")
        return {'ok': False, 'message': f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        logger.error(f"[EMA] restart_instance: {e}")
        return {'ok': False, 'message': str(e)}


def get_qr_code() -> dict:
    """Get QR code for reconnecting the WhatsApp instance.
    Returns {'ok': bool, 'base64': str, 'code': str, 'state': str}
    """
    cfg = _cfg()
    if not cfg['url'] or not cfg['key']:
        return {'ok': False, 'error': 'API não configurada'}
    try:
        from urllib.parse import quote
        instance_enc = quote(cfg['instance'])
        # GET /instance/connect returns QR code when instance is disconnected
        r = requests.get(f"{cfg['url']}/instance/connect/{instance_enc}",
                         headers={'apikey': cfg['key']}, timeout=15)
        if r.status_code == 200:
            data = r.json()
            b64  = data.get('base64', '')
            # Already connected returns state info without QR
            state = get_connection_status().get('state', 'unknown')
            if state == 'open':
                return {'ok': True, 'state': 'open', 'base64': None,
                        'message': 'WhatsApp já está conectado!'}
            return {'ok': True, 'state': state, 'base64': b64,
                    'code': data.get('code', ''), 'count': data.get('count', 0)}
        return {'ok': False, 'error': f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        logger.error(f"[EMA] get_qr_code: {e}")
        return {'ok': False, 'error': str(e)}


def set_webhook(webhook_url: str) -> dict:
    """Register (or update) the webhook on the Evolution API instance."""
    cfg = _cfg()
    if not cfg['url'] or not cfg['key']:
        return {'ok': False, 'error': 'Evolution API não configurada (EVOLUTION_API_URL / EVOLUTION_API_KEY ausentes)'}

    from urllib.parse import quote
    instance_enc = quote(cfg['instance'])
    endpoint = f"{cfg['url']}/webhook/set/{instance_enc}"

    payload = {
        'webhook': {
            'url':              webhook_url,
            'enabled':          True,
            'webhook_by_events': False,
            'webhook_base64':   False,
            'events':           ['MESSAGES_UPSERT'],
        }
    }
    try:
        r = requests.post(endpoint, json=payload, headers=_headers(cfg), timeout=15)
        if r.status_code in (200, 201):
            logger.info("[EMA] Webhook configurado com sucesso")
            return {'ok': True, 'response': r.json()}
        logger.error(f"[EMA] set_webhook falhou: {r.status_code} {r.text[:300]}")
        return {'ok': False, 'error': f"HTTP {r.status_code}: {r.text[:300]}"}
    except Exception as e:
        logger.error(f"[EMA] set_webhook exception: {e}")
        return {'ok': False, 'error': str(e)}


def get_webhook_info() -> dict:
    """Get current webhook configuration from Evolution API."""
    cfg = _cfg()
    if not cfg['url'] or not cfg['key']:
        return {'ok': False, 'error': 'não configurada'}
    from urllib.parse import quote
    instance_enc = quote(cfg['instance'])
    try:
        r = requests.get(f"{cfg['url']}/webhook/find/{instance_enc}",
                         headers=_headers(cfg), timeout=10)
        if r.status_code == 200:
            return {'ok': True, 'data': r.json()}
        return {'ok': False, 'error': f"Status {r.status_code}"}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def download_media(media_url: str, dest_path: str) -> bool:
    """Fallback: direct URL download (works only for already-decrypted URLs)."""
    cfg = _cfg()
    try:
        r = requests.get(media_url, headers=_headers(cfg), timeout=30, stream=True)
        r.raise_for_status()
        with open(dest_path, 'wb') as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        logger.info(f"[EMA] Mídia salva em {dest_path}")
        return True
    except Exception as e:
        logger.error(f"[EMA] Erro download_media: {e}")
        return False


def download_media_from_message(msg_data: dict, dest_path: str) -> tuple[bool, str]:
    """Download and decrypt WhatsApp media using Evolution API's getBase64FromMediaMessage."""
    import base64
    cfg = _cfg()
    if not cfg['url'] or not cfg['key']:
        logger.warning("[EMA] Evolution API não configurada — não é possível baixar mídia")
        return False, ''
    try:
        url = f"{cfg['url']}/chat/getBase64FromMediaMessage/{cfg['instance']}"
        payload = {
            'message': {
                'key':     msg_data.get('key', {}),
                'message': msg_data.get('message', {}),
            },
            'convertToMp4': False,
        }
        r = requests.post(url, json=payload, headers=_headers(cfg), timeout=60)
        r.raise_for_status()
        resp      = r.json()
        b64_data  = resp.get('base64', '')
        mime_type = resp.get('mimetype', '')
        if not b64_data:
            logger.error(f"[EMA] getBase64FromMediaMessage retornou vazio: {resp}")
            return False, ''
        raw_bytes = base64.b64decode(b64_data)
        with open(dest_path, 'wb') as f:
            f.write(raw_bytes)
        logger.info(f"[EMA] Mídia desencriptada salva em {dest_path} ({mime_type}, {len(raw_bytes)} bytes)")
        return True, mime_type
    except Exception as e:
        logger.error(f"[EMA] Erro download_media_from_message: {e}")
        return False, ''
