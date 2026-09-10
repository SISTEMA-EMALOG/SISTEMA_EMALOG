from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user
from flask_socketio import emit, join_room, leave_room, disconnect
from models import User, Quote, Client, AuditLog
from app import db, socketio
from datetime import datetime
import logging

notifications_bp = Blueprint('notifications', __name__, url_prefix='/notifications')

@notifications_bp.route('/')
@login_required
def index():
    """Página de notificações (opcional para histórico)"""
    return render_template('notifications/index.html')

@notifications_bp.route('/mark-read', methods=['POST'])
@login_required
def mark_as_read():
    """Marcar notificação como lida"""
    notification_id = request.json.get('notification_id')
    # Aqui você pode implementar um modelo de notificações se quiser histórico
    return jsonify({'success': True})

# Funções para enviar notificações
def notify_quote_created(quote):
    """Notifica operadores/admins sobre nova cotação"""
    try:
        logging.info(f"Enviando notificação de nova cotação: {quote.quote_number}")

        # Buscar todos os operadores e admins ativos
        operators = User.query.filter(
            User.role.in_(['admin', 'operador', 'vendedor']),
            User.active == True
        ).all()

        if not operators:
            logging.warning(f"Nenhum operador/admin ativo encontrado para notificar sobre cotação {quote.quote_number}")
            return

        notification_data = {
            'quote_id': quote.id,
            'quote_number': quote.quote_number,
            'client_name': quote.client.company_name if quote.client else 'Cliente não encontrado',
            'created_at': quote.created_at.strftime('%d/%m/%Y %H:%M'),
            'message': f'Nova cotação {quote.quote_number} criada pelo cliente {quote.client.company_name if quote.client else "Desconhecido"}'
        }

        logging.info(f"Enviando notificação para {len(operators)} operadores/admins")

        successful_sends = 0
        for operator in operators:
            try:
                room_name = f'user_{operator.id}'
                socketio.emit('new_quote_notification', notification_data, room=room_name)
                logging.debug(f"Notificação enviada para {operator.username} (ID: {operator.id}) - Room: {room_name}")
                successful_sends += 1
            except Exception as e:
                logging.error(f"Erro ao enviar notificação para operador {operator.username}: {e}")

        logging.info(f"Notificação de nova cotação enviada com sucesso para {successful_sends}/{len(operators)} operadores")

    except Exception as e:
        logging.error(f'Erro crítico ao enviar notificação de nova cotação {quote.quote_number}: {str(e)}')
        import traceback
        logging.error(traceback.format_exc())

def notify_quote_priced(quote):
    """Notifica cliente sobre cotação com preço definido"""
    try:
        logging.info(f"🔔 INICIANDO notificação de cotação precificada: {quote.quote_number}")
        logging.info(f"📊 Dados da cotação: ID={quote.id}, Cliente ID={quote.client_id}, Valor={quote.sale_value}")

        # Buscar usuários ativos do cliente específico
        client_users = User.query.filter(
            User.client_id == quote.client_id,
            User.role == 'cliente',
            User.active == True
        ).all()

        logging.info(f"📋 Usuários ATIVOS encontrados para cliente ID {quote.client_id}: {[f'{u.username}(ID:{u.id})' for u in client_users]}")

        if not client_users:
            logging.error(f"❌ NENHUM usuário ativo encontrado para o cliente ID {quote.client_id}")
            return False

        # Verificar se a cotação tem preço definido
        if not quote.sale_value or quote.sale_value <= 0:
            logging.error(f"❌ Cotação sem preço válido: {quote.quote_number}")
            return False

        notification_data = {
            'quote_id': quote.id,
            'quote_number': quote.quote_number,
            'sale_value': float(quote.sale_value),
            'valid_until': quote.valid_until.strftime('%d/%m/%Y') if quote.valid_until else 'Não definido',
            'client_name': quote.client.company_name,
            'timestamp': datetime.now().isoformat(),
            'message': f'🎉 Cotação {quote.quote_number} foi AVALIADA! Valor: R$ {quote.sale_value:,.2f}'
        }

        logging.info(f"📦 DADOS DA NOTIFICAÇÃO: {notification_data}")

        # Enviar notificação com múltiplas tentativas
        successful_sends = 0
        for client_user in client_users:
            try:
                # Sala específica do usuário
                user_room = f'user_{client_user.id}'
                
                logging.info(f"🚀 ENVIANDO para {client_user.username} (ID: {client_user.id}) - Sala: {user_room}")
                
                # Emitir para a sala específica do usuário
                socketio.emit('quote_priced_notification', notification_data, room=user_room)
                logging.info(f"✅ EMITIDO para sala: {user_room}")
                
                # Emitir também para todos os sockets conectados (broadcast)
                socketio.emit('quote_priced_notification', notification_data, broadcast=True)
                logging.info(f"📡 BROADCAST enviado para todos os clientes conectados")
                
                # Emitir para sala do cliente
                client_room = f'client_{quote.client_id}'
                socketio.emit('quote_priced_notification', notification_data, room=client_room)
                logging.info(f"🏢 EMITIDO para sala do cliente: {client_room}")
                
                successful_sends += 1
                
            except Exception as e:
                logging.error(f"❌ ERRO ao enviar para {client_user.username}: {e}")
                import traceback
                logging.error(traceback.format_exc())

        logging.info(f"🎯 RESULTADO: {successful_sends}/{len(client_users)} notificações enviadas")
        return successful_sends > 0

    except Exception as e:
        logging.error(f'💥 ERRO CRÍTICO na notificação {quote.quote_number}: {str(e)}')
        import traceback
        logging.error(traceback.format_exc())
        return False

def notify_quote_approved(quote, freight):
    """Notifica operadores/admins sobre cotação aprovada pelo cliente"""
    try:
        logging.info(f"Enviando notificação de cotação aprovada: {quote.quote_number}")

        # Buscar todos os operadores e admins ativos
        operators = User.query.filter(
            User.role.in_(['admin', 'operador', 'vendedor']),
            User.active == True
        ).all()

        if not operators:
            logging.warning(f"Nenhum operador/admin ativo para notificar aprovação da cotação {quote.quote_number}")
            return

        notification_data = {
            'quote_id': quote.id,
            'freight_id': freight.id,
            'quote_number': quote.quote_number,
            'freight_number': freight.freight_number,
            'client_name': quote.client.company_name if quote.client else 'Cliente não encontrado',
            'approved_at': quote.approved_at.strftime('%d/%m/%Y %H:%M') if quote.approved_at else datetime.now().strftime('%d/%m/%Y %H:%M'),
            'message': f'Cliente {quote.client.company_name if quote.client else "Desconhecido"} aprovou a cotação {quote.quote_number}. Frete {freight.freight_number} criado!'
        }

        logging.info(f"Enviando notificação de aprovação para {len(operators)} operadores/admins")

        successful_sends = 0
        for operator in operators:
            try:
                room_name = f'user_{operator.id}'
                socketio.emit('quote_approved_notification', notification_data, room=room_name)
                logging.debug(f"Notificação de aprovação enviada para {operator.username} (ID: {operator.id}) - Room: {room_name}")
                successful_sends += 1
            except Exception as e:
                logging.error(f"Erro ao enviar notificação para operador {operator.username}: {e}")

        logging.info(f"Notificação de aprovação enviada para {successful_sends}/{len(operators)} operadores")

    except Exception as e:
        logging.error(f'Erro crítico ao enviar notificação de cotação aprovada {quote.quote_number}: {str(e)}')
        import traceback
        logging.error(traceback.format_exc())

def notify_freight_status_update(freight, old_status, new_status):
    """Notifica cliente sobre atualização de status do frete"""
    try:
        logging.info(f"Enviando notificação de status do frete: {freight.freight_number} ({old_status} -> {new_status})")

        # Verificar se os status são diferentes
        if old_status == new_status:
            logging.warning(f"Status inalterado para frete {freight.freight_number}: {old_status}")
            return

        # Buscar usuários ativos do cliente específico
        client_users = User.query.filter(
            User.client_id == freight.client_id,
            User.role == 'cliente',
            User.active == True
        ).all()

        if not client_users:
            logging.warning(f"Nenhum usuário ativo encontrado para o cliente ID {freight.client_id} (frete {freight.freight_number})")
            return

        status_names = {
            'ofertado': 'Ofertado',
            'aceito': 'Aceito',
            'em_coleta': 'Em Coleta',
            'em_transito': 'Em Trânsito', 
            'entregue': 'Entregue',
            'cancelado': 'Cancelado'
        }

        notification_data = {
            'freight_id': freight.id,
            'freight_number': freight.freight_number,
            'old_status': status_names.get(old_status, old_status),
            'new_status': status_names.get(new_status, new_status),
            'updated_at': datetime.now().strftime('%d/%m/%Y %H:%M'),
            'message': f'Status do frete {freight.freight_number} foi atualizado para {status_names.get(new_status, new_status)}'
        }

        logging.info(f"Enviando notificação de status para {len(client_users)} usuários do cliente ID {freight.client_id}")

        # Enviar APENAS para salas específicas dos usuários
        successful_sends = 0
        for client_user in client_users:
            try:
                room_name = f'user_{client_user.id}'
                socketio.emit('freight_status_notification', notification_data, room=room_name)
                logging.debug(f"Notificação de status enviada para {client_user.username} (ID: {client_user.id}) - Room: {room_name}")
                successful_sends += 1
            except Exception as e:
                logging.error(f"Erro ao enviar notificação para cliente {client_user.username}: {e}")

        logging.info(f"Notificação de status do frete enviada para {successful_sends}/{len(client_users)} usuários")

    except Exception as e:
        logging.error(f'Erro crítico ao enviar notificação de status do frete {freight.freight_number}: {str(e)}')
        import traceback
        logging.error(traceback.format_exc())

# Eventos SocketIO para notificações
@socketio.on('join_notifications')
def on_join_notifications():
    """Usuário entra na sala de notificações com verificação de segurança"""
    if not current_user.is_authenticated:
        logging.warning(f"Tentativa de conexão não autenticada no SocketIO")
        emit('notification_status', {'status': 'unauthorized', 'message': 'Usuário não autenticado'})
        disconnect()
        return

    # Verificar se o usuário está ativo
    user = User.query.get(current_user.id)
    if not user or not user.active:
        logging.warning(f"Usuário inativo tentou se conectar: {current_user.id}")
        emit('notification_status', {'status': 'inactive', 'message': 'Usuário inativo'})
        disconnect()
        return

    # Entrar na sala específica do usuário
    room_name = f'user_{current_user.id}'
    join_room(room_name)

    # Log de segurança
    logging.info(f"Usuário {current_user.username} (ID: {current_user.id}, Role: {current_user.role}) conectou-se às notificações")

    # Registrar no audit log
    try:
        audit = AuditLog(
            user_id=current_user.id,
            action='notification_connect',
            table_name='notifications',
            new_values=f'Usuário {current_user.username} conectou-se às notificações'
        )
        db.session.add(audit)
        db.session.commit()
    except Exception as e:
        logging.error(f"Erro ao registrar audit log: {e}")

    emit('notification_status', {
        'status': 'connected',
        'room': room_name,
        'user_role': current_user.role,
        'message': 'Conectado ao sistema de notificações'
    })

@socketio.on('leave_notifications')
def on_leave_notifications():
    """Usuário sai da sala de notificações"""
    if current_user.is_authenticated:
        room_name = f'user_{current_user.id}'
        leave_room(room_name)

        # Log de segurança
        logging.info(f"Usuário {current_user.username} (ID: {current_user.id}) desconectou-se das notificações")

        # Registrar no audit log
        try:
            audit = AuditLog(
                user_id=current_user.id,
                action='notification_disconnect',
                table_name='notifications',
                new_values=f'Usuário desconectou-se do sistema de notificações - Room: {room_name}'
            )
            db.session.add(audit)
            db.session.commit()
        except Exception as e:
            logging.error(f"Erro ao registrar audit log: {e}")