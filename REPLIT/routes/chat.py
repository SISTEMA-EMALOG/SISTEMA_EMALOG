from flask import Blueprint, render_template, request, redirect, url_for, flash, send_from_directory, abort
from flask_login import login_required, current_user
from flask_socketio import emit, join_room, leave_room
from models import ChatMessage, User
from app import db, socketio
from werkzeug.utils import secure_filename
from sqlalchemy import func
import os
from datetime import datetime

chat_bp = Blueprint('chat', __name__, url_prefix='/chat')

CHAT_UPLOAD_FOLDER = os.path.join('uploads', 'chat')

@chat_bp.route('/')
@login_required
def index():
    rooms_data = _get_rooms_for_user(current_user)

    # Cliente com sala única → vai direto para a sala
    if isinstance(rooms_data, list):
        if len(rooms_data) == 1:
            return redirect(url_for('chat.room', room_id=rooms_data[0]['id']))
        _enrich_rooms(rooms_data)
        return render_template('chat/index.html',
                               internal_rooms=rooms_data,
                               client_rooms=[],
                               is_staff=False)

    # Admin / Operador — estrutura dict
    internal = rooms_data.get('internal', [])
    clients  = rooms_data.get('clients', [])
    _enrich_rooms(internal)
    _enrich_rooms(clients)
    return render_template('chat/index.html',
                           internal_rooms=internal,
                           client_rooms=clients,
                           is_staff=True)


@chat_bp.route('/room/<room_id>')
@login_required
def room(room_id):
    if not can_access_room(current_user, room_id):
        flash('Acesso negado a esta sala.', 'error')
        return redirect(url_for('chat.index'))

    # Limit to the most recent 200 messages to avoid loading unbounded history
    MESSAGE_LIMIT = 200
    messages = ChatMessage.query.filter_by(room=room_id) \
        .order_by(ChatMessage.sent_at.desc()) \
        .limit(MESSAGE_LIMIT).all()
    messages.reverse()

    # Marcar como lidas
    ChatMessage.query.filter_by(
        room=room_id, is_read=False
    ).filter(ChatMessage.sender_id != current_user.id).update({'is_read': True})
    db.session.commit()

    room_info = get_room_info(room_id)
    rooms_data = _get_rooms_for_user(current_user)

    # Build flat sidebar list
    if isinstance(rooms_data, list):
        sidebar_rooms = rooms_data
    else:
        sidebar_rooms = rooms_data.get('internal', []) + rooms_data.get('clients', [])

    _enrich_rooms(sidebar_rooms)

    from datetime import timedelta
    today     = datetime.utcnow().date()
    yesterday = today - timedelta(days=1)

    return render_template('chat/room.html',
                           room_id=room_id,
                           room_info=room_info,
                           messages=messages,
                           rooms_list=sidebar_rooms,
                           now_date=today.strftime('%d/%m/%Y'),
                           yesterday_date=yesterday.strftime('%d/%m/%Y'))


@chat_bp.route('/file/<path:filename>')
@login_required
def serve_file(filename):
    """Serve chat-uploaded files securely"""
    from utils.storage import get_file as _get
    from flask import send_file as _send
    safe_name = secure_filename(os.path.basename(filename))
    # Try new Object Storage key first, fall back to legacy filesystem path
    key = f"uploads/chat/{safe_name}"
    buf = _get(key)
    if buf is None:
        abort(404)
    return _send(buf, as_attachment=True, download_name=safe_name)

def _enrich_rooms(rooms):
    """Add last_message, unread_count, total_count to each room dict in-place.

    Uses bulk queries (grouped by room) instead of 3 queries per room to
    avoid N+1 behaviour when there are many rooms.
    """
    if not rooms:
        return
    room_ids = [r['id'] for r in rooms]

    # Total count and last message id/time per room in one grouped query
    last_sub = db.session.query(
        ChatMessage.room,
        func.max(ChatMessage.id).label('last_id'),
        func.count(ChatMessage.id).label('total_count')
    ).filter(ChatMessage.room.in_(room_ids)).group_by(ChatMessage.room).all()

    last_ids = [row.last_id for row in last_sub]
    totals_by_room = {row.room: row.total_count for row in last_sub}

    last_messages = {}
    if last_ids:
        from sqlalchemy.orm import joinedload
        for msg in ChatMessage.query.options(joinedload(ChatMessage.sender)).filter(ChatMessage.id.in_(last_ids)).all():
            last_messages[msg.room] = msg

    # Unread counts per room in one grouped query
    unread_rows = db.session.query(
        ChatMessage.room,
        func.count(ChatMessage.id).label('unread_count')
    ).filter(
        ChatMessage.room.in_(room_ids),
        ChatMessage.is_read == False,
        ChatMessage.sender_id != current_user.id
    ).group_by(ChatMessage.room).all()
    unread_by_room = {row.room: row.unread_count for row in unread_rows}

    for room in rooms:
        rid = room['id']
        room['last_message'] = last_messages.get(rid)
        room['unread_count'] = unread_by_room.get(rid, 0)
        room['total_count'] = totals_by_room.get(rid, 0)


def _get_rooms_for_user(user):
    """Return list of room dicts accessible by this user.

    For admin/operador the result is a dict with two keys:
        'internal'  – internal team rooms
        'clients'   – one room per active client (has portal users OR has messages)

    For cliente the result is a plain list (single room).
    """
    if user.role in ('admin', 'operador', 'vendedor'):
        internal = [
            {'id': 'general',    'name': 'Geral',      'description': 'Chat geral da empresa',       'icon': 'comments',  'group': 'internal'},
            {'id': 'operations', 'name': 'Operações',  'description': 'Chat da equipe de operações', 'icon': 'cogs',      'group': 'internal'},
        ]
        if user.role == 'admin':
            internal.append(
                {'id': 'support', 'name': 'Suporte Geral', 'description': 'Canal de suporte interno', 'icon': 'headset', 'group': 'internal'}
            )

        # Build one room per active client (with portal users OR already has chat messages)
        # Bulk-fetch to avoid N+2 queries per client.
        from models import Client, User as UserModel
        clients = Client.query.filter_by(active=True).order_by(Client.company_name).all()

        client_ids_with_users = {
            row.client_id for row in
            UserModel.query.with_entities(UserModel.client_id)
            .filter(UserModel.role == 'cliente', UserModel.client_id.isnot(None))
            .distinct().all()
        }
        client_rooms_with_messages = {
            row.room for row in
            ChatMessage.query.with_entities(ChatMessage.room)
            .filter(ChatMessage.room.like('client_%'))
            .distinct().all()
        }

        client_rooms = []
        for c in clients:
            room_id = f'client_{c.id}'
            has_users = c.id in client_ids_with_users
            has_messages = room_id in client_rooms_with_messages
            if has_users or has_messages:
                client_rooms.append({
                    'id': room_id,
                    'name': c.company_name,
                    'description': f'Chat com {c.company_name}',
                    'icon': 'building',
                    'group': 'client',
                    'client': c,
                })

        return {'internal': internal, 'clients': client_rooms}

    elif user.role == 'cliente' and user.client_id:
        return [
            {'id': f'client_{user.client_id}', 'name': 'Suporte EMALOG',
             'description': 'Fale diretamente com nossa equipe', 'icon': 'headset', 'group': 'client'},
        ]
    return []


def can_access_room(user, room_id):
    """Check if user can access the chat room"""
    if user.role == 'admin':
        return True
    elif user.role in ('operador', 'vendedor'):
        return room_id in ['general', 'operations'] or room_id.startswith('client_')
    elif user.role == 'cliente':
        return room_id == f'client_{user.client_id}'
    return False

def get_room_info(room_id):
    """Get room information"""
    room_names = {
        'general': 'Chat Geral',
        'operations': 'Operações',
        'support': 'Suporte'
    }
    
    if room_id.startswith('client_'):
        client_id = room_id.replace('client_', '')
        from models import Client
        client = Client.query.get(client_id)
        if client:
            return {'name': f'Cliente: {client.company_name}', 'description': 'Chat com cliente'}
    
    return {
        'name': room_names.get(room_id, room_id),
        'description': 'Chat da equipe'
    }

# ── Socket.IO events ──────────────────────────────────────────────────────────

@socketio.on('join')
def on_join(data):
    room = data.get('room', '')
    if can_access_room(current_user, room):
        join_room(room)
        # Silently join — no noisy status broadcast

@socketio.on('leave')
def on_leave(data):
    room = data.get('room', '')
    if can_access_room(current_user, room):
        leave_room(room)

@socketio.on('message')
def handle_message(data):
    room = data.get('room', '')
    message_content = (data.get('message') or '').strip()

    if not can_access_room(current_user, room):
        return
    if not message_content:
        return
    if len(message_content) > 1000:
        emit('error', {'message': 'Mensagem muito longa (máx. 1000 caracteres).'})
        return

    msg = ChatMessage(
        room=room,
        sender_id=current_user.id,
        message_content=message_content,
        message_type='text'
    )
    db.session.add(msg)
    db.session.commit()

    payload = {
        'id': msg.id,
        'message': message_content,
        'username': current_user.username,
        'user_initial': current_user.username[0].upper(),
        'timestamp': msg.sent_at.strftime('%H:%M'),
        'date': msg.sent_at.strftime('%d/%m/%Y'),
        'is_own': False
    }
    emit('message', payload, room=room, include_self=False)
    payload['is_own'] = True
    payload['username'] = 'Você'
    emit('message', payload)


@socketio.on('file_upload')
def handle_file_upload(data):
    import base64
    room = data.get('room', '')
    if not can_access_room(current_user, room):
        return

    try:
        file_data = data.get('file_data', '')
        original_name = secure_filename(data.get('filename', 'arquivo'))

        # Decode base64 and save
        if ',' in file_data:
            file_data = file_data.split(',', 1)[1]
        raw = base64.b64decode(file_data)

        # Limit 10 MB
        if len(raw) > 10 * 1024 * 1024:
            emit('error', {'message': 'Arquivo muito grande (máx. 10 MB).'})
            return

        ts_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{original_name}"
        key = f"uploads/chat/{ts_name}"

        from utils.storage import save_file as _save
        _save(raw, key)

        msg = ChatMessage(
            room=room,
            sender_id=current_user.id,
            message_content=f'Arquivo: {original_name}',
            message_type='file',
            file_path=key
        )
        db.session.add(msg)
        db.session.commit()

        # Build a proper download URL
        download_url = f'/chat/file/{ts_name}'
        ext = original_name.rsplit('.', 1)[-1].lower() if '.' in original_name else ''
        is_image = ext in ('jpg', 'jpeg', 'png', 'gif', 'webp')

        payload = {
            'id': msg.id,
            'filename': original_name,
            'download_url': download_url,
            'is_image': is_image,
            'username': current_user.username,
            'user_initial': current_user.username[0].upper(),
            'timestamp': msg.sent_at.strftime('%H:%M'),
            'date': msg.sent_at.strftime('%d/%m/%Y'),
            'is_own': False
        }
        emit('file_message', payload, room=room, include_self=False)
        payload['is_own'] = True
        payload['username'] = 'Você'
        emit('file_message', payload)

    except Exception as e:
        emit('error', {'message': f'Erro ao enviar arquivo: {str(e)}'})


@socketio.on('typing')
def handle_typing(data):
    room = data.get('room', '')
    if can_access_room(current_user, room):
        emit('typing', {
            'username': current_user.username,
            'typing': data.get('typing', False)
        }, room=room, include_self=False)
