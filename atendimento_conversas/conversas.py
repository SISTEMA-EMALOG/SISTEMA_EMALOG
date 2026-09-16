"""
Central de Atendimento — blueprint do módulo.

Fase 0 (modelagem de dados) entrega aqui apenas o diagnóstico de schema.
Webhook da Twilio, envio e tela de conversas entram na Fase 1.

Regra de arquitetura: este módulo só tem lógica de atendimento. Núcleo,
modelos e utilitários compartilhados vêm de infraestrutura_critica/.
"""

import os

from flask import Blueprint, jsonify
from flask_login import current_user, login_required
from sqlalchemy import inspect as sa_inspect

from infraestrutura_critica.app import db
from infraestrutura_critica.models import Conversation, WhatsAppMessage

conversas_bp = Blueprint('conversas', __name__, url_prefix='/conversas')


def _staff_only():
    return current_user.role in ('admin', 'operador')


@conversas_bp.route('/api/status')
@login_required
def status():
    """
    Diagnóstico da Fase 0: confirma que o schema da Central de Atendimento
    existe DE FATO no banco que a app está usando.

    Existe por um motivo concreto. O auto-migrator de utils/migrations.py
    engole falhas de DDL em log e segue o boot, então uma coluna pode não ter
    sido criada em produção sem que nada quebre visivelmente. Além disso, se a
    conexão com o PostgreSQL falhar, a app cai para um SQLite local e continua
    de pé. Este endpoint responde às duas perguntas de uma vez: qual banco está
    em uso, e a migração rodou mesmo nele.
    """
    if not _staff_only():
        return jsonify({'success': False, 'error': 'Acesso negado'}), 403

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
            'twilio_configurado': bool(os.environ.get('TWILIO_ACCOUNT_SID')),
        })
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500
