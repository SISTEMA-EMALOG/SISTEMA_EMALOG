"""
Fase 2 — prova de concorrência da fila, no banco configurado.

Cada "atendente" é uma thread com app_context próprio, logo sessão e conexão
próprias. Todas esperam numa threading.Barrier e disparam no mesmo instante.
Cada cenário roda RODADAS vezes; uma única violação de invariante reprova.

Como rodar: ver testes/fase2/LEIAME.md
"""
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes da app

import logging  # noqa: E402
logging.disable(logging.WARNING)

from infraestrutura_critica.main import app                      # noqa: E402
from infraestrutura_critica.app import db                        # noqa: E402
from infraestrutura_critica.models import (                      # noqa: E402
    Conversation, ConversationEvent, Driver, User, WhatsAppMessage,
)
from atendimento_conversas.utils import fila                     # noqa: E402
from atendimento_conversas.utils.conversas_service import (      # noqa: E402
    conversa_para_entrada, registrar_entrada,
)
from sqlalchemy import text                                      # noqa: E402

RODADAS = int(os.environ.get('RODADAS', '30'))

N_ATENDENTES = 5

DIALETO = _ambiente.conferir_dialeto(app, db)

FALHAS = []
CONTADOR = {'n': 0}


def check(label, ok, det=''):
    marca = 'OK ' if ok else 'FALHA'
    print(f"  [{marca}] {label}" + (f"  ({det})" if det else ''))
    if not ok:
        FALHAS.append(label)


def fone_unico():
    CONTADOR['n'] += 1
    return f"55119{int(time.time() * 1000) % 10000000:07d}{CONTADOR['n'] % 10}"[:13]


def ator(u):
    return SimpleNamespace(id=u['id'], role=u['role'])


def correr(tarefas):
    """Dispara todas as tarefas juntas. Devolve resultado ou exceção de cada uma."""
    barreira = threading.Barrier(len(tarefas))
    saida = [None] * len(tarefas)

    def rodar(i, tarefa):
        with app.app_context():
            try:
                barreira.wait(timeout=30)
                r = tarefa()
                saida[i] = ('ok', getattr(r, 'ok', None), getattr(r, 'codigo', None),
                            getattr(r, 'mensagem', None)) if r is not None else ('ok', None, None, None)
            except Exception as exc:
                saida[i] = ('erro', type(exc).__name__, str(exc)[:300],
                            traceback.format_exc()[-600:])
            finally:
                db.session.remove()

    ts = [threading.Thread(target=rodar, args=(i, t)) for i, t in enumerate(tarefas)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    return saida


# ── Preparo ──────────────────────────────────────────────────────────────────

def preparar_usuarios():
    with app.app_context():
        usuarios = []
        for i in range(N_ATENDENTES):
            nome = f'atendente{i}'
            u = User.query.filter_by(username=nome).first()
            if u is None:
                u = User(username=nome, email=f'{nome}@teste.local', password_hash='x',
                         role='operador', active=True)
                db.session.add(u)
                db.session.commit()
            usuarios.append({'id': u.id, 'role': u.role, 'nome': u.username})
        adm = User.query.filter_by(username='supervisor').first()
        if adm is None:
            adm = User(username='supervisor', email='supervisor@teste.local', password_hash='x',
                       role='admin', active=True)
            db.session.add(adm)
            db.session.commit()
        return usuarios, {'id': adm.id, 'role': 'admin', 'nome': 'supervisor'}


def nova_conversa(dono_id=None, com_motorista=True):
    with app.app_context():
        fone = fone_unico()
        drv = None
        if com_motorista:
            drv = Driver(name=f'Motorista {fone}', phone=fone,
                         whatsapp_mode='manual' if dono_id else 'auto',
                         whatsapp_assigned_to=dono_id)
            db.session.add(drv)
            db.session.flush()
        c = Conversation(contact_phone=fone, driver_id=drv.id if drv else None,
                         status='aberta', handling_mode='manual' if dono_id else 'auto',
                         assigned_agent_id=dono_id, last_activity_at=datetime.utcnow())
        db.session.add(c)
        db.session.commit()
        return {'id': c.id, 'fone': fone, 'driver_id': drv.id if drv else None}


def estado(conv_id):
    with app.app_context():
        c = db.session.get(Conversation, conv_id)
        d = db.session.get(Driver, c.driver_id) if c.driver_id else None
        eventos = [e.acao for e in ConversationEvent.query.filter_by(conversation_id=conv_id)
                   .order_by(ConversationEvent.id).all()]
        return {
            'status': c.status, 'dono': c.assigned_agent_id, 'modo': c.handling_mode,
            'drv_modo': d.whatsapp_mode if d else None,
            'drv_dono': d.whatsapp_assigned_to if d else None,
            'eventos': eventos, 'last': c.last_activity_at,
        }


USUARIOS, ADMIN = preparar_usuarios()
NOMES = {u['id']: u['nome'] for u in USUARIOS}

print()
print('=' * 72)
print(f'FASE 2 — CONCORRÊNCIA  |  banco: {DIALETO}  |  {RODADAS} rodadas por cenário')
print('=' * 72)


# ── C1. Cinco atendentes clicam "assumir" no mesmo instante ─────────────────
print(f'\nC1. {N_ATENDENTES} atendentes assumindo a MESMA conversa ao mesmo tempo')
violacoes, erros, espelho_ruim = 0, [], 0
for _ in range(RODADAS):
    conv = nova_conversa()
    saida = correr([lambda u=u: fila.assumir(conv['id'], ator(u)) for u in USUARIOS])
    vencedores = [i for i, s in enumerate(saida) if s[0] == 'ok' and s[2] == 'ok']
    perdedores = [s for s in saida if s[0] == 'ok' and s[2] == 'conflito']
    erros += [s for s in saida if s[0] == 'erro']
    st = estado(conv['id'])
    if len(vencedores) != 1 or len(perdedores) != N_ATENDENTES - 1:
        violacoes += 1
        continue
    vencedor = USUARIOS[vencedores[0]]
    if st['dono'] != vencedor['id'] or st['eventos'].count('assumiu') != 1:
        violacoes += 1
    # O perdedor precisa ser avisado de QUEM ganhou.
    if not all(vencedor['nome'] in (p[3] or '') for p in perdedores):
        violacoes += 1
    if st['drv_dono'] != vencedor['id'] or st['drv_modo'] != 'manual':
        espelho_ruim += 1
check('exatamente 1 vencedor e 4 conflitos em toda rodada', violacoes == 0,
      f'{violacoes} violação(ões)')
check('nenhuma exceção', not erros, erros[0][1] + ': ' + erros[0][2] if erros else '')
check('espelho no motorista segue o vencedor, bot calado', espelho_ruim == 0,
      f'{espelho_ruim} divergência(s)')


# ── C2. Dono transfere enquanto outro tenta assumir ─────────────────────────
print('\nC2. dono transfere para B enquanto C tenta assumir')
A, B, C = USUARIOS[0], USUARIOS[1], USUARIOS[2]
violacoes, erros = 0, []
for _ in range(RODADAS):
    conv = nova_conversa(dono_id=A['id'])
    saida = correr([
        lambda: fila.transferir(conv['id'], ator(A), B['id']),
        lambda: fila.assumir(conv['id'], ator(C)),
    ])
    erros += [s for s in saida if s[0] == 'erro']
    st = estado(conv['id'])
    if not (saida[0][2] == 'ok' and saida[1][2] == 'conflito' and st['dono'] == B['id']
            and st['eventos'] == ['transferiu'] and st['drv_dono'] == B['id']):
        violacoes += 1
check('transferência vence, assumir recebe conflito, dono final é B', violacoes == 0,
      f'{violacoes} violação(ões)')
check('nenhuma exceção', not erros, erros[0][2] if erros else '')


# ── C3. Dono e admin transferem para pessoas diferentes ao mesmo tempo ──────
print('\nC3. dono transfere para B e admin transfere para C, simultâneos')
violacoes, erros = 0, []
placar = {'dono': 0, 'admin': 0}
for _ in range(RODADAS):
    conv = nova_conversa(dono_id=A['id'])
    saida = correr([
        lambda: fila.transferir(conv['id'], ator(A), B['id']),
        lambda: fila.transferir(conv['id'], ator(ADMIN), C['id'], de_esperado=A['id']),
    ])
    erros += [s for s in saida if s[0] == 'erro']
    ok = [i for i, s in enumerate(saida) if s[0] == 'ok' and s[2] == 'ok']
    st = estado(conv['id'])
    if len(ok) != 1 or st['eventos'].count('transferiu') != 1:
        violacoes += 1
        continue
    esperado = B['id'] if ok[0] == 0 else C['id']
    placar['dono' if ok[0] == 0 else 'admin'] += 1
    if st['dono'] != esperado or st['drv_dono'] != esperado:
        violacoes += 1
check('exatamente uma transferência vence; o admin não atropela', violacoes == 0,
      f"{violacoes} violação(ões); venceu dono {placar['dono']}x, admin {placar['admin']}x")
check('nenhuma exceção', not erros, erros[0][2] if erros else '')


# ── Ordem forçada ────────────────────────────────────────────────────────────
# Disparar tudo junto tende a repetir sempre a mesma intercalação. Para cobrir
# as duas, as rodadas alternam: simultâneo, primeira tarefa adiantada, segunda
# tarefa adiantada. Cada cenário reprova se alguma intercalação nunca ocorrer.
ATRASOS = [(0.0, 0.0), (0.0, 0.08), (0.08, 0.0)]


def atrasada(segundos, tarefa):
    def f():
        if segundos:
            time.sleep(segundos)
        return tarefa()
    return f


# ── C4. Dono libera enquanto outro responde (assumir-para-enviar) ───────────
print('\nC4. dono libera para a fila enquanto outro atendente tenta responder')
violacoes, erros = 0, []
placar = {'respondeu': 0, 'barrado': 0}
for rodada in range(RODADAS):
    conv = nova_conversa(dono_id=A['id'])
    a1, a2 = ATRASOS[rodada % 3]
    saida = correr([
        atrasada(a1, lambda cid=conv['id']: fila.liberar(cid, ator(A))),
        atrasada(a2, lambda cid=conv['id']: fila.garantir_dono_para_envio(cid, ator(C))),
    ])
    erros += [s for s in saida if s[0] == 'erro']
    st = estado(conv['id'])
    if saida[0][2] != 'ok':
        violacoes += 1
        continue
    if saida[1][2] == 'ok':
        placar['respondeu'] += 1
        if (st['dono'] != C['id'] or st['eventos'] != ['liberou', 'assumiu']
                or st['drv_dono'] != C['id']):
            violacoes += 1
    elif saida[1][2] == 'conflito':
        placar['barrado'] += 1
        if st['dono'] is not None or st['eventos'] != ['liberou'] or st['drv_dono'] is not None:
            violacoes += 1
    else:
        violacoes += 1
check('estado final sempre coerente com quem venceu', violacoes == 0,
      f"{violacoes} violação(ões)")
check('as DUAS intercalações foram exercitadas',
      placar['respondeu'] > 0 and placar['barrado'] > 0,
      f"C pegou depois da liberação {placar['respondeu']}x, barrado antes {placar['barrado']}x")
check('nenhuma exceção', not erros, erros[0][2] if erros else '')


# ── C5. Atendente resolve enquanto chega mensagem nova ──────────────────────
print('\nC5. atendente resolve no mesmo instante em que chega mensagem nova')
violacoes, erros = 0, []
placar = {'resolveu_antes': 0, 'mensagem_antes': 0}
for rodada in range(RODADAS):
    conv = nova_conversa()
    visto = estado(conv['id'])['last']
    marca = f'mensagem nova {rodada}'

    def entrada(fone=conv['fone'], marca=marca):
        c, _ = conversa_para_entrada(fone)
        registrar_entrada(c, marca, source='teste')
        db.session.commit()
        return SimpleNamespace(ok=True, codigo='ok', mensagem='')

    a1, a2 = ATRASOS[rodada % 3]
    saida = correr([
        atrasada(a1, lambda cid=conv['id'], v=visto: fila.resolver(cid, ator(A), visto_ate=v)),
        atrasada(a2, entrada),
    ])
    erros += [s for s in saida if s[0] == 'erro']
    with app.app_context():
        msg = WhatsAppMessage.query.filter_by(message_content=marca).first()
        conv_msg = db.session.get(Conversation, msg.conversation_id) if msg else None
        ativas = Conversation.query.filter(Conversation.contact_phone == conv['fone'],
                                           Conversation.status.in_(('aberta', 'pendente'))).count()
    if conv_msg is None or conv_msg.status not in ('aberta', 'pendente') or ativas != 1:
        violacoes += 1
    if saida[0][2] == 'ok':
        placar['resolveu_antes'] += 1
    else:
        placar['mensagem_antes'] += 1
        # Se a resolução foi recusada, tem de ter sido por causa da mensagem
        # nova, e não por outro motivo qualquer.
        if 'mensagem nova' not in (saida[0][3] or '').lower():
            violacoes += 1
check('a mensagem nova SEMPRE termina numa conversa ativa', violacoes == 0,
      f"{violacoes} violação(ões)")
check('as DUAS intercalações foram exercitadas',
      placar['resolveu_antes'] > 0 and placar['mensagem_antes'] > 0,
      f"resolveu antes {placar['resolveu_antes']}x, mensagem antes {placar['mensagem_antes']}x")
check('nenhuma exceção', not erros, erros[0][2] if erros else '')


# ── C6. EMA escala para humano enquanto atendente assume ────────────────────
print('\nC6. EMA escala para humano enquanto um atendente assume (ordem de lock)')
violacoes, erros = 0, []
deadlocks_antes = fila.ESTATISTICAS['deadlock']
for rodada in range(RODADAS):
    conv = nova_conversa()

    def escalar(cid=conv['id'], did=conv['driver_id']):
        drv = db.session.get(Driver, did)
        fila.escalar_pelo_ema(cid, drv)
        db.session.commit()
        return SimpleNamespace(ok=True, codigo='ok', mensagem='')

    a1, a2 = ATRASOS[rodada % 3]
    saida = correr([atrasada(a1, escalar),
                    atrasada(a2, lambda cid=conv['id']: fila.assumir(cid, ator(A)))])
    erros += [s for s in saida if s[0] == 'erro']
    st = estado(conv['id'])
    if (saida[1][2] != 'ok' or st['dono'] != A['id'] or st['modo'] != 'manual'
            or st['drv_modo'] != 'manual' or st['drv_dono'] != A['id']):
        violacoes += 1
repetidos = fila.ESTATISTICAS['deadlock'] - deadlocks_antes
check('estado coerente e espelho com o dono real', violacoes == 0,
      f'{violacoes} violação(ões)')
check('nenhuma exceção', not erros, erros[0][2] if erros else '')
check('ZERO deadlocks, nem mesmo mascarados pela retentativa', repetidos == 0,
      f'{repetidos} deadlock(s) repetido(s)')


# ── C7. Controles, só PostgreSQL ─────────────────────────────────────────────
if DIALETO == 'postgresql':
    print('\nC7. CONTROLE: ordem de lock invertida precisa dar deadlock')

    def inverter(cid, did):
        def f():
            db.session.execute(text("UPDATE drivers SET whatsapp_mode='manual' WHERE id=:d"), {'d': did})
            time.sleep(0.4)
            db.session.execute(text("UPDATE conversations SET handling_mode='manual' WHERE id=:c"), {'c': cid})
            db.session.commit()
            return SimpleNamespace(ok=True, codigo='ok', mensagem='')
        return f

    def ordem_da_fila_sem_retentativa(cid, did):
        def f():
            time.sleep(0.15)
            db.session.execute(text("UPDATE conversations SET handling_mode='manual' WHERE id=:c"), {'c': cid})
            db.session.execute(text("UPDATE drivers SET whatsapp_mode='manual' WHERE id=:d"), {'d': did})
            db.session.commit()
            return SimpleNamespace(ok=True, codigo='ok', mensagem='')
        return f

    detectados = 0
    for _ in range(3):
        conv = nova_conversa()
        saida = correr([inverter(conv['id'], conv['driver_id']),
                        ordem_da_fila_sem_retentativa(conv['id'], conv['driver_id'])])
        if any(s[0] == 'erro' and 'deadlock' in (s[2] or '').lower() for s in saida):
            detectados += 1
    check('SQL puro, sem retentativa: inversão gera deadlock', detectados == 3,
          f'{detectados}/3 — a ordem de lock importa de verdade')

    print('\nC7b. CONTROLE: a retentativa da fila é acionada E contada no deadlock')
    antes = fila.ESTATISTICAS['deadlock']
    conv = nova_conversa()

    def assumir_atrasado(cid=conv['id']):
        time.sleep(0.15)
        return fila.assumir(cid, ator(A))

    saida = correr([inverter(conv['id'], conv['driver_id']), assumir_atrasado])
    contados = fila.ESTATISTICAS['deadlock'] - antes
    check('fila.assumir sobrevive ao deadlock provocado',
          saida[1][0] == 'ok' and saida[1][2] == 'ok', str(saida[1][:3]))
    check('o deadlock foi contado, não engolido', contados >= 1,
          f'{contados} repetição(ões) — por isso o zero do C6 tem valor')


# ── C8. Lock de motorista longo, como o EMA durante a chamada de IA ─────────
print('\nC8. EMA segurando o motorista (IA lenta) enquanto atendente assume')
conv = nova_conversa()
DURACAO = 1.5
tempo = {}


def ema_lento(did=conv['driver_id']):
    db.session.execute(text("SELECT id FROM drivers WHERE id=:d FOR UPDATE"), {'d': did})
    time.sleep(DURACAO)
    db.session.commit()
    return SimpleNamespace(ok=True, codigo='ok', mensagem='')


def atendente(cid=conv['id']):
    time.sleep(0.2)
    t0 = time.time()
    r = fila.assumir(cid, ator(A))
    tempo['assumir'] = time.time() - t0
    return r


saida = correr([ema_lento, atendente])
check('assumir conclui, sem erro nem trava do processo', saida[1][0] == 'ok' and saida[1][2] == 'ok',
      str(saida[1][:3]))
if DIALETO == 'postgresql':
    check('no PostgreSQL o clique espera a IA terminar (latência, não falha)',
          tempo.get('assumir', 0) >= DURACAO - 0.4,
          f"assumir levou {tempo.get('assumir', 0):.2f}s com o motorista travado por {DURACAO}s")
else:
    print(f"  [INFO] SQLite ignora FOR UPDATE: assumir levou {tempo.get('assumir', 0):.2f}s")


print()
print('=' * 72)
if FALHAS:
    print(f'RESULTADO ({DIALETO}): {len(FALHAS)} FALHA(S)')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)
print(f'RESULTADO ({DIALETO}): TUDO OK')
