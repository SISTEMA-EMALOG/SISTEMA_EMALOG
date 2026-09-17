"""
Relatórios da Central de Atendimento (Fase 4).

Regra de portabilidade
----------------------
As consultas só filtram por intervalo de datas e listas de ids. Nenhum
date_trunc, strftime, extract ou date(): foi um date_trunc em relatório que
quebrou este sistema no SQLite. O agrupamento por dia e por semana acontece
aqui, em Python, sobre as linhas já filtradas.

Isso também resolve o fuso. O banco guarda UTC sem fuso. Um dia de operação
é um dia em São Paulo: uma mensagem às 23h30 de segunda, horário local, está
gravada como 02h30 de terça em UTC, e precisa contar na segunda.

Definições
----------
Resposta de atendente: mensagem enviada com source='operator' e autor
preenchido. É o que a Central e a Contratação gravam.

Resposta do bot: mensagem enviada sem autor. Encerra a espera do contato,
mas não entra no tempo de resposta de ninguém.

Automação: mensagem enviada com autor mas source diferente de 'operator',
como oferta de frete disparada em massa. Não responde ao que o contato
perguntou, então não encerra a espera nem conta como resposta.

Espera: começa na PRIMEIRA mensagem recebida depois da última resposta.
Várias mensagens seguidas do contato são uma espera só. Termina na primeira
resposta de atendente ou do bot. Só entram esperas que começaram dentro do
período.

Tempo de resposta: duração da espera encerrada por atendente, atribuída a
quem respondeu. Relatado em média e mediana, porque uma única espera durante
a madrugada distorce a média.
"""

import csv
import io
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from infraestrutura_critica.app import db
from infraestrutura_critica.models import (
    Conversation,
    ConversationEvent,
    User,
    WhatsAppMessage,
    CONVERSATION_ACTIVE_STATUSES,
)

try:
    from zoneinfo import ZoneInfo
    FUSO = ZoneInfo('America/Sao_Paulo')
except Exception:                              # pragma: no cover
    # Sem base de fusos: São Paulo não tem horário de verão desde 2019.
    FUSO = timezone(timedelta(hours=-3))

MAX_DIAS = 92

# Respostas que chegam logo depois do fim do período ainda contam para
# esperas que começaram dentro dele.
FOLGA_RESPOSTA = timedelta(days=2)

# Teto de linhas lidas. Acima disso o período é grande demais para agregar em
# memória com segurança.
MAX_LINHAS = 300_000


class PeriodoInvalido(ValueError):
    pass


# ── Período e fuso ───────────────────────────────────────────────────────────

def local_para_utc(dia_local, hora=time.min):
    """Início de um dia de São Paulo, como datetime UTC sem fuso."""
    return datetime.combine(dia_local, hora, tzinfo=FUSO).astimezone(timezone.utc).replace(tzinfo=None)


def utc_para_dia_local(momento_utc):
    return momento_utc.replace(tzinfo=timezone.utc).astimezone(FUSO).date()


def hoje_local():
    return datetime.now(timezone.utc).astimezone(FUSO).date()


def ler_periodo(de_txt, ate_txt):
    """Valida o período pedido. Sem datas: últimos 7 dias, incluindo hoje."""
    try:
        ate = date.fromisoformat(ate_txt) if ate_txt else hoje_local()
        de = date.fromisoformat(de_txt) if de_txt else ate - timedelta(days=6)
    except ValueError:
        raise PeriodoInvalido('Datas no formato AAAA-MM-DD.')
    if de > ate:
        raise PeriodoInvalido('A data inicial é depois da final.')
    if (ate - de).days + 1 > MAX_DIAS:
        raise PeriodoInvalido(f'Período máximo de {MAX_DIAS} dias.')
    return de, ate


# ── Agregação ────────────────────────────────────────────────────────────────

@dataclass
class Atendente:
    id: int
    nome: str
    conversas_atendidas: set = field(default_factory=set)
    mensagens_enviadas: int = 0
    assumidas: int = 0
    recebidas_por_transferencia: int = 0
    resolvidas: int = 0
    tempos: list = field(default_factory=list)


def _estatistica(tempos):
    if not tempos:
        return {'quantidade': 0, 'media_segundos': None, 'mediana_segundos': None}
    return {
        'quantidade': len(tempos),
        'media_segundos': round(statistics.fmean(tempos)),
        'mediana_segundos': round(statistics.median(tempos)),
    }


def _dia_vazio(dia):
    return {'dia': dia.isoformat(), 'recebidas': 0, 'enviadas_atendente': 0, 'enviadas_bot': 0,
            'enviadas_automacao': 0, 'conversas_abertas': 0, 'conversas_resolvidas': 0}


def _tipo_saida(source, created_by):
    if created_by is None:
        return 'bot'
    if source == 'operator':
        return 'atendente'
    return 'automacao'


def gerar(de, ate):
    """Relatório do período [de, ate], datas locais inclusivas."""
    inicio = local_para_utc(de)
    fim = local_para_utc(ate + timedelta(days=1))

    # ── Mensagens: filtro por intervalo, nada mais ──────────────────────────
    linhas = (
        db.session.query(WhatsAppMessage.conversation_id, WhatsAppMessage.direction,
                         WhatsAppMessage.source, WhatsAppMessage.created_by,
                         WhatsAppMessage.sent_at, WhatsAppMessage.id)
        .filter(WhatsAppMessage.conversation_id.isnot(None),
                WhatsAppMessage.sent_at >= inicio,
                WhatsAppMessage.sent_at < fim + FOLGA_RESPOSTA)
        .order_by(WhatsAppMessage.conversation_id, WhatsAppMessage.sent_at, WhatsAppMessage.id)
        .limit(MAX_LINHAS + 1)
        .all()
    )
    if len(linhas) > MAX_LINHAS:
        raise PeriodoInvalido('Volume grande demais para este período. Reduza o intervalo.')

    dias = {}
    d = de
    while d <= ate:
        dias[d] = _dia_vazio(d)
        d += timedelta(days=1)

    atendentes = {}

    def atendente(uid):
        if uid not in atendentes:
            atendentes[uid] = Atendente(id=uid, nome=f'#{uid}')
        return atendentes[uid]

    tempos_globais = []
    esperas_abertas = 0

    conversa_atual, espera_inicio = None, None
    for cid, direcao, source, autor, enviado, _mid in linhas:
        if cid != conversa_atual:
            if espera_inicio is not None:
                esperas_abertas += 1
            conversa_atual, espera_inicio = cid, None

        no_periodo = inicio <= enviado < fim
        if no_periodo:
            dia = dias[utc_para_dia_local(enviado)]

        if direcao == 'inbound':
            if no_periodo:
                dia['recebidas'] += 1
                if espera_inicio is None:
                    espera_inicio = enviado
            continue

        tipo = _tipo_saida(source, autor)
        if no_periodo:
            dia[f'enviadas_{tipo}'] += 1
            if tipo == 'atendente':
                a = atendente(autor)
                a.mensagens_enviadas += 1
                a.conversas_atendidas.add(cid)

        if tipo == 'automacao' or espera_inicio is None:
            continue
        if tipo == 'atendente':
            segundos = (enviado - espera_inicio).total_seconds()
            atendente(autor).tempos.append(segundos)
            tempos_globais.append(segundos)
        espera_inicio = None

    if espera_inicio is not None:
        esperas_abertas += 1

    # ── Eventos da fila ─────────────────────────────────────────────────────
    for acao, ator, para in (
        db.session.query(ConversationEvent.acao, ConversationEvent.ator_id,
                         ConversationEvent.para_agente_id)
        .filter(ConversationEvent.created_at >= inicio, ConversationEvent.created_at < fim)
        .all()
    ):
        if acao == 'assumiu' and ator:
            atendente(ator).assumidas += 1
        elif acao == 'transferiu' and para:
            atendente(para).recebidas_por_transferencia += 1
        elif acao == 'resolveu' and ator:
            atendente(ator).resolvidas += 1

    # ── Conversas abertas e resolvidas por dia ──────────────────────────────
    for (criada,) in (db.session.query(Conversation.created_at)
                      .filter(Conversation.created_at >= inicio, Conversation.created_at < fim).all()):
        dias[utc_para_dia_local(criada)]['conversas_abertas'] += 1
    for (resolvida,) in (db.session.query(Conversation.resolved_at)
                         .filter(Conversation.resolved_at >= inicio, Conversation.resolved_at < fim).all()):
        dias[utc_para_dia_local(resolvida)]['conversas_resolvidas'] += 1

    # ── Situação agora ──────────────────────────────────────────────────────
    ativas = Conversation.query.filter(Conversation.status.in_(CONVERSATION_ACTIVE_STATUSES))
    agora = {
        'ativas': ativas.count(),
        'na_fila_humana': ativas.filter(Conversation.assigned_agent_id.is_(None),
                                        Conversation.handling_mode == 'manual').count(),
        'com_atendente': ativas.filter(Conversation.assigned_agent_id.isnot(None)).count(),
        'com_ema': ativas.filter(Conversation.assigned_agent_id.is_(None),
                                 Conversation.handling_mode == 'auto').count(),
    }

    if atendentes:
        for uid, nome in db.session.query(User.id, User.username).filter(User.id.in_(atendentes)).all():
            atendentes[uid].nome = nome

    lista_dias = [dias[k] for k in sorted(dias)]
    return {
        'periodo': {'de': de.isoformat(), 'ate': ate.isoformat(), 'fuso': 'America/Sao_Paulo'},
        'resumo': {
            'recebidas': sum(x['recebidas'] for x in lista_dias),
            'enviadas_atendente': sum(x['enviadas_atendente'] for x in lista_dias),
            'enviadas_bot': sum(x['enviadas_bot'] for x in lista_dias),
            'enviadas_automacao': sum(x['enviadas_automacao'] for x in lista_dias),
            'conversas_abertas': sum(x['conversas_abertas'] for x in lista_dias),
            'conversas_resolvidas': sum(x['conversas_resolvidas'] for x in lista_dias),
            'esperas_sem_resposta': esperas_abertas,
            'tempo_resposta': _estatistica(tempos_globais),
        },
        'agora': agora,
        'atendentes': sorted((
            {
                'id': a.id,
                'nome': a.nome,
                'conversas_atendidas': len(a.conversas_atendidas),
                'mensagens_enviadas': a.mensagens_enviadas,
                'assumidas': a.assumidas,
                'recebidas_por_transferencia': a.recebidas_por_transferencia,
                'resolvidas': a.resolvidas,
                'tempo_resposta': _estatistica(a.tempos),
            } for a in atendentes.values()
        ), key=lambda x: (-x['conversas_atendidas'], x['nome'])),
        'dias': lista_dias,
        'semanas': _por_semana(lista_dias),
    }


def _por_semana(lista_dias):
    """Semanas de segunda a domingo, somando só os dias dentro do período."""
    semanas = {}
    for d in lista_dias:
        dia = date.fromisoformat(d['dia'])
        segunda = dia - timedelta(days=dia.weekday())
        s = semanas.setdefault(segunda, {
            'inicio': segunda.isoformat(),
            'fim': (segunda + timedelta(days=6)).isoformat(),
            'dias_no_periodo': 0,
            **{k: 0 for k in d if k != 'dia'},
        })
        s['dias_no_periodo'] += 1
        for k, v in d.items():
            if k != 'dia':
                s[k] += v
    return [semanas[k] for k in sorted(semanas)]


# ── CSV ──────────────────────────────────────────────────────────────────────

def _celula(valor):
    """
    Neutraliza injeção de fórmula: planilha trata célula que começa com
    = + - @ como fórmula. Um nome de usuário '=HYPERLINK(...)' viraria link
    ao abrir o arquivo.
    """
    if valor is None:
        return ''
    texto = str(valor)
    if texto and texto[0] in '=+-@\t\r':
        return "'" + texto
    return texto


def csv_atendentes(relatorio):
    linhas = [['atendente', 'conversas_atendidas', 'mensagens_enviadas', 'assumidas',
               'recebidas_por_transferencia', 'resolvidas', 'respostas_medidas',
               'tempo_medio_segundos', 'tempo_mediano_segundos']]
    for a in relatorio['atendentes']:
        t = a['tempo_resposta']
        linhas.append([a['nome'], a['conversas_atendidas'], a['mensagens_enviadas'], a['assumidas'],
                       a['recebidas_por_transferencia'], a['resolvidas'], t['quantidade'],
                       t['media_segundos'], t['mediana_segundos']])
    return _montar_csv(linhas)


def csv_dias(relatorio):
    campos = ['dia', 'recebidas', 'enviadas_atendente', 'enviadas_bot', 'enviadas_automacao',
              'conversas_abertas', 'conversas_resolvidas']
    return _montar_csv([campos] + [[d[c] for c in campos] for d in relatorio['dias']])


def _montar_csv(linhas):
    """Ponto e vírgula e BOM UTF-8: é assim que o Excel em português abre certo."""
    saida = io.StringIO()
    escritor = csv.writer(saida, delimiter=';', lineterminator='\r\n')
    for linha in linhas:
        escritor.writerow([_celula(v) for v in linha])
    return '﻿' + saida.getvalue()
