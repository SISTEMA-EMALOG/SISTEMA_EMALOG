import os
import uuid
import logging
import hashlib
import hmac
from datetime import datetime, timedelta

from flask import (Blueprint, render_template, request, jsonify,
                   flash, redirect, url_for, current_app)
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError
from app import db, socketio
from models import Driver, DriverBid, EmaSession, WhatsAppMessage

ema_bp = Blueprint('ema', __name__, url_prefix='/ema')
logger = logging.getLogger(__name__)


# ── helpers ────────────────────────────────────────────────────────────────

def _staff_only():
    return current_user.role in ('admin', 'operador', 'vendedor')


def _find_session_by_phone(phone: str) -> EmaSession | None:
    """Find the active EMA session for an incoming phone number."""
    from utils.evolution_api import normalize_phone
    phone_n = normalize_phone(phone)
    # Match against all active sessions
    sessions = EmaSession.query.filter(
        EmaSession.status.in_(['pending', 'active', 'awaiting_file', 'awaiting_confirmation'])
    ).all()
    matches = [
        session for session in sessions
        if session.driver and normalize_phone(session.driver.phone) == phone_n
    ]
    if len(matches) > 1:
        logger.error("[EMA Webhook] Telefone possui múltiplas sessões EMA ativas: %s",
                     [session.id for session in matches])
        return None
    return matches[0] if matches else None


def _find_driver_by_phone(phone: str) -> Driver | None:
    from utils.evolution_api import normalize_phone
    phone_n = normalize_phone(phone)
    matches = [
        driver for driver in Driver.query.filter_by(active=True).all()
        if normalize_phone(driver.phone) == phone_n
    ]
    if len(matches) > 1:
        logger.error("[EMA Webhook] Telefone duplicado em motoristas: %s",
                     [driver.id for driver in matches])
        return None
    return matches[0] if matches else None


def _webhook_token():
    key = os.environ.get('EVOLUTION_API_KEY', '')
    return hashlib.sha256(key.encode()).hexdigest() if key else ''


# ── routes ─────────────────────────────────────────────────────────────────

@ema_bp.route('/')
@login_required
def index():
    if not _staff_only():
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))

    return redirect(url_for('contracting.index', tab='registrations'))


@ema_bp.route('/start', methods=['POST'])
@login_required
def start_sessions():
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403

    driver_ids = request.json.get('driver_ids', [])
    if not driver_ids:
        return jsonify({'error': 'Nenhum motorista selecionado'}), 400

    from utils.ema_agent import (greeting_message, FIELD_DEFS,
                                 _is_field_applicable, _append_history)
    from utils.evolution_api import send_text

    started = 0
    errors  = []
    for did in driver_ids:
        driver = Driver.query.get(did)
        if not driver:
            continue
        # If an active session already exists, restart it in revalidation mode
        existing = (EmaSession.query
                    .filter_by(driver_id=did)
                    .filter(EmaSession.status.in_(['active', 'pending', 'awaiting_file',
                                                   'awaiting_confirmation']))
                    .first())

        applicable  = [f for f in FIELD_DEFS if _is_field_applicable(f, driver)]
        first_field = applicable[0]['key'] if applicable else None

        if existing:
            # Reuse the existing session — reset it to revalidation mode from first field
            existing.status             = 'active'
            existing.revalidation       = True
            existing.current_field      = first_field
            existing.history            = []
            existing.staged_data        = {}
            existing.error_msg          = None
            existing.completed_at       = None
            existing.started_at         = datetime.utcnow()
            existing.updated_at         = datetime.utcnow()
            # CRITICAL: reset inactivity tracking — old timestamps cause immediate reminder/abandon
            existing.reminder_sent_at   = None
            existing.last_driver_msg_at = None
            existing.abandoned_reason   = None
            ema_session = existing
        else:
            ema_session = EmaSession(
                driver_id     = driver.id,
                status        = 'active',
                revalidation  = True,
                current_field = first_field,
                history       = [],
                staged_data   = {},
                started_at    = datetime.utcnow(),
            )
            db.session.add(ema_session)
        db.session.flush()

        greeting = greeting_message(driver, revalidation=True)
        ok = send_text(driver.phone, greeting)
        if ok:
            _append_history(ema_session, 'ema', greeting)
            db.session.add(WhatsAppMessage(
                driver_id=driver.id, phone_number=driver.phone,
                message_content=greeting, direction='outbound',
                source='ema', status='enviado', created_by=current_user.id,
            ))
            started += 1
        else:
            ema_session.status    = 'error'
            ema_session.error_msg = 'Falha ao enviar mensagem inicial'
            errors.append(f"{driver.name}: falha no envio")

    db.session.commit()
    socketio.emit('contracting_conversation_update', {
        'driver_ids': [int(did) for did in driver_ids]
    }, room='operators')
    return jsonify({'started': started, 'errors': errors})


@ema_bp.route('/configure-webhook', methods=['POST'])
@login_required
def configure_webhook():
    """Call Evolution API to auto-register the webhook URL for this app."""
    if not _staff_only():
        return jsonify({'ok': False, 'error': 'Acesso negado'}), 403

    from utils.evolution_api import set_webhook, get_webhook_info

    base = request.host_url.rstrip('/')
    webhook_url = f"{base}/ema/webhook"

    result = set_webhook(webhook_url)
    if result.get('ok'):
        # Also fetch current config to confirm
        info = get_webhook_info()
        return jsonify({'ok': True, 'webhook_url': webhook_url, 'info': info.get('data')})
    return jsonify({'ok': False, 'error': result.get('error', 'Erro desconhecido')})


@ema_bp.route('/check-webhook', methods=['GET'])
@login_required
def check_webhook():
    """Return current webhook config from Evolution API."""
    if not _staff_only():
        return jsonify({'ok': False, 'error': 'Acesso negado'}), 403
    from utils.evolution_api import get_webhook_info
    return jsonify(get_webhook_info())


@ema_bp.route('/webhook/ping', methods=['GET'])
def webhook_ping():
    """Health-check endpoint — the Evolution API webhook URL should point to /ema/webhook (POST).
    Visiting this GET URL confirms the server is reachable and returns the POST URL to configure."""
    base = request.host_url.rstrip('/')
    webhook_url = f"{base}/ema/webhook"
    return jsonify({
        'status': 'ok',
        'message': 'EMA webhook está ativo. Configure a Evolution API com a URL abaixo.',
        'webhook_url': webhook_url,
        'event': 'messages.upsert',
    })


@ema_bp.route('/webhook', methods=['POST'])
def webhook():
    """Receives incoming messages from Evolution API."""
    expected = _webhook_token()
    supplied = (request.args.get('token') or request.headers.get('apikey')
                or request.headers.get('x-api-key') or '')
    api_key = os.environ.get('EVOLUTION_API_KEY', '')
    if not expected:
        logger.error("[EMA Webhook] EVOLUTION_API_KEY ausente; webhook bloqueado")
        return jsonify({'ok': False, 'error': 'webhook not configured'}), 503
    if expected and not (
        hmac.compare_digest(supplied, expected)
        or hmac.compare_digest(supplied, api_key)
    ):
        logger.warning("[EMA Webhook] Requisição sem autenticação válida")
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    try:
        raw_body = request.get_data(as_text=True)
        data  = request.get_json(silent=True) or {}
        event = data.get('event', '')

        logger.info(f"[EMA Webhook] Recebido — event='{event}' body_len={len(raw_body)}")

        # Evolution API v2 sends MESSAGES_UPSERT (uppercase), v1 sends messages.upsert
        ACCEPTED_EVENTS = {'messages.upsert', 'MESSAGES_UPSERT'}
        if event not in ACCEPTED_EVENTS:
            logger.info(f"[EMA Webhook] Evento ignorado: {event}")
            return jsonify({'ok': True})

        # ── Normalize payload: v1 has data={}, v2 has data=[{},...] ──────────
        raw_data = data.get('data', {})

        # Log full payload for first 2000 chars (helps debug unknown formats)
        logger.debug(f"[EMA Webhook] payload keys={list(data.keys())} data_type={type(raw_data).__name__}")

        # v2: data is a list — process each message item
        if isinstance(raw_data, list):
            items = raw_data
        else:
            items = [raw_data]

        for msg_data in items:
            if not isinstance(msg_data, dict):
                continue
            try:
                _process_single_message(msg_data)
            except IntegrityError:
                db.session.rollback()
                external_id = str((msg_data.get('key') or {}).get('id')
                                  or msg_data.get('id') or '').strip()
                if external_id and WhatsAppMessage.query.filter_by(
                        external_message_id=external_id).first():
                    logger.info("[EMA Webhook] Redelivery concorrente ignorada: %s", external_id)
                    continue
                raise

        return jsonify({'ok': True})

    except Exception as e:
        logger.error(f"[EMA Webhook] Erro: {e}", exc_info=True)
        db.session.rollback()
        return jsonify({'ok': False, 'error': 'processing failed'}), 500


def _process_single_message(msg_data: dict):
    """Process a single inbound WhatsApp message dict."""
    key = msg_data.get('key', {})
    external_id = str(key.get('id') or msg_data.get('id') or '').strip() or None
    existing_inbound = None
    if external_id:
        existing_inbound = WhatsAppMessage.query.filter_by(
            external_message_id=external_id).first()
        if existing_inbound and existing_inbound.status == 'processado':
            logger.info("[EMA Webhook] Mensagem já processada: %s", external_id)
            return
        if existing_inbound and existing_inbound.status == 'processando' and \
                existing_inbound.response_at and \
                datetime.utcnow() - existing_inbound.response_at < timedelta(minutes=2):
            raise RuntimeError('message processing in progress')

    # Ignore messages sent by us
    if key.get('fromMe', False):
        logger.info("[EMA Webhook] Mensagem própria (fromMe=True) — ignorada")
        return

    remote_jid  = key.get('remoteJid', '')
    phone       = remote_jid.replace('@s.whatsapp.net', '').replace('@c.us', '')

    message_obj  = msg_data.get('message', {})
    message_type = msg_data.get('messageType', '') or _detect_message_type(message_obj)

    logger.info(f"[EMA Webhook] phone={phone} type={message_type}")

    # Determine text and optional media
    text       = ''
    media_path = None

    if message_type == 'conversation':
        text = message_obj.get('conversation', '')
    elif message_type == 'extendedTextMessage':
        text = (message_obj.get('extendedTextMessage') or {}).get('text', '')
    elif message_type in ('imageMessage', 'documentMessage', 'videoMessage'):
        media_info = message_obj.get(message_type, {})
        text       = media_info.get('caption', '') or '[arquivo enviado]'
        logger.info(f"[EMA Webhook] Baixando mídia ({message_type})…")
        media_path = _download_media_decrypted(msg_data, message_type)
        logger.info(f"[EMA Webhook] Mídia salva em: {media_path}")
    elif message_type == 'audioMessage':
        text = '[áudio enviado]'
    elif message_obj:
        # Fallback: try conversation key or stringify
        text = message_obj.get('conversation', '') or str(message_obj)[:200]
    else:
        logger.info(f"[EMA Webhook] Sem conteúdo reconhecível — ignorado (type={message_type})")
        return

    if not text and not media_path:
        logger.info("[EMA Webhook] Mensagem vazia — ignorada")
        return

    ema_session = _find_session_by_phone(phone)
    from routes.driver_bids import find_active_bid_by_phone
    active_bid = find_active_bid_by_phone(phone)
    if ema_session:
        driver_for_message = ema_session.driver
    elif active_bid:
        driver_for_message = active_bid.driver
    else:
        driver_for_message = _find_driver_by_phone(phone)
    inbound = existing_inbound or WhatsAppMessage(
        driver_id=driver_for_message.id if driver_for_message else None,
        freight_id=active_bid.freight_id if active_bid else None,
        phone_number=phone, message_content=text, direction='inbound',
        source='ema' if ema_session else ('freight' if active_bid else 'general'),
        status='recebido', external_message_id=external_id,
    )
    if not existing_inbound:
        db.session.add(inbound)
    # Reserve the provider message ID before any AI/external side effect. A
    # concurrent redelivery is stopped by the unique index at this flush.
    db.session.flush()
    db.session.commit()
    inbound = WhatsAppMessage.query.filter_by(id=inbound.id).with_for_update().one()
    inbound.status = 'processando'
    inbound.response_at = datetime.utcnow()
    db.session.commit()

    if driver_for_message and driver_for_message.whatsapp_mode == 'manual':
        inbound.status = 'processado'
        db.session.commit()
        socketio.emit('contracting_conversation_update', {
            'driver_id': driver_for_message.id
        }, room='operators')
        return

    if ema_session and active_bid:
        driver_for_message.whatsapp_mode = 'manual'
        driver_for_message.whatsapp_assigned_to = None
        inbound.source = 'general'
        inbound.status = 'processado'
        db.session.commit()
        socketio.emit('contracting_conversation_update', {
            'driver_id': driver_for_message.id,
            'requires_manual_review': True,
        }, room='operators')
        logger.warning("[EMA Webhook] Sessão EMA e oferta simultâneas; atendimento automático pausado")
        return

    # ── Route 1: EMA data-enrichment session ──────────────────────────────
    if ema_session:
        driver = Driver.query.filter_by(id=ema_session.driver_id).with_for_update().one()
        if driver.whatsapp_mode == 'manual':
            inbound.status = 'processado'
            db.session.commit()
            return
        logger.info(f"[EMA Webhook] Sessão EMA → driver={driver.name} status={ema_session.status}")
        from utils.ema_agent import process_message, _append_history
        from utils.evolution_api import send_text

        driver_name  = str(driver.name)   # capture before potential session invalidation
        driver_phone = str(driver.phone)  # capture before potential session invalidation
        try:
            reply = process_message(ema_session, driver, text, media_path=media_path)
        except Exception as proc_err:
            logger.error(f"[EMA Webhook] Erro ao processar mensagem do driver {driver_name}: {proc_err}", exc_info=True)
            reply = "Xiii, deu um probleminha aqui no sistema. Pode mandar de novo? 🙏\n\nEMA | EMALOG"
            try:
                db.session.rollback()
                # Log fallback in history after rollback
                _append_history(ema_session, 'ema', '[Sistema] Erro ao processar — fallback enviado.')
                db.session.commit()
            except Exception:
                pass
        ok = send_text(driver_phone, reply)
        db.session.add(WhatsAppMessage(
            driver_id=driver.id, phone_number=driver_phone,
            message_content=reply, direction='outbound', source='ema',
            status='enviado' if ok else 'erro',
        ))
        inbound.status = 'processado' if ok else 'erro'
        inbound.response_at = datetime.utcnow()
        db.session.commit()
        socketio.emit('contracting_conversation_update', {
            'driver_id': driver.id
        }, room='operators')
        logger.info(f"[EMA Webhook] Resposta enviada para {driver_phone} — ok={ok}")
        return

    # ── Route 2: Driver bid (price consultation) ───────────────────────────
    try:
        bid = active_bid
        if bid:
            from utils.driver_bid_agent import process_bid_response, apply_bid_result
            from utils.evolution_api import send_text

            bid = DriverBid.query.filter_by(id=bid.id).with_for_update().one()
            driver = Driver.query.filter_by(id=bid.driver_id).with_for_update().one()
            if driver.whatsapp_mode == 'manual' or bid.status not in (
                    'sent', 'responded', 'no_price'):
                inbound.status = 'processado'
                db.session.commit()
                socketio.emit('contracting_conversation_update', {
                    'driver_id': driver.id
                }, room='operators')
                return
            result = process_bid_response(bid, text, apply=False)
            apply_bid_result(bid, text, result)
            if result.get('reply'):
                ok = send_text(driver.phone, result['reply'])
                db.session.add(WhatsAppMessage(
                    driver_id=driver.id, freight_id=bid.freight_id,
                    phone_number=driver.phone,
                    message_content=result['reply'], direction='outbound',
                    source='freight', status='enviado' if ok else 'erro',
                ))
            inbound.status = 'processado'
            inbound.response_at = datetime.utcnow()
            db.session.commit()
            socketio.emit('contracting_update', {
                'bid_id': bid.id, 'stage': bid.kanban_stage
            }, room='operators')
            socketio.emit('contracting_conversation_update', {
                'driver_id': driver.id
            }, room='operators')
            return
    except Exception as bid_err:
        logger.warning(f"[EMA Webhook] Erro ao verificar bid: {bid_err}")

    inbound.status = 'processado'
    inbound.response_at = datetime.utcnow()
    db.session.commit()
    if driver_for_message:
        socketio.emit('contracting_conversation_update', {
            'driver_id': driver_for_message.id
        }, room='operators')
    logger.info(f"[EMA Webhook] Nenhuma sessão/bid ativo para {phone}")


def _detect_message_type(message_obj: dict) -> str:
    """Detect message type from message object keys when messageType is missing."""
    for key in ('conversation', 'extendedTextMessage', 'imageMessage',
                'documentMessage', 'videoMessage', 'audioMessage'):
        if key in message_obj:
            return key
    return 'unknown'


def _download_media_temp(media_url: str, msg_type: str) -> str | None:
    """Fallback: direct URL download. Returns local path (may be encrypted)."""
    try:
        ext_map = {
            'imageMessage': '.jpg',
            'documentMessage': '.pdf',
            'videoMessage': '.mp4',
        }
        ext      = ext_map.get(msg_type, '.bin')
        tmp_dir  = os.path.join(current_app.root_path, 'static', 'uploads', 'ema_tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex}{ext}")
        from utils.evolution_api import download_media
        ok = download_media(media_url, tmp_path)
        return tmp_path if ok else None
    except Exception as e:
        logger.error(f"[EMA] Erro download media: {e}")
        return None


def _download_media_decrypted(msg_data: dict, msg_type: str) -> str | None:
    """Download and decrypt WhatsApp media via Evolution API getBase64FromMediaMessage.
    Falls back to direct URL download if the API call fails.
    Returns local file path or None.
    """
    from utils.evolution_api import download_media_from_message

    # Determine extension from mime_type hint or message type
    ext_map = {
        'imageMessage':    '.jpg',
        'documentMessage': '.pdf',
        'videoMessage':    '.mp4',
    }
    # Mime-type extension mapping for when API returns actual mime
    mime_ext = {
        'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
        'application/pdf': '.pdf', 'video/mp4': '.mp4',
    }

    tmp_dir  = os.path.join(current_app.root_path, 'static', 'uploads', 'ema_tmp')
    os.makedirs(tmp_dir, exist_ok=True)

    # First try: decrypting endpoint
    tmp_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex}.bin")
    ok, mime = download_media_from_message(msg_data, tmp_path)
    if ok:
        # Rename to proper extension based on detected mime type
        ext      = mime_ext.get(mime, ext_map.get(msg_type, '.bin'))
        final    = tmp_path.replace('.bin', ext)
        os.rename(tmp_path, final)
        return final

    # Fallback: try direct URL (encrypted — OCR will likely fail but file is saved)
    message_obj = msg_data.get('message', {})
    media_info  = message_obj.get(msg_type, {})
    media_url   = media_info.get('url') or media_info.get('directPath', '')
    if media_url:
        logger.warning("[EMA] Fallback para download direto (pode estar encriptado)")
        return _download_media_temp(media_url, msg_type)

    return None


@ema_bp.route('/session/<int:sid>')
@login_required
def session_detail(sid):
    if not _staff_only():
        flash('Acesso negado.', 'error')
        return redirect(url_for('ema.index'))

    from utils.ema_agent import driver_missing_fields, driver_completion_pct, FIELD_DEFS
    ema_session = EmaSession.query.get_or_404(sid)
    driver      = ema_session.driver
    missing     = driver_missing_fields(driver)
    pct         = driver_completion_pct(driver)
    return render_template('ema/session.html',
                           ema_session=ema_session, driver=driver,
                           missing=missing, pct=pct,
                           field_defs=FIELD_DEFS)


@ema_bp.route('/session/<int:sid>/send', methods=['POST'])
@login_required
def session_send(sid):
    """Operator sends a manual message in an EMA session."""
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403

    session = EmaSession.query.get_or_404(sid)
    if session.driver.whatsapp_mode != 'manual' or \
            session.driver.whatsapp_assigned_to != current_user.id:
        return jsonify({'error': 'Assuma esta conversa na Central de Contratação antes de enviar.'}), 409
    text    = (request.json or {}).get('text', '').strip()
    if not text:
        return jsonify({'error': 'Mensagem vazia'}), 400

    from utils.ema_agent import _append_history
    from utils.evolution_api import send_text

    ok = send_text(session.driver.phone, text)
    _append_history(session, 'ema', f'[Operador] {text}')
    db.session.commit()
    return jsonify({'ok': ok})


@ema_bp.route('/session/<int:sid>/simulate', methods=['POST'])
@login_required
def session_simulate(sid):
    """Simulate an incoming driver message — useful when webhook isn't configured yet.
    Processes the text through the AI as if the driver had sent it via WhatsApp."""
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403

    ema_session = EmaSession.query.get_or_404(sid)
    if ema_session.driver.whatsapp_mode == 'manual':
        return jsonify({'error': 'Devolva a conversa à EMA antes de simular uma resposta.'}), 409
    text        = (request.json or {}).get('text', '').strip()
    if not text:
        return jsonify({'error': 'Mensagem vazia'}), 400

    if ema_session.status not in ('active', 'awaiting_file', 'awaiting_confirmation'):
        return jsonify({'error': 'Sessão não está ativa'}), 400

    driver = ema_session.driver
    from utils.ema_agent import process_message
    from utils.evolution_api import send_text

    reply = process_message(ema_session, driver, text, media_path=None)
    db.session.commit()
    send_text(driver.phone, reply)
    logger.info(f"[EMA Simulate] Mensagem simulada para {driver.name}: '{text[:60]}' → '{reply[:60]}'")
    return jsonify({'ok': True, 'reply': reply})


@ema_bp.route('/session/<int:sid>/reset', methods=['POST'])
@login_required
def session_reset(sid):
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403

    ema_session = EmaSession.query.get_or_404(sid)
    driver = ema_session.driver
    from utils.ema_agent import FIELD_DEFS, _is_field_applicable, driver_missing_fields

    revalidation = ema_session.revalidation or False
    if revalidation:
        applicable = [f for f in FIELD_DEFS if _is_field_applicable(f, driver)]
        first_field = applicable[0]['key'] if applicable else None
    else:
        missing = driver_missing_fields(driver)
        first_field = missing[0]['key'] if missing else None

    ema_session.status        = 'active'
    ema_session.history       = []
    ema_session.error_msg     = None
    ema_session.current_field = first_field
    ema_session.completed_at  = None
    db.session.commit()
    flash('Sessão reiniciada.', 'success')
    return redirect(url_for('ema.session_detail', sid=sid))


@ema_bp.route('/session/<int:sid>/restart', methods=['POST'])
@login_required
def session_restart(sid):
    """Re-send the greeting and re-activate the session.
    Cooldown: refuses to resend if the last EMA message was sent within 5 minutes,
    unless the operator passes force=true in the request body.
    """
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403

    ema_session = EmaSession.query.get_or_404(sid)
    driver      = ema_session.driver
    force       = (request.json or {}).get('force', False)

    # ── Cooldown check ─────────────────────────────────────────────────────
    COOLDOWN_MINUTES = 5
    hist = ema_session.history or []
    # Find the last message sent by EMA (not operator, not driver)
    last_ema_ts = None
    for entry in reversed(hist):
        if entry.get('role') == 'ema':
            try:
                last_ema_ts = datetime.fromisoformat(entry['ts'])
            except Exception:
                pass
            break

    if last_ema_ts and not force:
        from datetime import timezone
        now = datetime.utcnow()
        diff_secs = (now - last_ema_ts).total_seconds()
        if diff_secs < COOLDOWN_MINUTES * 60:
            remaining = int(COOLDOWN_MINUTES * 60 - diff_secs)
            mins = remaining // 60
            secs = remaining % 60
            return jsonify({
                'ok':       False,
                'cooldown': True,
                'remaining_secs': remaining,
                'message': f'Aguarde {mins}m{secs:02d}s antes de reenviar (evita spam e bloqueio no WhatsApp).',
            })

    from utils.ema_agent import (greeting_message, FIELD_DEFS, _is_field_applicable,
                                 driver_missing_fields, _append_history)
    from utils.evolution_api import send_text

    revalidation = ema_session.revalidation or False
    if revalidation:
        applicable  = [f for f in FIELD_DEFS if _is_field_applicable(f, driver)]
        first_field = applicable[0]['key'] if applicable else None
    else:
        missing     = driver_missing_fields(driver)
        first_field = missing[0]['key'] if missing else None

    ema_session.status            = 'active'
    ema_session.current_field     = first_field
    ema_session.error_msg         = None
    ema_session.abandoned_reason  = None
    ema_session.reminder_sent_at  = None
    ema_session.last_driver_msg_at= None

    greeting = greeting_message(driver, revalidation=revalidation)
    ok = send_text(driver.phone, greeting)
    if ok:
        _append_history(ema_session, 'ema', greeting)
    db.session.commit()
    return jsonify({'ok': ok, 'cooldown': False})


@ema_bp.route('/session/<int:sid>/validate', methods=['POST'])
@login_required
def session_validate(sid):
    """Operator marks a completed session as validated (approved)."""
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403

    ema_session = EmaSession.query.get_or_404(sid)
    if ema_session.status not in ('completed', 'awaiting_confirmation'):
        return jsonify({'error': 'Sessão não está aguardando validação'}), 400

    ema_session.status = 'validated'
    driver = ema_session.driver
    driver.validated = True  # mark driver as validated (add column if missing via migration)
    db.session.commit()
    flash(f'Cadastro de {driver.name} validado com sucesso!', 'success')
    return redirect(url_for('ema.session_detail', sid=sid))


@ema_bp.route('/session/<int:sid>/poll')
@login_required
def session_poll(sid):
    """Lightweight JSON endpoint for chat polling — returns message count + last ts."""
    sess = EmaSession.query.get_or_404(sid)
    hist = sess.history or []
    last_ts = hist[-1]['ts'] if hist else None
    return jsonify({'count': len(hist), 'last_ts': last_ts, 'status': sess.status})


@ema_bp.route('/api/status')
@login_required
def api_status():
    from utils.evolution_api import get_connection_status
    status = get_connection_status()
    # Translate state to pt-BR
    state_labels = {
        'open':          'Conectado',
        'close':         'Desconectado',
        'connecting':    'Conectando…',
        'not_configured':'Não configurado',
        'error':         'Erro',
        'unknown':       'Desconhecido',
    }
    status['state_label'] = state_labels.get(status.get('state', ''), status.get('state', ''))
    return jsonify(status)


@ema_bp.route('/api/reconnect', methods=['POST'])
@login_required
def api_reconnect():
    """Soft-restart the WhatsApp Baileys connection (no QR needed if session is valid)."""
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403
    from utils.evolution_api import restart_instance
    result = restart_instance()
    return jsonify(result)


@ema_bp.route('/api/qrcode')
@login_required
def api_qrcode():
    """Return the WhatsApp QR code as a JSON object (base64 image string).
    Only available when instance is disconnected/needs re-authentication.
    """
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403
    from utils.evolution_api import get_qr_code
    return jsonify(get_qr_code())


@ema_bp.route('/reconnect')
@login_required
def reconnect_page():
    """WhatsApp reconnect page — shows QR code for scanning."""
    if not _staff_only():
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))
    from utils.evolution_api import get_connection_status
    status = get_connection_status()
    return render_template('ema/reconnect.html', evo_status=status)


@ema_bp.route('/api/diagnostics/<phone>')
@login_required
def api_diagnostics(phone):
    """Diagnostic: check if a phone number is on WhatsApp and return recent messages."""
    if not _staff_only():
        return jsonify({'error': 'Acesso negado'}), 403
    from utils.evolution_api import (check_number_on_whatsapp, get_recent_messages,
                                     get_connection_status)
    conn   = get_connection_status()
    exists = check_number_on_whatsapp(phone)
    msgs   = get_recent_messages(phone, limit=5)

    # Build message summary
    msg_summary = []
    for m in msgs:
        from_me  = m.get('key', {}).get('fromMe', False)
        msg_obj  = m.get('message', {})
        text     = (msg_obj.get('conversation')
                    or (msg_obj.get('extendedTextMessage') or {}).get('text')
                    or '[mídia]')
        msg_summary.append({
            'direction': 'bot' if from_me else 'driver',
            'text':      text[:80],
            'status':    m.get('status', '?'),
            'ts':        m.get('messageTimestamp'),
        })

    bot_count    = sum(1 for m in msg_summary if m['direction'] == 'bot')
    driver_count = sum(1 for m in msg_summary if m['direction'] == 'driver')

    return jsonify({
        'connection': conn,
        'whatsapp':   exists,
        'recent_messages': msg_summary,
        'bot_count':    bot_count,
        'driver_count': driver_count,
        'warning': (
            'Motorista nunca respondeu — pode ter bloqueado o número ou estar sem WhatsApp ativo nesse telefone.'
            if driver_count == 0 and bot_count >= 3 else None
        ),
    })


@ema_bp.route('/api/driver/<int:did>/pct')
@login_required
def api_driver_pct(did):
    driver = Driver.query.get_or_404(did)
    from utils.ema_agent import driver_completion_pct, driver_missing_fields
    return jsonify({'pct': driver_completion_pct(driver),
                    'missing': len(driver_missing_fields(driver))})
