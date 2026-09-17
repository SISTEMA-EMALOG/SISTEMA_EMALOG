"""
Fase 2 — prova pela camada HTTP e pelo socket, como dois navegadores fariam.

Cada atendente é um test_client do Flask com sessão de login e token CSRF
próprios. O provedor de WhatsApp é interceptado: nada sai da máquina.
"""
import os
import sys
import threading
import time
from datetime import datetime

import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes da app

import logging  # noqa: E402
logging.disable(logging.WARNING)

from infraestrutura_critica.main import app                 # noqa: E402
from infraestrutura_critica.app import db, socketio         # noqa: E402
from infraestrutura_critica.models import (                 # noqa: E402
    Conversation, Driver, User, WhatsAppMessage,
)
from atendimento_conversas.utils import providers           # noqa: E402

app.config['TESTING'] = True

DIALETO = _ambiente.conferir_dialeto(app, db)
with app.app_context():
    URL_BANCO = str(db.engine.url.render_as_string(hide_password=False))

FALHAS = []


def check(label, ok, det=''):
    print(f"  [{'OK ' if ok else 'FALHA'}] {label}" + (f"  ({det})" if det else ''))
    if not ok:
        FALHAS.append(label)


# ── Provedor interceptado ────────────────────────────────────────────────────
ENVIOS = []
SONDA = {'ativa': False, 'resultado': []}


def sondar_banco(conv_id, driver_id):
    """
    Chamada DE DENTRO do envio HTTP simulado. De uma conexão separada,
    verifica que a requisição de envio não segura transação nem lock.
    """
    import psycopg2
    dsn = URL_BANCO.replace('postgresql+psycopg2://', 'postgresql://')
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute("""SELECT pid, left(query, 120) FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND state LIKE 'idle in transaction%%'
                          AND pid <> pg_backend_pid()""")
        ociosas = cur.fetchall()
        travas = []
        for tabela, rid in (('conversations', conv_id), ('drivers', driver_id)):
            if rid is None:
                continue
            try:
                cur.execute(f"SELECT id FROM {tabela} WHERE id = %s FOR UPDATE NOWAIT", (rid,))
                conn.rollback()
            except psycopg2.errors.LockNotAvailable:
                conn.rollback()
                travas.append(tabela)
        SONDA['resultado'].append({'ociosas': ociosas, 'travas': travas})
    finally:
        conn.close()


def enviar_falso(chave, texto):
    ENVIOS.append({'chave': chave, 'texto': texto, 't': time.time()})
    if SONDA['ativa']:
        sondar_banco(SONDA['conv'], SONDA['driver'])
    time.sleep(0.05)   # latência de rede simulada
    return {'provider': 'evolution', 'external_id': f'EVO{len(ENVIOS):010d}{time.time_ns() % 100000}',
            'status': 'enviado', 'status_bruto': 'SERVER_ACK'}


providers.enviar = enviar_falso
providers.canal_configurado = lambda: 'evolution'
providers.estado_evolution = lambda: {'configurada': True, 'estado': 'open'}


# ── Preparo ──────────────────────────────────────────────────────────────────

def usuario(nome, papel='operador'):
    with app.app_context():
        u = User.query.filter_by(username=nome).first()
        if u is None:
            u = User(username=nome, email=f'{nome}@teste.local', password_hash='x',
                     role=papel, active=True)
            db.session.add(u)
            db.session.commit()
        return {'id': u.id, 'nome': u.username, 'role': u.role}


def navegador(u):
    c = app.test_client()
    token = f'csrf-{u["nome"]}'
    with c.session_transaction() as s:
        s['_user_id'] = str(u['id'])
        s['_fresh'] = True
        s['_csrf_token'] = token
    c.cab = {'X-CSRFToken': token}
    c.u = u
    return c


def nova_conversa(dono_id=None):
    with app.app_context():
        fone = f"5511{time.time_ns() % 1000000000:09d}"[:13]
        d = Driver(name=f'Mot {fone[-4:]}', phone=fone,
                   whatsapp_mode='manual' if dono_id else 'auto', whatsapp_assigned_to=dono_id)
        db.session.add(d)
        db.session.flush()
        c = Conversation(contact_phone=fone, driver_id=d.id, status='aberta',
                         handling_mode='manual' if dono_id else 'auto',
                         assigned_agent_id=dono_id, last_activity_at=datetime.utcnow())
        db.session.add(c)
        db.session.commit()
        return {'id': c.id, 'driver_id': d.id, 'fone': fone}


def simultaneo(chamadas):
    barreira = threading.Barrier(len(chamadas))
    saida = [None] * len(chamadas)

    def rodar(i, f):
        barreira.wait(timeout=30)
        try:
            r = f()
            saida[i] = (r.status_code, r.get_json(silent=True) or {})
        except Exception as exc:
            saida[i] = ('erro', {'error': repr(exc)})

    ts = [threading.Thread(target=rodar, args=(i, f)) for i, f in enumerate(chamadas)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    return saida


ATENDENTES = [usuario(f'nav{i}') for i in range(5)]
ADMIN = usuario('nav_admin', 'admin')
NAVS = [navegador(u) for u in ATENDENTES]
NAV_ADMIN = navegador(ADMIN)
A, B = NAVS[0], NAVS[1]

print()
print('=' * 72)
print(f'FASE 2 — HTTP E TEMPO REAL  |  banco: {DIALETO}')
print('=' * 72)


# ── H1. Cinco navegadores clicam Assumir juntos ─────────────────────────────
print('\nH1. cinco navegadores clicam Assumir no mesmo instante')
violacoes = 0
for _ in range(10):
    conv = nova_conversa()
    url = f"/conversas/api/conversas/{conv['id']}/assumir"
    saida = simultaneo([lambda n=n: n.post(url, headers=n.cab) for n in NAVS])
    ok = [i for i, s in enumerate(saida) if s[0] == 200]
    conflito = [s for s in saida if s[0] == 409]
    if len(ok) != 1 or len(conflito) != 4:
        violacoes += 1
        continue
    vencedor = ATENDENTES[ok[0]]['nome']
    if not all(vencedor in (s[1].get('error') or '') for s in conflito):
        violacoes += 1
check('uma resposta 200 e quatro 409 que dizem quem venceu', violacoes == 0,
      f'{violacoes} violação(ões) em 10 rodadas')


# ── H2. Dois atendentes RESPONDEM juntos uma conversa livre ─────────────────
print('\nH2. dois atendentes respondem ao mesmo tempo uma conversa livre')
violacoes, detalhes = 0, ''
for _ in range(10):
    conv = nova_conversa()
    ENVIOS.clear()
    url = f"/conversas/api/conversas/{conv['id']}/enviar"
    saida = simultaneo([
        lambda: A.post(url, json={'texto': 'resposta de A'}, headers=A.cab),
        lambda: B.post(url, json={'texto': 'resposta de B'}, headers=B.cab),
    ])
    codigos = sorted(s[0] for s in saida)
    with app.app_context():
        gravadas = WhatsAppMessage.query.filter_by(conversation_id=conv['id'],
                                                   direction='outbound').count()
    if codigos != [200, 409] or len(ENVIOS) != 1 or gravadas != 1:
        violacoes += 1
        detalhes = f'codigos={codigos} envios={len(ENVIOS)} gravadas={gravadas}'
check('só UMA mensagem sai para o motorista; o outro recebe 409', violacoes == 0,
      detalhes or '10 rodadas, sempre 1 envio')


# ── H3. Quem não é dono não envia, e nada sai ───────────────────────────────
print('\nH3. conversa com A: B tenta responder')
conv = nova_conversa()
A.post(f"/conversas/api/conversas/{conv['id']}/assumir", headers=A.cab)
ENVIOS.clear()
r = B.post(f"/conversas/api/conversas/{conv['id']}/enviar", json={'texto': 'intruso'}, headers=B.cab)
check('B recebe 409', r.status_code == 409, f'HTTP {r.status_code}')
check('nenhuma chamada ao provedor', len(ENVIOS) == 0, f'{len(ENVIOS)} envio(s)')
d = B.get(f"/conversas/api/conversas/{conv['id']}").get_json()
check('a tela de B recebe permissão de envio negada', d['permissoes']['enviar'] is False)
check('a tela de B não oferece Assumir', d['permissoes']['assumir'] is False)
r = A.post(f"/conversas/api/conversas/{conv['id']}/enviar", json={'texto': 'do dono'}, headers=A.cab)
check('A, o dono, envia normalmente', r.status_code == 200 and len(ENVIOS) == 1, f'HTTP {r.status_code}')


# ── H4. Resolver com tela velha ─────────────────────────────────────────────
print('\nH4. A resolve com a tela desatualizada, depois de chegar mensagem nova')
conv = nova_conversa()
A.post(f"/conversas/api/conversas/{conv['id']}/assumir", headers=A.cab)
visto = A.get(f"/conversas/api/conversas/{conv['id']}").get_json()['conversa']['last_activity_at']
time.sleep(0.01)
with app.app_context():
    from atendimento_conversas.utils.conversas_service import conversa_para_entrada, registrar_entrada
    c, _ = conversa_para_entrada(conv['fone'])
    registrar_entrada(c, 'chegou enquanto A olhava', source='teste')
    db.session.commit()
r = A.post(f"/conversas/api/conversas/{conv['id']}/status",
           json={'status': 'resolvida', 'visto_ate': visto}, headers=A.cab)
corpo = r.get_json() or {}
check('resolução recusada com 409', r.status_code == 409, f'HTTP {r.status_code}')
check('o motivo é a mensagem nova', 'mensagem nova' in (corpo.get('error') or '').lower(),
      corpo.get('error'))
novo_visto = A.get(f"/conversas/api/conversas/{conv['id']}").get_json()['conversa']['last_activity_at']
r = A.post(f"/conversas/api/conversas/{conv['id']}/status",
           json={'status': 'resolvida', 'visto_ate': novo_visto}, headers=A.cab)
check('depois de ler, resolve', r.status_code == 200, f'HTTP {r.status_code}')


# ── H5. Admin transfere com tela velha ──────────────────────────────────────
print('\nH5. admin tenta transferir baseado num dono que já mudou')
conv = nova_conversa()
A.post(f"/conversas/api/conversas/{conv['id']}/assumir", headers=A.cab)
A.post(f"/conversas/api/conversas/{conv['id']}/transferir",
       json={'para': ATENDENTES[1]['id']}, headers=A.cab)
r = NAV_ADMIN.post(f"/conversas/api/conversas/{conv['id']}/transferir",
                   json={'para': ATENDENTES[2]['id'], 'de': ATENDENTES[0]['id']}, headers=NAV_ADMIN.cab)
check('admin recebe 409 em vez de atropelar a transferência', r.status_code == 409,
      f"HTTP {r.status_code}: {(r.get_json() or {}).get('error')}")
with app.app_context():
    dono = db.session.get(Conversation, conv['id']).assigned_agent_id
check('a conversa continua com quem recebeu de verdade', dono == ATENDENTES[1]['id'])


# ── H6. Nenhuma transação aberta durante a chamada ao provedor ──────────────
if DIALETO == 'postgresql':
    print('\nH6. durante o envio HTTP, a requisição não segura transação nem lock')
    conv = nova_conversa()
    ENVIOS.clear()
    SONDA.update(ativa=True, conv=conv['id'], driver=conv['driver_id'], resultado=[])
    r = A.post(f"/conversas/api/conversas/{conv['id']}/enviar",
               json={'texto': 'sonda'}, headers=A.cab)
    SONDA['ativa'] = False
    res = SONDA['resultado'][0] if SONDA['resultado'] else {'ociosas': ['sonda não rodou'], 'travas': ['?']}
    check('envio concluído', r.status_code == 200, f'HTTP {r.status_code}')
    check('nenhuma conexão "idle in transaction" durante a chamada HTTP', not res['ociosas'],
          str(res['ociosas'])[:200])
    check('linhas da conversa e do motorista livres durante a chamada HTTP', not res['travas'],
          f"travadas: {res['travas']}")


# ── H7. Tempo real: B vê na hora que A assumiu ──────────────────────────────
print('\nH7. tempo real: o socket de B recebe o evento quando A assume')
conv = nova_conversa()
sock_b = socketio.test_client(app, flask_test_client=B)
check('socket de B conectado', sock_b.is_connected())
sock_b.emit('join_notifications')
sock_b.get_received()
A.post(f"/conversas/api/conversas/{conv['id']}/assumir", headers=A.cab)
eventos = [e for e in sock_b.get_received() if e['name'] == 'conversa_atualizada']
alvo = [e['args'][0] for e in eventos if e['args'][0].get('conversation_id') == conv['id']]
check('B recebeu conversa_atualizada', bool(alvo), f'{len(eventos)} evento(s) recebido(s)')
if alvo:
    p = alvo[-1]
    check('o evento diz que A é o dono', p.get('assigned_agent_id') == ATENDENTES[0]['id']
          and p.get('assigned_agent') == ATENDENTES[0]['nome'], str(p)[:160])
    check('o evento não carrega texto de mensagem', 'texto' not in p and 'message_content' not in p)
sock_b.disconnect()


# ── H8. A tela ───────────────────────────────────────────────────────────────
print('\nH8. a tela carrega com a fila e o script novo')
r = A.get('/conversas/')
html = r.get_data(as_text=True)
check('tela responde 200', r.status_code == 200, f'HTTP {r.status_code}')
check('abas Fila e Minhas presentes', 'data-aba="fila"' in html and 'data-aba="minhas"' in html)
check('script da Fase 2 com versão nova', 'conversas.js' in html and '20260917-fase2' in html)
lst = A.get('/conversas/api/conversas?aba=minhas').get_json()
check('aba Minhas lista as conversas de A', lst['aba'] == 'minhas' and lst['contagens']['minhas'] >= 1,
      f"contagem minhas={lst['contagens']['minhas']}")

print()
print('=' * 72)
if FALHAS:
    print(f'RESULTADO ({DIALETO}): {len(FALHAS)} FALHA(S)')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)
print(f'RESULTADO ({DIALETO}): TUDO OK')
