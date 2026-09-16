"""
Central de Atendimento — blueprint do módulo.

Fase 0 entregou a modelagem. Fase 1 entrega a entrada e a saída pela Twilio,
mais a tela de conversas ligada a essa fonte de dados.

Regra de arquitetura: este módulo só tem lógica de atendimento. Núcleo,
modelos e utilitários compartilhados vêm de infraestrutura_critica/.
"""

import logging
import os
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, inspect as sa_inspect

from infraestrutura_critica.app import db
from infraestrutura_critica.models import (
    Conversation,
    User,
    WhatsAppMessage,
    CONVERSATION_ACTIVE_STATUSES,
    CONVERSATION_STATUS_OPEN,
    CONVERSATION_STATUS_PENDING,
    CONVERSATION_STATUS_RESOLVED,
)
from atendimento_conversas.utils import providers, twilio_client
from atendimento_conversas.utils.conversas_service import (
    EVENTO_MENSAGEM,
    assumir_conversa,
    avisar_conversa,
    get_or_create_conversation,
    mensagem_ja_processada,
    registrar_entrada,
    registrar_saida,
    serializar_conversa,
    serializar_mensagem,
)

log = logging.getLogger(__name__)

conversas_bp = Blueprint('conversas', __name__, url_prefix='/conversas')

LIMITE_MENSAGEM = 2000


def _staff_only():
    return current_user.role in ('admin', 'operador', 'vendedor')


def _forbidden():
    return jsonify({'success': False, 'error': 'Acesso negado'}), 403


# ── Tela ─────────────────────────────────────────────────────────────────────

@conversas_bp.route('/')
@login_required
def index():
    if not _staff_only():
        return _forbidden()
    canal = providers.canal_configurado()
    evolution = providers.estado_evolution()
    return render_template(
        'conversas/index.html',
        canal_ativo=canal,
        # A Evolution pode estar configurada mas com o WhatsApp desconectado.
        # Nesse caso o envio falha, então a tela avisa antes.
        evolution_conectada=(evolution.get('estado') == 'open'),
        evolution_estado=evolution.get('estado'),
    )


# ── Webhook de entrada da Twilio ─────────────────────────────────────────────

def _resposta_twiml(vazio=True):
    """A Twilio espera TwiML. Vazio significa 'recebi, não responda nada'."""
    corpo = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    return corpo, 200, {'Content-Type': 'application/xml'}


@conversas_bp.route('/webhook', methods=['POST'])
def webhook():
    """
    Recebe mensagem de WhatsApp da Twilio.

    Sem @login_required de propósito: é chamada server-to-server. A
    autenticação é a assinatura X-Twilio-Signature, e o caminho está em
    CSRF_EXEMPT_PREFIXES porque não existe sessão de browser aqui.

    Falha fechada: sem TWILIO_AUTH_TOKEN configurado, recusa tudo. Um webhook
    aberto deixa qualquer um injetar mensagem falsa no sistema.
    """
    if not twilio_client.auth_token():
        log.error("❌ Conversas: webhook chamado sem TWILIO_AUTH_TOKEN configurado.")
        return jsonify({'error': 'Integração não configurada'}), 503

    assinatura = request.headers.get('X-Twilio-Signature', '')
    url = twilio_client.webhook_url(request)
    if not twilio_client.validate_signature(url, request.form.to_dict(), assinatura):
        log.warning(
            "⚠️ Conversas: assinatura Twilio inválida. IP %s, URL %s",
            request.remote_addr, url,
        )
        return jsonify({'error': 'Assinatura inválida'}), 403

    dados = twilio_client.parse_inbound(request.form)

    # A Twilio manda form-urlencoded, não JSON. Se o corpo vier vazio aqui,
    # o provável é alguém ter apontado o webhook para o content-type errado.
    if not dados['from_raw']:
        log.warning("⚠️ Conversas: webhook sem campo From.")
        return _resposta_twiml()

    external_id = dados['message_sid'] or None

    # Idempotência: a Twilio reentrega em caso de timeout ou erro 5xx.
    if external_id:
        ja = mensagem_ja_processada(external_id)
        if ja is not None:
            log.info(f"↩️ Conversas: mensagem {external_id} já registrada, ignorada.")
            return _resposta_twiml()

    try:
        # A conversa vem primeiro: em corrida, a recuperação é um rollback,
        # que desfaria qualquer outra coisa pendente na mesma sessão.
        conversa, _criada = get_or_create_conversation(
            dados['from_raw'],
            contact_name=dados['profile_name'] or None,
        )
        if conversa is None:
            log.warning(f"⚠️ Conversas: telefone inutilizável em {dados['from_raw']!r}.")
            return _resposta_twiml()

        texto = dados['body']
        if not texto and dados['num_media']:
            # Anexos entram na Fase 3. Por ora registra que chegou algo.
            texto = f"[{dados['num_media']} anexo(s) recebido(s)]"

        msg = registrar_entrada(
            conversa, texto, external_id=external_id, source='twilio'
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        # Não logar o conteúdo da mensagem em produção.
        log.error(f"❌ Conversas: falha ao gravar mensagem recebida: {exc}")
        # 500 faz a Twilio reentregar, o que é o comportamento desejado.
        return jsonify({'error': 'Falha ao processar'}), 500

    avisar_conversa(conversa, extra={'nova_mensagem': True})
    avisar_conversa(conversa, evento=EVENTO_MENSAGEM,
                    extra={'message_id': msg.id})
    return _resposta_twiml()


@conversas_bp.route('/webhook/status', methods=['POST'])
def webhook_status():
    """
    Callback de status de entrega da Twilio.

    Atualiza whatsapp_messages.status de enviando para enviado, entregue,
    lido ou erro, conforme o ciclo de vida da mensagem.
    """
    if not twilio_client.auth_token():
        return jsonify({'error': 'Integração não configurada'}), 503

    assinatura = request.headers.get('X-Twilio-Signature', '')
    url = twilio_client.webhook_url(request)
    if not twilio_client.validate_signature(url, request.form.to_dict(), assinatura):
        log.warning("⚠️ Conversas: assinatura inválida no callback de status.")
        return jsonify({'error': 'Assinatura inválida'}), 403

    sid = request.form.get('MessageSid') or request.form.get('SmsSid') or ''
    novo = twilio_client.map_status(request.form.get('MessageStatus'))
    if not sid:
        return _resposta_twiml()

    try:
        msg = WhatsAppMessage.query.filter_by(external_message_id=sid).first()
        if msg is not None:
            msg.status = novo
            if novo == 'entregue' and not msg.delivered_at:
                msg.delivered_at = datetime.utcnow()
            db.session.commit()
            avisar_conversa(msg.conversation,
                            extra={'message_id': msg.id, 'status': novo})
    except Exception as exc:
        db.session.rollback()
        log.warning(f"⚠️ Conversas: falha ao atualizar status de {sid}: {exc}")

    return _resposta_twiml()


# ── API da tela ──────────────────────────────────────────────────────────────

@conversas_bp.route('/api/conversas')
@login_required
def listar():
    """Lista de conversas, mais recentes primeiro."""
    if not _staff_only():
        return _forbidden()

    status = (request.args.get('status') or '').strip()
    busca = (request.args.get('busca') or '').strip()

    q = Conversation.query
    if status == 'ativas':
        q = q.filter(Conversation.status.in_(CONVERSATION_ACTIVE_STATUSES))
    elif status in (CONVERSATION_STATUS_OPEN, CONVERSATION_STATUS_PENDING,
                    CONVERSATION_STATUS_RESOLVED):
        q = q.filter(Conversation.status == status)

    if busca:
        like = f'%{busca}%'
        q = q.filter(or_(
            Conversation.contact_phone.like(like),
            Conversation.contact_name.ilike(like),
        ))

    conversas = q.order_by(Conversation.last_activity_at.desc(),
                           Conversation.id.desc()).limit(200).all()
    if not conversas:
        return jsonify({'conversas': []})

    ids = [c.id for c in conversas]

    # Não lidas por conversa, numa consulta só. Agrupamento simples, sem
    # função exclusiva de dialeto.
    nao_lidas = dict(
        db.session.query(
            WhatsAppMessage.conversation_id, func.count(WhatsAppMessage.id)
        )
        .filter(WhatsAppMessage.conversation_id.in_(ids))
        .filter(WhatsAppMessage.direction == 'inbound')
        .filter(WhatsAppMessage.status != 'lido')
        .group_by(WhatsAppMessage.conversation_id)
        .all()
    )

    # Última mensagem de cada conversa, sem carregar a thread inteira.
    # max(id) por conversa é portável nos dois bancos.
    ultimos_ids = (
        db.session.query(func.max(WhatsAppMessage.id))
        .filter(WhatsAppMessage.conversation_id.in_(ids))
        .group_by(WhatsAppMessage.conversation_id)
    )
    ultimas = {
        m.conversation_id: m
        for m in WhatsAppMessage.query.filter(
            WhatsAppMessage.id.in_(ultimos_ids)
        ).all()
    }

    return jsonify({'conversas': [
        serializar_conversa(c, nao_lidas.get(c.id, 0), ultimas.get(c.id))
        for c in conversas
    ]})


@conversas_bp.route('/api/conversas/<int:conversa_id>')
@login_required
def detalhe(conversa_id):
    """Thread de uma conversa. Marca as recebidas como lidas."""
    if not _staff_only():
        return _forbidden()

    conversa = Conversation.query.get_or_404(conversa_id)
    mensagens = (WhatsAppMessage.query
                 .filter_by(conversation_id=conversa.id)
                 .order_by(WhatsAppMessage.sent_at.asc(),
                           WhatsAppMessage.id.asc())
                 .all())

    marcou = False
    for msg in mensagens:
        if msg.direction == 'inbound' and msg.status != 'lido':
            msg.status = 'lido'
            marcou = True
    if marcou:
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            log.warning(f"⚠️ Conversas: falha ao marcar como lidas: {exc}")

    driver = conversa.driver
    return jsonify({
        'conversa': serializar_conversa(conversa, 0,
                                        mensagens[-1] if mensagens else None),
        'mensagens': [serializar_mensagem(m) for m in mensagens],
        'motorista': {
            'id': driver.id,
            'nome': driver.name,
            'telefone': driver.phone,
            'cidade': driver.city,
            'uf': driver.state,
            'veiculo': driver.truck_type,
            'placa': driver.vehicle_plate,
            'validado': bool(driver.validated),
        } if driver else None,
        'canal_ativo': providers.canal_configurado(),
        'pode_enviar': providers.canal_configurado() is not None,
    })


@conversas_bp.route('/api/conversas/<int:conversa_id>/enviar', methods=['POST'])
@login_required
def enviar(conversa_id):
    """Envia uma mensagem pela Twilio e grava como saída."""
    if not _staff_only():
        return _forbidden()

    conversa = Conversation.query.get_or_404(conversa_id)

    corpo = request.get_json(silent=True) or {}
    texto = str(corpo.get('texto') or '').strip()
    if not texto or len(texto) > LIMITE_MENSAGEM:
        return jsonify({
            'error': f'A mensagem deve ter entre 1 e {LIMITE_MENSAGEM} caracteres.'
        }), 400

    if providers.canal_configurado() is None:
        return jsonify({
            'error': 'Nenhum canal de WhatsApp configurado. Configure a '
                     'Evolution ou a Twilio no ambiente.'
        }), 503

    # A atribuição é respeitada, mas a fila e o lock são a Fase 2.
    if (conversa.assigned_agent_id
            and conversa.assigned_agent_id != current_user.id
            and current_user.role != 'admin'):
        return jsonify({'error': 'Esta conversa está atribuída a outro atendente.'}), 409

    canal = None
    try:
        resultado = providers.enviar(conversa.contact_phone, texto)
        canal = resultado.get('provider')
        status = resultado.get('status') or 'enviado'
        external_id = resultado.get('external_id')
        erro = None
    except providers.EnvioError as exc:
        canal, status, external_id, erro = None, 'erro', None, str(exc)
        log.error(f"❌ Conversas: envio falhou na conversa {conversa.id}: {exc}")

    try:
        msg = registrar_saida(conversa, texto, autor_id=current_user.id,
                              external_id=external_id, status=status)
        # Responder é assumir. Sem isto o EMA continuaria respondendo em
        # paralelo ao atendente, na mesma conversa.
        if erro is None:
            assumir_conversa(conversa, current_user.id)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.error(f"❌ Conversas: falha ao gravar mensagem enviada: {exc}")
        return jsonify({'error': 'Mensagem enviada, mas não foi possível registrá-la.'}), 500

    avisar_conversa(conversa, extra={'message_id': msg.id})

    if erro:
        return jsonify({'error': erro, 'message_id': msg.id}), 502
    return jsonify({'ok': True, 'mensagem': serializar_mensagem(msg)})


@conversas_bp.route('/api/conversas/<int:conversa_id>/status', methods=['POST'])
@login_required
def mudar_status(conversa_id):
    """Resolve ou reabre uma conversa."""
    if not _staff_only():
        return _forbidden()

    conversa = Conversation.query.get_or_404(conversa_id)
    novo = str((request.get_json(silent=True) or {}).get('status') or '').strip()
    if novo not in (CONVERSATION_STATUS_OPEN, CONVERSATION_STATUS_PENDING,
                    CONVERSATION_STATUS_RESOLVED):
        return jsonify({'error': 'Status inválido.'}), 400

    conversa.status = novo
    conversa.resolved_at = (datetime.utcnow()
                            if novo == CONVERSATION_STATUS_RESOLVED else None)
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.error(f"❌ Conversas: falha ao mudar status: {exc}")
        return jsonify({'error': 'Não foi possível alterar o status.'}), 500

    avisar_conversa(conversa)
    return jsonify({'ok': True, 'status': novo})


# ── Diagnóstico ──────────────────────────────────────────────────────────────

@conversas_bp.route('/api/status')
@login_required
def status():
    """
    Diagnóstico: confirma que o schema da Central existe DE FATO no banco em
    uso, e se a Twilio está configurada.

    Existe por um motivo concreto. O auto-migrator de utils/migrations.py
    engole falhas de DDL em log e segue o boot, então uma coluna pode não ter
    sido criada em produção sem que nada quebre visivelmente. Além disso, se
    a conexão com o PostgreSQL falhar, a app cai para um SQLite local e
    continua de pé. Este endpoint responde às duas perguntas de uma vez.
    """
    if not _staff_only():
        return _forbidden()

    try:
        inspector = sa_inspect(db.engine)
        tabelas = set(inspector.get_table_names())

        colunas_wa = set()
        if 'whatsapp_messages' in tabelas:
            colunas_wa = {c['name'] for c in inspector.get_columns('whatsapp_messages')}

        indices = set()
        for tabela in ('whatsapp_messages', 'conversations'):
            if tabela in tabelas:
                indices |= {i['name'] for i in inspector.get_indexes(tabela)}

        tem_tabela = 'conversations' in tabelas
        tem_coluna = 'conversation_id' in colunas_wa
        schema_ok = tem_tabela and tem_coluna

        return jsonify({
            'success': True,
            'dialeto': db.engine.dialect.name,
            'schema_ok': schema_ok,
            'tabela_conversations': tem_tabela,
            'coluna_conversation_id': tem_coluna,
            'indice_conversation_id': 'idx_whatsapp_messages_conversation_id' in indices,
            'indice_conversa_ativa': 'uq_conversations_open_contact' in indices,
            'total_conversas': Conversation.query.count() if schema_ok else None,
            'mensagens_sem_conversa': (
                WhatsAppMessage.query
                .filter(WhatsAppMessage.conversation_id.is_(None))
                .count() if schema_ok else None
            ),
            'canais': providers.diagnostico(),
            # URLs exatas para colar no console da Twilio. Geradas a partir
            # da requisição real, então já refletem o domínio e o HTTPS que o
            # balanceador entrega, que é justamente o que a Twilio assina.
            'webhook_entrada': url_for('conversas.webhook', _external=True),
            'webhook_status': url_for('conversas.webhook_status', _external=True),
        })
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500
