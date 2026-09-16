"""
Regras da Central de Atendimento: abrir conversa, gravar mensagem, avisar a tela.

Fica separado das rotas pelo mesmo motivo de
kanban_contratacao/utils/contracting_service.py: o webhook, a tela e o backfill
precisam das mesmas regras, e duplicá-las é como as duas normalizações de
telefone deste projeto passaram a divergir.
"""

import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from infraestrutura_critica.app import db, socketio
from infraestrutura_critica.models import (
    Conversation,
    WhatsAppMessage,
    CONVERSATION_ACTIVE_STATUSES,
    CONVERSATION_STATUS_OPEN,
)
from atendimento_conversas.utils.phone import (
    normalize_contact_key,
    find_driver_by_phone,
)

log = logging.getLogger(__name__)

# Sala em que admin, operador e vendedor entram no login.
# Ver infraestrutura_critica/app.py, handler de 'join_notifications'.
SALA_OPERADORES = 'operators'

EVENTO_CONVERSA = 'conversa_atualizada'
EVENTO_MENSAGEM = 'conversa_mensagem'


# ── Conversa ─────────────────────────────────────────────────────────────────

def conversa_ativa(chave):
    """A conversa não-resolvida de um contato, ou None."""
    return (
        Conversation.query
        .filter(Conversation.contact_phone == chave)
        .filter(Conversation.status.in_(CONVERSATION_ACTIVE_STATUSES))
        .order_by(Conversation.id.desc())
        .first()
    )


def get_or_create_conversation(telefone_bruto, contact_name=None, driver=None):
    """
    Devolve (conversa, criada) para um contato, abrindo uma se não houver ativa.

    CHAME ANTES de adicionar qualquer outra coisa à sessão. Em corrida, o
    índice parcial único uq_conversations_open_contact faz o segundo INSERT
    falhar, e a recuperação é um rollback — que desfaria também o que já
    estivesse pendente na mesma sessão.

    A corrida é real: a Twilio reentrega webhook, e duas entregas do mesmo
    contato podem chegar juntas. Conferir em Python antes do INSERT não
    basta, porque as duas conferências passam antes de qualquer gravação.
    """
    chave = normalize_contact_key(telefone_bruto)
    if not chave:
        return None, False

    conversa = conversa_ativa(chave)
    if conversa is not None:
        return conversa, False

    if driver is None:
        try:
            driver = find_driver_by_phone(chave)
        except Exception as exc:
            log.warning(f"⚠️ Conversas: falha ao buscar motorista: {exc}")
            driver = None

    conversa = Conversation(
        contact_phone=chave,
        contact_name=(contact_name or (driver.name if driver else None) or None),
        driver_id=driver.id if driver else None,
        status=CONVERSATION_STATUS_OPEN,
        handling_mode=(driver.whatsapp_mode if driver else None) or 'auto',
        assigned_agent_id=driver.whatsapp_assigned_to if driver else None,
        last_activity_at=datetime.utcnow(),
    )
    db.session.add(conversa)
    try:
        db.session.flush()
    except IntegrityError:
        # Outra requisição abriu a conversa entre a consulta e o INSERT.
        db.session.rollback()
        conversa = conversa_ativa(chave)
        if conversa is None:
            log.error(f"⚠️ Conversas: conflito ao abrir conversa de {chave} sem vencedor.")
            return None, False
        return conversa, False

    return conversa, True


def assumir_conversa(conversa, usuario_id):
    """
    Passa a conversa para atendimento humano e CALA O BOT.

    Sem isto, responder pela Central enquanto o motorista está em modo
    automático faz o EMA responder também: duas vozes na mesma conversa,
    para o mesmo motorista, sem ninguém perceber.

    O EMA decide se fala lendo Driver.whatsapp_mode, não a conversa. Por
    isso o modo é espelhado no cadastro do motorista, que é onde ele olha.
    Ver prospeccao_captacao_motorista/ema_agent.py, no ponto em que checa
    whatsapp_mode == 'manual' e marca a mensagem como processada sem
    responder.

    A atribuição definitiva, com disputa entre atendentes, é da Fase 2.
    Aqui só se garante que quem respondeu primeiro fica como responsável.
    """
    if conversa is None:
        return

    conversa.handling_mode = 'manual'
    if conversa.assigned_agent_id is None:
        conversa.assigned_agent_id = usuario_id
        conversa.assigned_at = datetime.utcnow()

    driver = conversa.driver
    if driver is not None:
        driver.whatsapp_mode = 'manual'
        if driver.whatsapp_assigned_to is None:
            driver.whatsapp_assigned_to = conversa.assigned_agent_id


def registrar_atividade(conversa, quando=None):
    """Atualiza o carimbo de atividade e reabre a conversa se estava resolvida."""
    if conversa is None:
        return
    quando = quando or datetime.utcnow()
    if conversa.last_activity_at is None or quando > conversa.last_activity_at:
        conversa.last_activity_at = quando
    if conversa.status not in CONVERSATION_ACTIVE_STATUSES:
        conversa.status = CONVERSATION_STATUS_OPEN
        conversa.resolved_at = None


# ── Mensagens ────────────────────────────────────────────────────────────────

def mensagem_ja_processada(external_id):
    """
    A mensagem do provedor já existe? Devolve a linha ou None.

    A unicidade de external_message_id é garantida no banco: unique=True no
    modelo cobre o SQLite via create_all, e o índice parcial
    uq_whatsapp_external_message cobre o PostgreSQL.
    """
    if not external_id:
        return None
    return WhatsAppMessage.query.filter_by(external_message_id=external_id).first()


def registrar_entrada(conversa, texto, external_id=None, quando=None,
                      source='twilio'):
    """Grava uma mensagem recebida e devolve a linha."""
    msg = WhatsAppMessage(
        conversation_id=conversa.id,
        driver_id=conversa.driver_id,
        phone_number=conversa.contact_phone[:20],
        message_content=texto or '',
        direction='inbound',
        source=source,
        status='recebido',
        external_message_id=external_id or None,
        sent_at=quando or datetime.utcnow(),
    )
    db.session.add(msg)
    registrar_atividade(conversa, msg.sent_at)
    return msg


def registrar_saida(conversa, texto, autor_id=None, external_id=None,
                    status='enviado', source='operator'):
    """Grava uma mensagem enviada e devolve a linha."""
    msg = WhatsAppMessage(
        conversation_id=conversa.id,
        driver_id=conversa.driver_id,
        phone_number=conversa.contact_phone[:20],
        message_content=texto or '',
        direction='outbound',
        source=source,
        status=status,
        created_by=autor_id,
        external_message_id=external_id or None,
        sent_at=datetime.utcnow(),
    )
    db.session.add(msg)
    registrar_atividade(conversa, msg.sent_at)
    return msg


# ── Serialização para a tela ────────────────────────────────────────────────

def _iso(valor):
    return valor.isoformat() if valor else None


def serializar_conversa(conversa, nao_lidas=None, ultima=None):
    """Resumo de uma conversa para a lista da tela."""
    driver = conversa.driver
    nome = conversa.contact_name or (driver.name if driver else None)
    return {
        'id': conversa.id,
        'contact_phone': conversa.contact_phone,
        'nome': nome or conversa.contact_phone,
        'inicial': (nome or '?')[0].upper(),
        'driver_id': conversa.driver_id,
        'status': conversa.status,
        'handling_mode': conversa.handling_mode,
        'assigned_agent_id': conversa.assigned_agent_id,
        'assigned_agent': (conversa.assigned_agent.username
                           if conversa.assigned_agent else None),
        'last_activity_at': _iso(conversa.last_activity_at),
        'ultima_mensagem': (ultima.message_content if ultima else ''),
        'nao_lidas': nao_lidas if nao_lidas is not None else 0,
    }


def serializar_mensagem(msg):
    """Uma mensagem para a thread."""
    return {
        'id': msg.id,
        'direction': msg.direction,
        'texto': msg.message_content,
        'status': msg.status,
        'source': msg.source,
        'autor_id': msg.created_by,
        'timestamp': _iso(msg.sent_at),
    }


# ── Tempo real ───────────────────────────────────────────────────────────────

def avisar_conversa(conversa, evento=EVENTO_CONVERSA, extra=None):
    """
    Avisa os atendentes que a conversa mudou.

    Manda só identificadores, nunca o texto da mensagem. Dois motivos: o
    toast global do sistema interpola sem escapar, e conteúdo de WhatsApp vem
    de terceiro não autenticado; e a sala 'operators' inclui gente que ainda
    não abriu aquela conversa.
    """
    if conversa is None:
        return
    payload = {
        'conversation_id': conversa.id,
        'contact_phone': conversa.contact_phone,
        'status': conversa.status,
        'driver_id': conversa.driver_id,
    }
    if extra:
        payload.update(extra)
    try:
        socketio.emit(evento, payload, room=SALA_OPERADORES)
    except Exception as exc:
        # Falha de tempo real nunca pode derrubar a gravação da mensagem.
        log.warning(f"⚠️ Conversas: falha ao emitir {evento}: {exc}")
