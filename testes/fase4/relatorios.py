"""
Fase 4 — relatórios, conferidos número a número contra um cenário montado à
mão, com horários fixos.

Casos traiçoeiros incluídos de propósito:
  - mensagem às 23h30 de São Paulo, que em UTC já é o dia seguinte;
  - várias mensagens seguidas do contato, que são uma espera só;
  - resposta do bot, que encerra a espera mas não conta para ninguém;
  - oferta de frete com autor, que não é resposta;
  - resposta que chega depois do fim do período;
  - mensagens e eventos fora do período.

Como rodar: ver testes/fase4/LEIAME.md.
"""
import ast
import csv
import io
import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes da app

import logging  # noqa: E402
logging.disable(logging.WARNING)

from infraestrutura_critica.main import app                       # noqa: E402
from infraestrutura_critica.app import db                         # noqa: E402
from infraestrutura_critica.models import (                       # noqa: E402
    Conversation, ConversationEvent, User, WhatsAppMessage,
)
from atendimento_conversas.utils import relatorios                # noqa: E402

app.config['TESTING'] = True
DIALETO = _ambiente.conferir_dialeto(app, db)

FALHAS = []


def check(label, ok, det=''):
    print(f"  [{'OK ' if ok else 'FALHA'}] {label}" + (f"  ({det})" if det else ''))
    if not ok:
        FALHAS.append(label)


def utc(m, d, h, mi, s=0, ano=2026):
    return datetime(ano, m, d, h, mi, s)


def usuario(nome, papel='operador'):
    with app.app_context():
        u = User.query.filter_by(username=nome).first()
        if u is None:
            u = User(username=nome, email=f'{abs(hash(nome))}@teste.local', password_hash='x',
                     role=papel, active=True)
            db.session.add(u)
            db.session.commit()
        return SimpleNamespace(id=u.id, nome=u.username)


def navegador(u):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


A, B = usuario('rel_a'), usuario('rel_b')
ADMIN, OP = usuario('rel_admin', 'admin'), usuario('rel_op')
INJETOR = usuario('=cmd|calc')

# ── Cenário ──────────────────────────────────────────────────────────────────
# Semana local de 03/08/2026 (segunda) a 09/08/2026 (domingo). São Paulo é
# UTC-3 o ano todo.
with app.app_context():
    def conversa(fone, criada, **kw):
        c = Conversation(contact_phone=fone, created_at=criada, last_activity_at=criada, **kw)
        db.session.add(c)
        db.session.flush()
        return c.id

    def msg(cid, direcao, quando, autor=None, source=None):
        db.session.add(WhatsAppMessage(
            conversation_id=cid, phone_number='5511900000000', message_content='x',
            direction=direcao, created_by=autor, sent_at=quando,
            source=source or ('teste' if direcao == 'inbound' else 'operator'),
            status='recebido' if direcao == 'inbound' else 'enviado'))

    def evento(cid, acao, quando, ator=None, para=None, de=None):
        db.session.add(ConversationEvent(conversation_id=cid, acao=acao, created_at=quando,
                                         ator_id=ator, para_agente_id=para, de_agente_id=de))

    # C1, 03/08: três esperas.
    c1 = conversa('5511911110001', utc(8, 3, 13, 0), status='aberta', handling_mode='manual',
                  assigned_agent_id=B.id)
    msg(c1, 'inbound', utc(8, 3, 13, 0))
    msg(c1, 'inbound', utc(8, 3, 13, 2))                        # mesma espera
    msg(c1, 'outbound', utc(8, 3, 13, 10), autor=A.id)          # A: 600 s
    msg(c1, 'inbound', utc(8, 3, 14, 0))
    msg(c1, 'outbound', utc(8, 3, 14, 0, 30), source='ema')     # bot encerra, sem tempo
    msg(c1, 'inbound', utc(8, 3, 15, 0))
    msg(c1, 'outbound', utc(8, 3, 15, 3), autor=B.id)           # B: 180 s
    evento(c1, 'assumiu', utc(8, 3, 13, 5), ator=A.id, para=A.id)
    evento(c1, 'transferiu', utc(8, 3, 14, 30), ator=A.id, de=A.id, para=B.id)

    # C2: 02h30 UTC de 04/08 é 23h30 de 03/08 em São Paulo.
    c2 = conversa('5511911110002', utc(8, 4, 2, 30), status='resolvida', handling_mode='manual',
                  assigned_agent_id=A.id, resolved_at=utc(8, 4, 4, 0))
    msg(c2, 'inbound', utc(8, 4, 2, 30))
    msg(c2, 'outbound', utc(8, 4, 3, 30), autor=A.id)           # A: 3600 s, em 04/08 local
    evento(c2, 'assumiu', utc(8, 4, 2, 40), ator=A.id, para=A.id)
    evento(c2, 'resolveu', utc(8, 4, 4, 0), ator=A.id, de=A.id)

    # C3, 05/08: oferta em massa com autor não é resposta; espera fica aberta.
    c3 = conversa('5511911110003', utc(8, 5, 12, 0), status='aberta', handling_mode='manual')
    msg(c3, 'inbound', utc(8, 5, 12, 0))
    msg(c3, 'outbound', utc(8, 5, 12, 5), autor=A.id, source='freight')

    # C4: tudo fora do período.
    c4 = conversa('5511911110004', utc(8, 2, 20, 0), status='aberta', handling_mode='auto')
    msg(c4, 'inbound', utc(8, 3, 2, 0))                         # 02/08 23h00 local
    msg(c4, 'inbound', utc(8, 10, 3, 30))                       # 10/08 00h30 local
    evento(c4, 'assumiu', utc(8, 10, 12, 0), ator=B.id, para=B.id)

    # C9, 01/07: nome de usuário com fórmula, para o CSV.
    c9 = conversa('5511911110009', utc(7, 1, 13, 0), status='aberta', handling_mode='manual')
    msg(c9, 'inbound', utc(7, 1, 13, 0))
    msg(c9, 'outbound', utc(7, 1, 13, 1), autor=INJETOR.id)

    db.session.commit()

ADM = navegador(ADMIN)


def gerar(de, ate):
    r = ADM.get(f'/conversas/api/relatorios?de={de}&ate={ate}')
    return r.status_code, r.get_json()


def linha(rel, nome):
    return next((a for a in rel['atendentes'] if a['nome'] == nome), None)


print()
print('=' * 72)
print(f'FASE 4 — RELATÓRIOS  |  banco: {DIALETO}')
print('=' * 72)


# ── R1. A semana inteira ─────────────────────────────────────────────────────
print('\nR1. semana de 03/08 a 09/08')
st, rel = gerar('2026-08-03', '2026-08-09')
check('relatório gerado', st == 200, f'HTTP {st}')
s = rel['resumo']
check('recebidas = 6', s['recebidas'] == 6, s['recebidas'])
check('respostas de atendente = 3', s['enviadas_atendente'] == 3, s['enviadas_atendente'])
check('respostas do bot = 1', s['enviadas_bot'] == 1, s['enviadas_bot'])
check('oferta em massa contada como automação = 1', s['enviadas_automacao'] == 1, s['enviadas_automacao'])
check('conversas abertas = 3, com C2 no dia local', s['conversas_abertas'] == 3, s['conversas_abertas'])
check('conversas resolvidas = 1', s['conversas_resolvidas'] == 1, s['conversas_resolvidas'])
check('esperas sem resposta = 1, a da oferta', s['esperas_sem_resposta'] == 1, s['esperas_sem_resposta'])
t = s['tempo_resposta']
check('tempo global: 3 respostas, média 1460 s, mediana 600 s',
      (t['quantidade'], t['media_segundos'], t['mediana_segundos']) == (3, 1460, 600), str(t))

a, b = linha(rel, 'rel_a'), linha(rel, 'rel_b')
check('A: 2 conversas, 2 mensagens', a and (a['conversas_atendidas'], a['mensagens_enviadas']) == (2, 2), str(a))
check('A: assumiu 2, resolveu 1, recebeu 0 por transferência',
      a and (a['assumidas'], a['resolvidas'], a['recebidas_por_transferencia']) == (2, 1, 0))
check('A: tempos 600 e 3600, média e mediana 2100',
      a and (a['tempo_resposta']['quantidade'], a['tempo_resposta']['media_segundos'],
             a['tempo_resposta']['mediana_segundos']) == (2, 2100, 2100), str(a and a['tempo_resposta']))
check('B: 1 conversa, 1 mensagem, recebeu 1 por transferência',
      b and (b['conversas_atendidas'], b['mensagens_enviadas'], b['recebidas_por_transferencia']) == (1, 1, 1),
      str(b))
check('B: tempo 180 s', b and b['tempo_resposta']['mediana_segundos'] == 180)
check('evento de B fora do período não conta', b and b['assumidas'] == 0)

dias = {d['dia']: d for d in rel['dias']}
check('7 dias listados, inclusive os vazios', len(rel['dias']) == 7 and '2026-08-09' in dias)
d3, d4, d5 = dias['2026-08-03'], dias['2026-08-04'], dias['2026-08-05']
check('03/08: 5 recebidas, com a das 23h30 local', d3['recebidas'] == 5, d3['recebidas'])
check('03/08: 2 de atendente, 1 do bot, 2 abertas',
      (d3['enviadas_atendente'], d3['enviadas_bot'], d3['conversas_abertas']) == (2, 1, 2), str(d3))
check('04/08: a resposta das 00h30 local e a resolução',
      (d4['recebidas'], d4['enviadas_atendente'], d4['conversas_resolvidas']) == (0, 1, 1), str(d4))
check('05/08: 1 recebida, 1 automação, 1 aberta',
      (d5['recebidas'], d5['enviadas_automacao'], d5['conversas_abertas']) == (1, 1, 1), str(d5))
check('09/08 zerado', all(v == 0 for k, v in dias['2026-08-09'].items() if k != 'dia'))

sem = rel['semanas']
check('uma semana, de segunda a domingo, com os totais',
      len(sem) == 1 and sem[0]['inicio'] == '2026-08-03' and sem[0]['fim'] == '2026-08-09'
      and sem[0]['dias_no_periodo'] == 7 and sem[0]['recebidas'] == 6 and sem[0]['enviadas_atendente'] == 3,
      str(sem[0] if sem else None))

# "Agora" é o estado atual do banco inteiro, não do período: C1 com B, C3 e
# C9 livres fora do EMA, C4 com o EMA, C2 resolvida.
ag = rel['agora']
check('agora: 4 ativas, 2 na fila humana, 1 com atendente, 1 com o EMA',
      (ag['ativas'], ag['na_fila_humana'], ag['com_atendente'], ag['com_ema']) == (4, 2, 1, 1), str(ag))


# ── R2. Um dia só: espera que começou antes não entra ────────────────────────
print('\nR2. só o dia 04/08')
st, rel = gerar('2026-08-04', '2026-08-04')
a = linha(rel, 'rel_a')
check('A enviou 1 mensagem em 04/08', a and a['mensagens_enviadas'] == 1, str(a))
check('mas sem tempo medido: a espera começou em 03/08', a and a['tempo_resposta']['quantidade'] == 0)
check('recebidas 0, resolvidas 1',
      (rel['resumo']['recebidas'], rel['resumo']['conversas_resolvidas']) == (0, 1), str(rel['resumo']))


# ── R3. Resposta depois do fim do período ────────────────────────────────────
print('\nR3. só o dia 03/08: a resposta das 00h30 do dia seguinte ainda mede a espera')
st, rel = gerar('2026-08-03', '2026-08-03')
a = linha(rel, 'rel_a')
check('A: 1 mensagem no dia, mas 2 tempos medidos', a and a['mensagens_enviadas'] == 1
      and a['tempo_resposta']['quantidade'] == 2, str(a))
check('nenhuma espera ficou sem resposta', rel['resumo']['esperas_sem_resposta'] == 0)


# ── R4. Validação do período ─────────────────────────────────────────────────
print('\nR4. período inválido')
check('início depois do fim: 400', gerar('2026-08-09', '2026-08-03')[0] == 400)
check('mais de 92 dias: 400', gerar('2026-01-01', '2026-08-01')[0] == 400)
check('formato errado: 400', gerar('03/08/2026', '2026-08-09')[0] == 400)
check('92 dias exatos: aceito', gerar('2026-05-10', '2026-08-09')[0] == 200)


# ── R5. Acesso ───────────────────────────────────────────────────────────────
print('\nR5. só administrador')
op = navegador(OP)
check('operador: página 403', op.get('/conversas/relatorios').status_code == 403)
check('operador: API 403', op.get('/conversas/api/relatorios').status_code == 403)
check('operador: CSV 403', op.get('/conversas/api/relatorios.csv').status_code == 403)
pag = ADM.get('/conversas/relatorios')
html = pag.get_data(as_text=True)
check('admin: página 200 com gráfico e script', pag.status_code == 200 and 'chart.js@4.4.0' in html
      and 'conversas_relatorios.js' in html)
check('admin vê atalho de Relatórios em Conversas',
      '/conversas/relatorios' in ADM.get('/conversas/').get_data(as_text=True))
check('operador não vê o atalho', '/conversas/relatorios' not in op.get('/conversas/').get_data(as_text=True))


# ── R6. CSV ──────────────────────────────────────────────────────────────────
print('\nR6. CSV')
r = ADM.get('/conversas/api/relatorios.csv?tipo=atendentes&de=2026-08-03&ate=2026-08-09')
texto = r.get_data(as_text=True)
check('tipo text/csv com nome de arquivo', r.mimetype == 'text/csv'
      and 'central-atendentes-2026-08-03-a-2026-08-09.csv' in r.headers.get('Content-Disposition', ''))
check('BOM UTF-8 para o Excel', texto.startswith('﻿'))
linhas = list(csv.reader(io.StringIO(texto.lstrip('﻿')), delimiter=';'))
check('cabeçalho e uma linha por atendente', linhas[0][0] == 'atendente' and len(linhas) == 3, str(len(linhas)))
linha_a = next((l for l in linhas if l[0] == 'rel_a'), None)
check('linha de A bate com a API', linha_a and linha_a[1:] == ['2', '2', '2', '0', '1', '2', '2100', '2100'],
      str(linha_a))
r = ADM.get('/conversas/api/relatorios.csv?tipo=dias&de=2026-08-03&ate=2026-08-09')
check('CSV de dias: cabeçalho e 7 linhas',
      len(list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip('﻿')), delimiter=';'))) == 8)
r = ADM.get('/conversas/api/relatorios.csv?tipo=atendentes&de=2026-07-01&ate=2026-07-01')
celulas = [l[0] for l in csv.reader(io.StringIO(r.get_data(as_text=True).lstrip('﻿')), delimiter=';')]
check('nome começando com = sai neutralizado', "'=cmd|calc" in celulas and '=cmd|calc' not in celulas,
      str(celulas))
check('tipo inválido: 400', ADM.get('/conversas/api/relatorios.csv?tipo=xpto').status_code == 400)


# ── R7. Portabilidade ────────────────────────────────────────────────────────
print('\nR7. nenhuma função de data de dialeto no código dos relatórios')
fonte = open(os.path.join(_ambiente.RAIZ, 'atendimento_conversas', 'utils', 'relatorios.py'),
             encoding='utf-8').read()
arvore = ast.parse(fonte)
funcoes_sql = {n.attr for n in ast.walk(arvore)
               if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'func'}
proibidas = {'date_trunc', 'strftime', 'extract', 'date', 'to_char', 'julianday', 'date_part'}
check('sem func.date_trunc, strftime, extract e afins', not (funcoes_sql & proibidas), str(funcoes_sql))
chamadas_text = [n for n in ast.walk(arvore) if isinstance(n, ast.Call)
                 and getattr(n.func, 'id', None) == 'text']
check('sem SQL cru', not chamadas_text)


print()
print('=' * 72)
if FALHAS:
    print(f'RESULTADO ({DIALETO}): {len(FALHAS)} FALHA(S)')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)
print(f'RESULTADO ({DIALETO}): TUDO OK')
