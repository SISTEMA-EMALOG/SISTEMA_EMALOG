"""
Ganchos de sessão do SQLAlchemy para mensagens de WhatsApp (Fase 3).

Por que ganchos, e não edição em cada ponto que cria mensagem
--------------------------------------------------------------
Hoje nove pontos do sistema criam WhatsAppMessage: webhook do EMA, respostas
do bot, ofertas de frete, Contratação, execução de frete e a própria Central.
Só os da Central preenchiam conversation_id. Resultado: as respostas do bot e
as ofertas de frete não apareciam na conversa, e a tela só se atualizava em
tempo real para parte das mensagens.

Editar os nove pontos espalharia a mesma regra por cinco módulos e deixaria o
próximo ponto novo esquecido. Os ganchos valem para qualquer mensagem gravada
por qualquer caminho.

O que cada gancho faz
---------------------
before_flush: mensagem nova sem conversation_id é vinculada à conversa ATIVA
do contato, procurada pelo motorista e, na falta dele, pelo telefone. Nunca
cria conversa: uma oferta de frete enviada a cem motoristas não pode encher a
fila com cem conversas.

after_flush: anota quais conversas ganharam mensagem, com o estado de
atribuição lido dentro da transação.

after_commit: emite 'conversa_atualizada' para os atendentes. Só depois do
commit, e sem SQL, porque a sessão já não tem transação nesse ponto.

Regras de segurança
-------------------
Os ganchos rodam em TODO flush do sistema, então:
  - saem na hora quando não há mensagem nova na sessão;
  - nunca gravam na tabela conversations, só leem. Gravar ali de dentro do
    flush do EMA, que já segura a linha do motorista, inverteria a ordem de
    lock conversa, depois motorista, e geraria deadlock;
  - nunca propagam exceção: uma falha aqui não pode derrubar o envio de uma
    oferta de frete.
"""

import logging

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from infraestrutura_critica.models import (
    Conversation,
    User,
    WhatsAppMessage,
    CONVERSATION_ACTIVE_STATUSES,
)
from atendimento_conversas.utils.phone import normalize_contact_key

log = logging.getLogger(__name__)

CHAVE_PENDENTES = 'central_conversas_para_avisar'

_registrado = False


def _mensagens_novas(session):
    return [o for o in session.new if isinstance(o, WhatsAppMessage)]


def _vincular(session, flush_context, instances):
    novas = [m for m in _mensagens_novas(session) if m.conversation_id is None]
    if not novas:
        return
    try:
        with session.no_autoflush:
            conn = session.connection()

            driver_ids = {m.driver_id for m in novas if m.driver_id}
            por_driver = {}
            if driver_ids:
                linhas = conn.execute(
                    select(Conversation.driver_id, Conversation.id)
                    .where(Conversation.driver_id.in_(driver_ids),
                           Conversation.status.in_(CONVERSATION_ACTIVE_STATUSES))
                    .order_by(Conversation.id)
                ).all()
                por_driver = {d: c for d, c in linhas}

            chaves = {}
            for m in novas:
                if m.driver_id in por_driver:
                    continue
                chave = normalize_contact_key(m.phone_number)
                if chave:
                    chaves[id(m)] = chave
            por_chave = {}
            if chaves:
                linhas = conn.execute(
                    select(Conversation.contact_phone, Conversation.id)
                    .where(Conversation.contact_phone.in_(set(chaves.values())),
                           Conversation.status.in_(CONVERSATION_ACTIVE_STATUSES))
                    .order_by(Conversation.id)
                ).all()
                por_chave = {f: c for f, c in linhas}

            for m in novas:
                alvo = por_driver.get(m.driver_id) or por_chave.get(chaves.get(id(m)))
                if alvo:
                    m.conversation_id = alvo
    except Exception as exc:
        log.warning(f"⚠️ Conversas: falha ao vincular mensagem nova à conversa: {exc}")


def _anotar(session, flush_context):
    novas = [m for m in _mensagens_novas(session) if m.conversation_id]
    if not novas:
        return
    try:
        pendentes = session.info.setdefault(CHAVE_PENDENTES, {})
        entrada = {}
        for m in novas:
            entrada[m.conversation_id] = entrada.get(m.conversation_id, False) or \
                (m.direction == 'inbound')

        linhas = session.connection().execute(
            select(Conversation.id, Conversation.contact_phone, Conversation.status,
                   Conversation.driver_id, Conversation.handling_mode,
                   Conversation.assigned_agent_id, User.username)
            .outerjoin(User, User.id == Conversation.assigned_agent_id)
            .where(Conversation.id.in_(entrada.keys()))
        ).all()

        for cid, fone, status, driver_id, modo, dono, dono_nome in linhas:
            anterior = pendentes.get(cid, {})
            pendentes[cid] = {
                'conversation_id': cid,
                'contact_phone': fone,
                'status': status,
                'driver_id': driver_id,
                'handling_mode': modo,
                'assigned_agent_id': dono,
                'assigned_agent': dono_nome,
                'nova_mensagem': anterior.get('nova_mensagem', False) or entrada[cid],
                'acao': 'mensagem',
            }
    except Exception as exc:
        log.warning(f"⚠️ Conversas: falha ao anotar aviso de mensagem: {exc}")


def _avisar(session):
    pendentes = session.info.pop(CHAVE_PENDENTES, None)
    if not pendentes:
        return
    try:
        from infraestrutura_critica.app import socketio
        from atendimento_conversas.utils.conversas_service import (
            EVENTO_CONVERSA, SALA_OPERADORES,
        )
        for payload in pendentes.values():
            socketio.emit(EVENTO_CONVERSA, payload, room=SALA_OPERADORES)
    except Exception as exc:
        log.warning(f"⚠️ Conversas: falha ao emitir aviso de mensagem: {exc}")


def _descartar(session, *args):
    session.info.pop(CHAVE_PENDENTES, None)


def registrar():
    """Liga os ganchos uma única vez por processo."""
    global _registrado
    if _registrado:
        return
    event.listen(Session, 'before_flush', _vincular)
    event.listen(Session, 'after_flush', _anotar)
    event.listen(Session, 'after_commit', _avisar)
    event.listen(Session, 'after_rollback', _descartar)
    _registrado = True
