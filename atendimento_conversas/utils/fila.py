"""
Fila compartilhada da Central de Atendimento (Fase 2).

Assumir, transferir, liberar, devolver ao bot, escalar para humano, resolver
e reabrir. Cada operação é decidida no BANCO, nunca no socket.

Como a disputa é decidida
-------------------------
Toda operação é um UPDATE condicional sobre a linha da conversa, e o número
de linhas afetadas diz quem venceu. Exemplo, para assumir:

    UPDATE conversations
       SET assigned_agent_id = :eu, assigned_at = :agora, handling_mode = 'manual'
     WHERE id = :id
       AND assigned_agent_id IS NULL
       AND status IN ('aberta', 'pendente')

Dois atendentes clicando juntos:

  PostgreSQL, em READ COMMITTED: o segundo UPDATE espera o lock de linha do
  primeiro. Quando o primeiro commita, o PostgreSQL reavalia o WHERE sobre a
  versão nova da linha, vê assigned_agent_id preenchido e afeta 0 linhas.

  SQLite: só um escritor por vez. O segundo UPDATE roda depois do commit do
  primeiro e também afeta 0 linhas.

Não se usa SELECT ... FOR UPDATE: no SQLite ele é ignorado, e o
comportamento de desenvolvimento divergiria do de produção. O SQL sai do
SQLAlchemy Core, portável nos dois bancos.

Regras de transação que NÃO podem ser quebradas
-----------------------------------------------
1. Ordem de lock fixa: primeiro a linha da conversa, depois a do motorista.
   O webhook do EMA também segue essa ordem ao escalar para humano. Invertida
   em um só lugar, dois pedidos simultâneos travam um ao outro e o
   PostgreSQL aborta um deles com deadlock.

2. Commit antes de qualquer ponto de espera externo: chamada HTTP, socket.
   A emissão de socket acontece depois do commit, em quem chama.

3. Quem chama não deve reaproveitar objetos carregados antes da operação:
   o UPDATE condicional não atualiza o mapa de identidade da sessão. Cada
   operação recarrega a conversa com populate_existing.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError, OperationalError

from infraestrutura_critica.app import db
from infraestrutura_critica.models import (
    Conversation,
    ConversationEvent,
    User,
    CONVERSATION_ACTIVE_STATUSES,
    CONVERSATION_STATUS_OPEN,
    CONVERSATION_STATUS_RESOLVED,
)

log = logging.getLogger(__name__)

PAPEIS_ATENDENTE = ('admin', 'operador', 'vendedor')

MODO_BOT = 'auto'
MODO_HUMANO = 'manual'

# Tentativas diante de erro transitório de concorrência. Ver _transitorio.
TENTATIVAS_BANCO_OCUPADO = 3

# Contadores de retentativa, por processo. Existem para que uma inversão de
# ordem de lock introduzida no futuro não passe em silêncio: toda repetição
# por deadlock também vai para o log como aviso.
ESTATISTICAS = {'deadlock': 0, 'serializacao': 0, 'sqlite_ocupado': 0}

# SQLSTATE do PostgreSQL para transação abortada por concorrência. A
# recomendação do próprio PostgreSQL para ambos é repetir a transação.
_PG_DEADLOCK = '40P01'
_PG_SERIALIZACAO = '40001'


@dataclass
class Resultado:
    ok: bool
    codigo: str            # ok | ja_era_sua | conflito | nao_encontrada | proibido | invalido
    mensagem: str = ''
    conversa: object = None
    dados: dict = field(default_factory=dict)

    @property
    def http(self):
        return {
            'ok': 200, 'ja_era_sua': 200, 'conflito': 409,
            'nao_encontrada': 404, 'proibido': 403, 'invalido': 400,
        }.get(self.codigo, 500)


# ── Primitivas ───────────────────────────────────────────────────────────────

def _agora():
    return datetime.utcnow()


def _cas(conversa_id, condicoes, valores):
    """
    UPDATE condicional na conversa. Devolve o número de linhas afetadas.

    Emitido na hora, sem esperar o flush da sessão, para que a linha da
    conversa seja sempre a PRIMEIRA a ser travada na transação.
    """
    stmt = (
        update(Conversation)
        .where(Conversation.id == conversa_id, *condicoes)
        .values(**valores)
        .execution_options(synchronize_session=False)
    )
    return db.session.execute(stmt).rowcount


def _recarregar(conversa_id):
    """Lê a conversa do banco, descartando o que houver no mapa de identidade."""
    return db.session.get(Conversation, conversa_id, populate_existing=True)


def _retrato(conversa):
    """
    Valores simples da conversa, lidos ANTES do commit.

    Depois do commit todo objeto da sessão expira, e ler um atributo abre
    transação nova. Quem vai fazer chamada HTTP em seguida usa este retrato.
    """
    return {
        'contact_phone': conversa.contact_phone,
        'driver_id': conversa.driver_id,
        'assigned_agent_id': conversa.assigned_agent_id,
        'status': conversa.status,
    }


def _espelhar_driver(conversa):
    """
    Copia modo e dono da conversa para o cadastro do motorista.

    O EMA e o agendador de inatividade decidem se falam lendo
    Driver.whatsapp_mode. A conversa é a autoridade; o motorista segue.
    Roda depois do UPDATE da conversa, na mesma transação, respeitando a
    ordem de lock conversa, depois motorista.
    """
    driver = conversa.driver
    if driver is None:
        return
    driver.whatsapp_mode = conversa.handling_mode or MODO_BOT
    driver.whatsapp_assigned_to = conversa.assigned_agent_id


def _evento(conversa_id, acao, ator_id=None, de=None, para=None):
    db.session.add(ConversationEvent(
        conversation_id=conversa_id, acao=acao, ator_id=ator_id,
        de_agente_id=de, para_agente_id=para, created_at=_agora(),
    ))


def _nome(usuario_id):
    if not usuario_id:
        return None
    u = db.session.get(User, usuario_id)
    return u.username if u else f'#{usuario_id}'


def _transitorio(exc):
    """
    Classifica o erro pelo CÓDIGO do banco, nunca por palavra na mensagem.

    Uma versão anterior procurava "locked" no texto e classificava deadlock
    do PostgreSQL como banco ocupado do SQLite, porque a mensagem de deadlock
    contém "blocked by process". O deadlock era repetido em silêncio.
    """
    orig = getattr(exc, 'orig', None)
    pgcode = getattr(orig, 'pgcode', None)
    if pgcode == _PG_DEADLOCK:
        return 'deadlock'
    if pgcode == _PG_SERIALIZACAO:
        return 'serializacao'
    if orig is not None and type(orig).__module__.startswith('sqlite3') \
            and 'database is locked' in str(orig).lower():
        return 'sqlite_ocupado'
    return None


def _executar(operacao):
    """
    Roda a operação com commit, repetindo só erro transitório de concorrência.

    Qualquer erro desfaz a transação inteira: conversa, espelho e histórico
    entram juntos ou não entram.

    Com a ordem de lock desta fila respeitada, deadlock entre operações da
    Central não deveria acontecer. Se acontecer, a operação é repetida, porque
    é o remédio correto para a vítima, mas o aviso no log denuncia que alguém
    inverteu a ordem de lock em algum ponto.
    """
    ultimo = None
    for _ in range(TENTATIVAS_BANCO_OCUPADO):
        try:
            resultado = operacao()
            if resultado.ok and resultado.codigo == 'ok':
                db.session.commit()
            else:
                db.session.rollback()
            return resultado
        except OperationalError as exc:
            db.session.rollback()
            tipo = _transitorio(exc)
            if tipo is None:
                raise
            ESTATISTICAS[tipo] += 1
            ultimo = exc
            if tipo == 'deadlock':
                log.warning("⚠️ Fila: deadlock detectado e operação repetida. "
                            "Ordem de lock conversa, depois motorista, violada em algum ponto.")
        except Exception:
            db.session.rollback()
            raise
    log.warning(f"⚠️ Fila: concorrência persistente após {TENTATIVAS_BANCO_OCUPADO} tentativas: {ultimo}")
    return Resultado(False, 'conflito', 'O sistema está ocupado. Tente de novo.')


def _explicar_conflito(conversa, ator_id, acao):
    """Recarrega e diz por que a operação não pôde ser feita."""
    if conversa is None:
        return Resultado(False, 'nao_encontrada', 'Conversa não encontrada.')
    if conversa.status not in CONVERSATION_ACTIVE_STATUSES and acao != 'reabrir':
        return Resultado(False, 'conflito', 'Esta conversa já foi resolvida.', conversa)
    dono = conversa.assigned_agent_id
    if dono and dono != ator_id:
        return Resultado(False, 'conflito',
                         f'{_nome(dono)} já está com esta conversa.', conversa)
    if dono is None and acao in ('transferir', 'liberar'):
        return Resultado(False, 'conflito',
                         'A conversa foi liberada por outra pessoa. Atualize a tela.', conversa)
    return Resultado(False, 'conflito',
                     'A conversa mudou enquanto você agia. Atualize a tela.', conversa)


def _eh_admin(usuario):
    return getattr(usuario, 'role', None) == 'admin'


def _condicao_dono(ator, de_esperado):
    """
    Quem pode agir sobre uma conversa já atribuída.

    Atendente: só sobre a própria. Admin: sobre qualquer uma, mas precisa
    dizer quem ele viu como dono. Se o dono mudou desde então, o UPDATE não
    casa e o admin recebe conflito em vez de atropelar uma transferência.
    """
    if _eh_admin(ator):
        if de_esperado is None:
            return Conversation.assigned_agent_id.is_(None)
        return Conversation.assigned_agent_id == de_esperado
    return Conversation.assigned_agent_id == ator.id


# ── Operações ────────────────────────────────────────────────────────────────

def assumir(conversa_id, ator):
    """Pega uma conversa livre. Idempotente para quem já é o dono."""
    ator_id = ator.id

    def op():
        agora = _agora()
        n = _cas(conversa_id,
                 [Conversation.assigned_agent_id.is_(None),
                  Conversation.status.in_(CONVERSATION_ACTIVE_STATUSES)],
                 dict(assigned_agent_id=ator_id, assigned_at=agora,
                      handling_mode=MODO_HUMANO))
        conversa = _recarregar(conversa_id)
        if n == 1:
            _espelhar_driver(conversa)
            _evento(conversa_id, 'assumiu', ator_id=ator_id, para=ator_id)
            return Resultado(True, 'ok', 'Conversa assumida.', conversa, _retrato(conversa))
        if conversa is not None and conversa.assigned_agent_id == ator_id \
                and conversa.status in CONVERSATION_ACTIVE_STATUSES:
            return Resultado(True, 'ja_era_sua', 'A conversa já é sua.', conversa,
                             _retrato(conversa))
        return _explicar_conflito(conversa, ator_id, 'assumir')

    return _executar(op)


def transferir(conversa_id, ator, para_id, de_esperado=None):
    """Passa a conversa para outro atendente ativo."""
    ator_id = ator.id

    try:
        para_id = int(para_id)
    except (TypeError, ValueError):
        return Resultado(False, 'invalido', 'Escolha para quem transferir.')

    destino = db.session.get(User, para_id)
    if destino is None or not destino.active or destino.role not in PAPEIS_ATENDENTE:
        return Resultado(False, 'invalido', 'O destino precisa ser um atendente ativo.')

    if not _eh_admin(ator):
        de_esperado = ator_id
    if de_esperado is not None and int(de_esperado) == para_id:
        return Resultado(False, 'invalido', 'A conversa já está com essa pessoa.')

    def op():
        n = _cas(conversa_id,
                 [_condicao_dono(ator, de_esperado),
                  Conversation.status.in_(CONVERSATION_ACTIVE_STATUSES)],
                 dict(assigned_agent_id=para_id, assigned_at=_agora(),
                      handling_mode=MODO_HUMANO))
        conversa = _recarregar(conversa_id)
        if n == 1:
            _espelhar_driver(conversa)
            _evento(conversa_id, 'transferiu', ator_id=ator_id,
                    de=de_esperado, para=para_id)
            return Resultado(True, 'ok', f'Transferida para {destino.username}.', conversa)
        if conversa is not None and conversa.assigned_agent_id \
                and conversa.assigned_agent_id != ator_id and not _eh_admin(ator):
            return Resultado(False, 'proibido',
                             'Só o responsável ou um administrador pode transferir.', conversa)
        return _explicar_conflito(conversa, ator_id, 'transferir')

    return _executar(op)


def liberar(conversa_id, ator, de_esperado=None):
    """Devolve a conversa para a fila humana, sem dono. O bot continua calado."""
    ator_id = ator.id
    if not _eh_admin(ator):
        de_esperado = ator_id
    if de_esperado is None:
        return Resultado(False, 'invalido', 'A conversa já está livre.')

    def op():
        n = _cas(conversa_id,
                 [_condicao_dono(ator, de_esperado),
                  Conversation.status.in_(CONVERSATION_ACTIVE_STATUSES)],
                 dict(assigned_agent_id=None, assigned_at=None,
                      handling_mode=MODO_HUMANO))
        conversa = _recarregar(conversa_id)
        if n == 1:
            _espelhar_driver(conversa)
            _evento(conversa_id, 'liberou', ator_id=ator_id, de=de_esperado)
            return Resultado(True, 'ok', 'Conversa devolvida para a fila.', conversa)
        return _explicar_conflito(conversa, ator_id, 'liberar')

    return _executar(op)


def devolver_ao_bot(conversa_id, ator, de_esperado=None):
    """
    Devolve a conversa ao EMA.

    Só faz sentido com motorista vinculado: é para motoristas que o EMA
    conduz cadastro e oferta de frete. Sem motorista, ninguém responderia.
    """
    ator_id = ator.id

    conversa_atual = _recarregar(conversa_id)
    if conversa_atual is None:
        return Resultado(False, 'nao_encontrada', 'Conversa não encontrada.')
    if conversa_atual.driver_id is None:
        return Resultado(False, 'invalido',
                         'Este contato não é um motorista cadastrado. Não há bot para assumir.',
                         conversa_atual)

    if _eh_admin(ator):
        condicao = (Conversation.assigned_agent_id.is_(None) if de_esperado is None
                    else Conversation.assigned_agent_id == de_esperado)
    else:
        # Atendente devolve a própria conversa ou uma livre.
        condicao = or_(Conversation.assigned_agent_id == ator_id,
                       Conversation.assigned_agent_id.is_(None))

    def op():
        n = _cas(conversa_id,
                 [condicao, Conversation.status.in_(CONVERSATION_ACTIVE_STATUSES)],
                 dict(assigned_agent_id=None, assigned_at=None, handling_mode=MODO_BOT))
        conversa = _recarregar(conversa_id)
        if n == 1:
            _espelhar_driver(conversa)
            _evento(conversa_id, 'devolveu_bot', ator_id=ator_id, de=de_esperado)
            return Resultado(True, 'ok', 'Conversa devolvida ao EMA.', conversa)
        return _explicar_conflito(conversa, ator_id, 'devolver_bot')

    return _executar(op)


def resolver(conversa_id, ator, visto_ate=None):
    """
    Resolve a conversa.

    visto_ate é a última atividade que o atendente tinha na tela. Se chegou
    mensagem depois disso, o UPDATE não casa e a resolução é recusada: o
    atendente iria fechar uma conversa com mensagem que ele não leu.

    O dono é mantido em assigned_agent_id, para os relatórios saberem quem
    atendeu. O modo do motorista também é mantido: resolver não religa o bot
    em silêncio. Só o vínculo de dono no cadastro é limpo, para que a próxima
    conversa desse contato caia na fila em vez de ir direto para a mesma
    pessoa.
    """
    ator_id = ator.id

    condicoes = [Conversation.status.in_(CONVERSATION_ACTIVE_STATUSES)]
    if not _eh_admin(ator):
        condicoes.append(or_(Conversation.assigned_agent_id == ator_id,
                             Conversation.assigned_agent_id.is_(None)))
    if visto_ate is not None:
        condicoes.append(Conversation.last_activity_at <= visto_ate)

    def op():
        agora = _agora()
        n = _cas(conversa_id, condicoes,
                 dict(status=CONVERSATION_STATUS_RESOLVED, resolved_at=agora))
        conversa = _recarregar(conversa_id)
        if n == 1:
            if conversa.driver is not None:
                conversa.driver.whatsapp_assigned_to = None
            _evento(conversa_id, 'resolveu', ator_id=ator_id,
                    de=conversa.assigned_agent_id)
            return Resultado(True, 'ok', 'Conversa resolvida.', conversa)
        if (conversa is not None and visto_ate is not None
                and conversa.status in CONVERSATION_ACTIVE_STATUSES
                and conversa.last_activity_at and conversa.last_activity_at > visto_ate):
            return Resultado(False, 'conflito',
                             'Chegou mensagem nova nesta conversa. Leia antes de resolver.',
                             conversa)
        return _explicar_conflito(conversa, ator_id, 'resolver')

    return _executar(op)


def reabrir(conversa_id, ator):
    """
    Reabre uma conversa resolvida, com o ator como dono.

    Pode colidir com o índice parcial único: se o contato já escreveu de novo,
    existe outra conversa ativa para ele, e reabrir esta criaria duas.
    """
    ator_id = ator.id

    def op():
        n = _cas(conversa_id,
                 [Conversation.status == CONVERSATION_STATUS_RESOLVED],
                 dict(status=CONVERSATION_STATUS_OPEN, resolved_at=None,
                      assigned_agent_id=ator_id, assigned_at=_agora(),
                      handling_mode=MODO_HUMANO, last_activity_at=_agora()))
        conversa = _recarregar(conversa_id)
        if n == 1:
            _espelhar_driver(conversa)
            _evento(conversa_id, 'reabriu', ator_id=ator_id, para=ator_id)
            return Resultado(True, 'ok', 'Conversa reaberta.', conversa)
        if conversa is not None and conversa.status in CONVERSATION_ACTIVE_STATUSES:
            return Resultado(False, 'conflito', 'Esta conversa já está aberta.', conversa)
        return _explicar_conflito(conversa, ator_id, 'reabrir')

    try:
        return _executar(op)
    except IntegrityError:
        db.session.rollback()
        return Resultado(False, 'conflito',
                         'Este contato já tem outra conversa ativa. Continue por ela.',
                         _recarregar(conversa_id))


def garantir_dono_para_envio(conversa_id, ator):
    """
    Antes de enviar: a conversa precisa ser do ator. Se estiver livre, assume.

    Assumir vem ANTES do envio, e commita, por dois motivos. Dois atendentes
    respondendo juntos a uma conversa livre mandariam as duas mensagens se o
    envio viesse primeiro. E a chamada HTTP ao provedor não pode acontecer
    com a linha da conversa travada.

    Administrador não fura: para responder conversa de outro, transfere para
    si antes.
    """
    return assumir(conversa_id, ator)


def escalar_para_humano(conversa_id, commit=False):
    """
    Marca a conversa como precisando de humano. Não mexe no dono.

    Devolve (conversa, escalou). escalou é False quando a conversa já não
    estava ativa. Segue a ordem de lock conversa, depois motorista.
    """
    n = _cas(conversa_id,
             [Conversation.status.in_(CONVERSATION_ACTIVE_STATUSES)],
             dict(handling_mode=MODO_HUMANO))
    conversa = _recarregar(conversa_id)
    if n == 1:
        _espelhar_driver(conversa)
        _evento(conversa_id, 'escalou_humano', ator_id=None)
    if commit:
        db.session.commit()
    return conversa, n == 1


def escalar_pelo_ema(conversa_id, driver):
    """
    Escalada para humano feita pelo webhook do EMA. Sem commit: o webhook
    commita em seguida.

    Existe como função, e não como linhas soltas no webhook, para que o
    webhook e os testes de concorrência usem exatamente o mesmo código. A
    versão solta escalava e depois zerava Driver.whatsapp_assigned_to. Isso
    só não apagava o dono de um atendente que assumisse no mesmo instante
    porque o SQLAlchemy omite do UPDATE a coluna cujo valor volta a ser igual
    ao carregado. Aqui a correção não depende dessa sutileza.

    Regra: havendo conversa ativa, quem escreve no motorista é o espelho, que
    copia o dono real. O motorista só é tocado diretamente quando não há
    conversa ativa para espelhar, e aí apenas o modo, para calar o bot.
    """
    conversa, escalou = (escalar_para_humano(conversa_id)
                         if conversa_id is not None else (None, False))

    espelhado = escalou and conversa is not None and driver is not None         and conversa.driver_id == driver.id
    if driver is not None and not espelhado:
        driver.whatsapp_mode = MODO_HUMANO
        if conversa is None:
            driver.whatsapp_assigned_to = None
    return conversa


# ── Consultas ────────────────────────────────────────────────────────────────

def atendentes_ativos():
    return (User.query
            .filter(User.active.is_(True), User.role.in_(PAPEIS_ATENDENTE))
            .order_by(User.username)
            .all())


def eventos(conversa_id, limite=50):
    rows = (ConversationEvent.query
            .filter_by(conversation_id=conversa_id)
            .order_by(ConversationEvent.id.desc())
            .limit(limite).all())
    return [{
        'id': e.id,
        'acao': e.acao,
        'ator': _nome(e.ator_id) or 'Sistema',
        'de': _nome(e.de_agente_id),
        'para': _nome(e.para_agente_id),
        'quando': e.created_at.isoformat() if e.created_at else None,
    } for e in rows]
