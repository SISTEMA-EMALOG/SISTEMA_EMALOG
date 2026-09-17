"""
Regras da Central de Atendimento: abrir conversa, gravar mensagem, avisar a tela.

Fica separado das rotas pelo mesmo motivo de
kanban_contratacao/utils/contracting_service.py: o webhook, a tela e o backfill
precisam das mesmas regras, e duplicá-las é como as duas normalizações de
telefone deste projeto passaram a divergir.

Atribuição, transferência e resolução ficam em utils/fila.py, que decide as
disputas no banco.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from infraestrutura_critica.app import db, socketio
from infraestrutura_critica.models import (
    Conversation,
    User,
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

# Mensagens enviadas antes de a conversa existir, e que entram nela quando o
# motorista responde. Ver _vincular_saidas_recentes.
DIAS_CONTEXTO_SAIDA = 7

# Voltas de conversa_para_entrada. Cada volta só se repete se a conversa foi
# resolvida entre a leitura e a gravação, o que exige um clique humano no
# mesmo instante; três é folga larga.
TENTATIVAS_ENTRADA = 3


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

    Para anexar mensagem recebida, use conversa_para_entrada, que além disso
    garante que a conversa continua ativa no momento do commit.
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
        # Se um humano já tinha tomado este motorista, a conversa nova nasce
        # com o bot calado. O dono vem do cadastro; resolver limpa esse dono,
        # então normalmente a conversa nova cai livre na fila.
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

    if conversa.driver_id:
        _vincular_saidas_recentes(conversa.id, conversa.driver_id)

    return conversa, True


def _vincular_saidas_recentes(conversa_id, driver_id):
    """
    Traz para a conversa recém-aberta as mensagens enviadas ao motorista nos
    últimos dias que ainda não tinham conversa.

    Caso típico: uma oferta de frete sai para cem motoristas, sem conversa,
    para não encher a fila. Um deles responde e abre a conversa. Sem isto, o
    atendente veria a resposta sem saber a que oferta ela se refere.

    Dentro de SAVEPOINT: no PostgreSQL, um comando que falha aborta a
    transação inteira, e uma falha aqui não pode perder a mensagem recebida.
    Só toca whatsapp_messages, que não entra na ordem de lock da fila.
    """
    limite = datetime.utcnow() - timedelta(days=DIAS_CONTEXTO_SAIDA)
    try:
        with db.session.begin_nested():
            db.session.execute(
                update(WhatsAppMessage)
                .where(WhatsAppMessage.conversation_id.is_(None),
                       WhatsAppMessage.driver_id == driver_id,
                       WhatsAppMessage.direction == 'outbound',
                       WhatsAppMessage.sent_at >= limite)
                .values(conversation_id=conversa_id)
                .execution_options(synchronize_session=False)
            )
    except Exception as exc:
        log.warning(f"⚠️ Conversas: falha ao vincular mensagens anteriores: {exc}")


def _tocar(conversa_id, quando):
    """
    Atualiza last_activity_at SÓ se a conversa continua ativa.

    UPDATE condicional: trava a linha até o commit de quem chamou. Devolve o
    número de linhas afetadas; 0 significa que a conversa foi resolvida.
    Nunca reabre conversa: reabrir é ação explícita, em fila.reabrir.
    """
    stmt = (
        update(Conversation)
        .where(Conversation.id == conversa_id,
               Conversation.status.in_(CONVERSATION_ACTIVE_STATUSES))
        .values(last_activity_at=quando)
        .execution_options(synchronize_session=False)
    )
    return db.session.execute(stmt).rowcount


def conversa_para_entrada(telefone_bruto, contact_name=None, driver=None):
    """
    Conversa ATIVA para anexar uma mensagem recebida. Devolve (conversa, criada).

    Resolve uma corrida real. O webhook lê a conversa como ativa; no mesmo
    instante um atendente a resolve; o webhook grava a mensagem nela. A
    mensagem nova do contato fica presa numa conversa fechada e some da fila.

    Aqui a última atividade é gravada por UPDATE condicional em status ativo.
    Se não casar, a conversa foi resolvida no meio do caminho e a próxima
    volta abre outra. Casando, a linha fica travada até o commit de quem
    chamou, então a conversa continua ativa quando a mensagem for gravada.

    O commit é de quem chama, e deve vir logo em seguida, sem chamada
    externa no meio.
    """
    for _ in range(TENTATIVAS_ENTRADA):
        conversa, criada = get_or_create_conversation(
            telefone_bruto, contact_name=contact_name, driver=driver
        )
        if conversa is None:
            return None, False
        if _tocar(conversa.id, datetime.utcnow()) == 1:
            db.session.refresh(conversa)
            return conversa, criada
        log.info(f"↪️ Conversas: conversa {conversa.id} resolvida durante a entrada; abrindo outra.")
        db.session.expire(conversa)

    log.error("❌ Conversas: não foi possível obter conversa ativa para a mensagem recebida.")
    return None, False


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
    """
    Grava uma mensagem recebida e devolve a linha.

    Espera uma conversa obtida por conversa_para_entrada, que já gravou a
    atividade e garantiu que a conversa está ativa.
    """
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
    return msg


def registrar_saida(conversa_id, contact_phone, driver_id, texto, autor_id=None,
                    external_id=None, status='enviado', source='operator'):
    """
    Grava uma mensagem enviada e devolve a linha.

    Recebe valores simples em vez do objeto da conversa: é chamada depois do
    envio HTTP, e o envio acontece com a sessão já commitada. Reusar o objeto
    carregado antes faria uma leitura extra só para descobrir o telefone.
    """
    agora = datetime.utcnow()
    msg = WhatsAppMessage(
        conversation_id=conversa_id,
        driver_id=driver_id,
        phone_number=(contact_phone or '')[:20],
        message_content=texto or '',
        direction='outbound',
        source=source,
        status=status,
        created_by=autor_id,
        external_message_id=external_id or None,
        sent_at=agora,
    )
    db.session.add(msg)
    _tocar(conversa_id, agora)
    return msg


# ── Serialização para a tela ────────────────────────────────────────────────

def _iso(valor):
    return valor.isoformat() if valor else None


def serializar_conversa(conversa, nao_lidas=None, ultima=None):
    """Resumo de uma conversa para a lista e o cabeçalho da tela."""
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
        'assigned_at': _iso(conversa.assigned_at),
        'last_activity_at': _iso(conversa.last_activity_at),
        'resolved_at': _iso(conversa.resolved_at),
        'ultima_mensagem': (ultima.message_content if ultima else ''),
        'nao_lidas': nao_lidas if nao_lidas is not None else 0,
    }


def serializar_mensagem(msg, anexos=None):
    """Uma mensagem para a thread."""
    return {
        'id': msg.id,
        'direction': msg.direction,
        'texto': msg.message_content,
        'status': msg.status,
        'source': msg.source,
        'autor_id': msg.created_by,
        'timestamp': _iso(msg.sent_at),
        'lida': msg.read_at is not None if msg.direction == 'inbound' else None,
        'anexos': anexos or [],
    }


# ── Leitura ──────────────────────────────────────────────────────────────────

def marcar_lidas(conversa_id, ids_exibidos):
    """
    Marca como lidas as mensagens recebidas que a tela EXIBIU. Sem commit.

    Por lista de ids, e não "tudo até agora": uma mensagem que chegou durante
    o carregamento da tela não foi vista, e não pode ser marcada.

    Grava read_at, nunca status. Ver models.WhatsAppMessage.read_at.
    """
    if not ids_exibidos:
        return 0
    return db.session.execute(
        update(WhatsAppMessage)
        .where(WhatsAppMessage.id.in_(list(ids_exibidos)),
               WhatsAppMessage.conversation_id == conversa_id,
               WhatsAppMessage.direction == 'inbound',
               WhatsAppMessage.read_at.is_(None))
        .values(read_at=datetime.utcnow())
        .execution_options(synchronize_session=False)
    ).rowcount


# ── Tempo real ───────────────────────────────────────────────────────────────

def avisar_conversa(conversa, evento=EVENTO_CONVERSA, extra=None):
    """
    Avisa os atendentes que a conversa mudou. SEMPRE depois do commit.

    O evento só notifica; quem decide é o banco. Uma tela que perdeu o evento
    continua segura, porque qualquer clique em cima de estado velho recebe 409
    do servidor.

    Manda identificadores e o estado de atribuição, nunca o texto da mensagem.
    O toast global do sistema interpola sem escapar, conteúdo de WhatsApp vem
    de terceiro não autenticado, e a sala 'operators' inclui quem não abriu a
    conversa.
    """
    if conversa is None:
        return
    dono_id = conversa.assigned_agent_id
    dono = db.session.get(User, dono_id) if dono_id else None
    payload = {
        'conversation_id': conversa.id,
        'contact_phone': conversa.contact_phone,
        'status': conversa.status,
        'driver_id': conversa.driver_id,
        'handling_mode': conversa.handling_mode,
        'assigned_agent_id': dono_id,
        'assigned_agent': dono.username if dono else None,
        'last_activity_at': _iso(conversa.last_activity_at),
    }
    if extra:
        payload.update(extra)
    try:
        socketio.emit(evento, payload, room=SALA_OPERADORES)
    except Exception as exc:
        # Falha de tempo real nunca pode derrubar a operação já commitada.
        log.warning(f"⚠️ Conversas: falha ao emitir {evento}: {exc}")
