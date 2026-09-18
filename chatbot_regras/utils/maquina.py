"""
Fase 5, etapa 5.2 — a máquina de estados da raiz "Oferta de frete por região" e
a interface pública que o gancho de roteamento (outra etapa) vai chamar.

Princípios (não violar):
- Estado e contexto vivem SEMPRE na tabela BotSession, nunca em memória do
  processo.
- Telefone normaliza SÓ por normalize_contact_key (atendimento_conversas/utils/
  phone.py), nunca por normalize_phone do evolution_api.
- A máquina NÃO envia WhatsApp e NÃO grava WhatsAppMessage — devolve os textos
  para quem a chamou.
- No máximo 2 tentativas por estado; a 3ª resposta não entendida vira handoff;
  `tentativas` zera a cada troca de estado; a `trilha` grava {estado, em} a cada
  passo.

O contrato completo está em atendimento_conversas/FASE5_CHATBOT.md.
"""
import re
import unicodedata
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from infraestrutura_critica.app import db
from infraestrutura_critica.models import (
    BotSession,
    BOT_ESTADO_ENTRADA, BOT_ESTADO_VIAGEM_ATIVA,
    BOT_SESSAO_ATIVA, BOT_SESSAO_HANDOFF,
    BOT_ORIGEM_CAMPANHA,
)
from atendimento_conversas.utils.phone import (
    normalize_contact_key, find_driver_by_phone, phone_variants,
)
from chatbot_regras.utils import estados


# Teto de saltos automáticos numa mesma mensagem (entrada→identificacao→
# viagem_ativa são 3). Uma folga generosa protege contra um laço acidental.
_MAX_SALTOS = 12


# ── Saídas globais (seção 6): valem em qualquer estado ──────────────────────
# Casadas por palavra inteira (para 'sair'/'pare' não pegarem 'preparar' etc.)
# mais algumas frases de duas palavras.
_OPTOUT_PALAVRAS = {'sair', 'saia', 'pare', 'parar', 'descadastrar',
                    'descadastra', 'cancelar', 'cancela'}
_OPTOUT_FRASES = ('nao quero', 'nao quero mais', 'para de', 'nao me manda',
                  'nao perturbe')

_HUMANO_PALAVRAS = {'atendente', 'humano', 'pessoa'}
_HUMANO_FRASES = ('falar com alguem', 'falar com atendente',
                  'falar com uma pessoa', 'falar com humano',
                  'falar com gente', 'quero um atendente')


def _norm(texto: str) -> str:
    """Minúsculas, sem acento, sem espaços nas pontas."""
    if not texto:
        return ''
    limpo = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    return limpo.strip().lower()


def _gatilho(texto_norm: str, palavras: set, frases=()) -> bool:
    tokens = set(re.findall(r'\w+', texto_norm))
    if tokens & palavras:
        return True
    return any(frase in texto_norm for frase in frases)


class _Contexto:
    """Empacota tudo que os handlers precisam de uma mensagem recebida."""

    def __init__(self, session, chave, texto, media, conversa, driver):
        self.session = session
        self.chave = chave
        self.texto = texto or ''
        self.texto_norm = _norm(self.texto)
        self.media = media
        self.conversa = conversa
        self._driver = driver
        self._driver_resolvido = driver is not None

    @property
    def driver(self):
        """Motorista do telefone, resolvido só por normalize_contact_key.
        Preguiçoso: só consulta o banco quando um handler precisa."""
        if not self._driver_resolvido:
            self._driver = find_driver_by_phone(self.chave)
            self._driver_resolvido = True
        return self._driver


# ── Persistência de um Passo na sessão ──────────────────────────────────────

def _mudar_estado(session, novo):
    if novo and novo != session.estado:
        session.estado_anterior = session.estado
        session.estado = novo
        session.tentativas = 0   # zera a cada troca de estado


def _trilha(session):
    """Grava {estado, em} na trilha, sem repetir o último estado. Reatribui a
    lista para a detecção de mudança do SQLAlchemy pegar."""
    marcas = list(session.trilha or [])
    if not marcas or marcas[-1].get('estado') != session.estado:
        marcas.append({'estado': session.estado,
                       'em': datetime.utcnow().isoformat()})
        session.trilha = marcas


def _aplicar(session, passo, replies):
    """Aplica um Passo à sessão. Devolve True quando o laço deve parar."""
    if passo.textos:
        replies.extend(passo.textos)

    if passo.tipo == 'terminar':
        _mudar_estado(session, passo.estado or session.estado)
        session.status = passo.status
        session.motivo_fim = passo.motivo
        _trilha(session)
        return True

    if passo.tipo == 'ir':
        _mudar_estado(session, passo.estado)
        _trilha(session)
        return not passo.automatico   # para e espera, se não for automático

    if passo.tipo == 'aguardar':
        # "Não entendi": conta a tentativa. Na 3ª no mesmo estado, handoff.
        session.tentativas = (session.tentativas or 0) + 1
        if session.tentativas >= 3:
            passo_handoff = estados.handoff(motivo='nao_entendido')
            _mudar_estado(session, passo_handoff.estado)
            session.status = passo_handoff.status
            session.motivo_fim = passo_handoff.motivo
            _trilha(session)
        return True

    # Passo desconhecido: por segurança, para.
    return True


def _saida_global(ctx):
    """Saídas que valem em qualquer estado (seção 6). Devolve um Passo ou None."""
    if ctx.media:
        # Áudio, foto ou vídeo: a máquina de regras não interpreta mídia.
        return estados.handoff(motivo='midia_recebida')

    t = ctx.texto_norm
    if _gatilho(t, _HUMANO_PALAVRAS, _HUMANO_FRASES):
        return estados.handoff(motivo='pediu_humano')
    if _gatilho(t, _OPTOUT_PALAVRAS, _OPTOUT_FRASES):
        return estados.registrar_e_encerrar_optout(ctx, motivo='pediu_saida')
    return None


def _sessao_ativa(chave):
    """A BotSession ativa do número, casando pelas variantes do 9º dígito.

    O WhatsApp devolve o remoteJid ora com 9 dígitos, ora com 8 (o nono dígito
    foi adicionado por etapas no Brasil). Uma busca por igualdade exata perderia
    a sessão quando a forma disparada difere da forma que volta na resposta — e o
    bot ficaria mudo. `phone_variants` é o mesmo reconciliador que
    `find_driver_by_phone` usa."""
    variantes = list(phone_variants(chave)) or [chave]
    return BotSession.query.filter(
        BotSession.telefone.in_(variantes),
        BotSession.status == BOT_SESSAO_ATIVA).first()


def _resultado(session, replies):
    return {
        'replies': replies,
        'status': session.status,
        'handoff': session.status == BOT_SESSAO_HANDOFF,
        'bot_session_id': session.id,
    }


# ════════════════════════════════════════════════════════════════════════════
# Interface pública
# ════════════════════════════════════════════════════════════════════════════

def processar_mensagem_bot(phone, texto, *, conversa=None, driver=None, media=None):
    """Procura BotSession status='ativa' para phone (normalize_contact_key).
    Se NÃO houver, retorna None (o roteador segue para o ramo 'nada').
    Se houver, avança a máquina, PERSISTE tudo (estado, estado_anterior,
    tentativas, trilha, status, motivo_fim, ultima_msg_em) e retorna:
        {'replies': [str, ...], 'status': str, 'handoff': bool, 'bot_session_id': int}
    NÃO envia WhatsApp nem grava WhatsAppMessage."""
    chave = normalize_contact_key(phone)
    if not chave:
        return None

    session = _sessao_ativa(chave)
    if session is None:
        return None

    # O motorista respondeu: registra atividade e zera o relógio do lembrete.
    session.ultima_msg_em = datetime.utcnow()
    session.lembrete_em = None

    ctx = _Contexto(session, chave, texto, media, conversa, driver)
    replies = []

    # 1) Saídas globais, antes de qualquer estado.
    saida = _saida_global(ctx)
    if saida is not None:
        _aplicar(session, saida, replies)
        db.session.commit()
        return _resultado(session, replies)

    # 2) Laço da máquina: processa o estado atual e segue enquanto as transições
    #    forem automáticas (identificacao e viagem_ativa não pedem texto).
    entrada_usuario = True
    for _ in range(_MAX_SALTOS):
        handler = estados.HANDLERS.get(session.estado)
        if handler is None:
            # Estado de uma etapa ainda não construída: handoff honesto.
            passo = estados.handoff(motivo='etapa_nao_implementada')
        else:
            passo = handler(ctx, entrada_usuario)
        if _aplicar(session, passo, replies):
            break
        entrada_usuario = False
    else:
        # Estouro do teto de saltos: não deveria acontecer na 5.2.
        _aplicar(session, estados.handoff(motivo='laco_estados'), replies)

    db.session.commit()
    return _resultado(session, replies)


def criar_sessao_bot(phone, *, origem=BOT_ORIGEM_CAMPANHA, campanha_id=None,
                     conversa=None, driver=None):
    """Cria BotSession status='ativa' no estado inicial ('entrada' para campanha;
    'viagem_ativa' para reoferta/pedido). Respeita o índice único parcial (uma
    ativa por telefone): se já existe uma ativa, retorna a existente."""
    chave = normalize_contact_key(phone)
    if not chave:
        raise ValueError('telefone sem dígitos utilizáveis para criar_sessao_bot')

    existente = _sessao_ativa(chave)
    if existente is not None:
        return existente

    estado_inicial = (BOT_ESTADO_ENTRADA if origem == BOT_ORIGEM_CAMPANHA
                      else BOT_ESTADO_VIAGEM_ATIVA)
    agora = datetime.utcnow()

    session = BotSession(
        telefone=chave,
        conversation_id=(conversa.id if conversa is not None else None),
        driver_id=(driver.id if driver is not None else None),
        campanha_id=campanha_id,
        origem=origem,
        estado=estado_inicial,
        contexto={'origem': origem},
        trilha=[{'estado': estado_inicial, 'em': agora.isoformat()}],
        tentativas=0,
        status=BOT_SESSAO_ATIVA,
    )
    db.session.add(session)
    try:
        db.session.commit()
    except IntegrityError:
        # Corrida de dois disparos: o índice parcial único barrou o segundo.
        db.session.rollback()
        existente = _sessao_ativa(chave)
        if existente is not None:
            return existente
        raise
    return session
