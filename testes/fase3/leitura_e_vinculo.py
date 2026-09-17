"""
Fase 3 — leitura, vínculo de mensagens, tempo real e busca.

Nada sai da máquina: a IA do EMA, o envio pela Evolution e a Twilio são
interceptados. Como rodar: ver testes/fase3/LEIAME.md.
"""
import hashlib
import os
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes da app

CHAVE_EVO = 'chave-teste-fase3'
os.environ['EVOLUTION_API_KEY'] = CHAVE_EVO
TOKEN_EVO = hashlib.sha256(CHAVE_EVO.encode()).hexdigest()
os.environ['TWILIO_ACCOUNT_SID'] = 'ACtestefase3000000000000000000000'
os.environ['TWILIO_AUTH_TOKEN'] = 'token-teste-fase3'
os.environ['TWILIO_WHATSAPP_NUMBER'] = 'whatsapp:+14155550100'
os.environ['TWILIO_WEBHOOK_URL'] = 'http://localhost/conversas/webhook'

import logging  # noqa: E402
logging.disable(logging.WARNING)

from flask import render_template_string                             # noqa: E402
from flask_login import login_user                                   # noqa: E402

from infraestrutura_critica.main import app                         # noqa: E402
from infraestrutura_critica.app import db, socketio                 # noqa: E402
from infraestrutura_critica.models import (                         # noqa: E402
    Conversation, Driver, EmaSession, User, WhatsAppMessage,
)
from atendimento_conversas.utils import fila, twilio_client         # noqa: E402
from atendimento_conversas.utils.conversas_service import (         # noqa: E402
    conversa_para_entrada, marcar_lidas, registrar_entrada,
)
import prospeccao_captacao_motorista.utils.ema_agent as ema_utils   # noqa: E402
import infraestrutura_critica.utils.evolution_api as evo            # noqa: E402

app.config['TESTING'] = True
DIALETO = _ambiente.conferir_dialeto(app, db)

BOT = {'ia': 0, 'envios': 0}
ema_utils.process_message = lambda *a, **k: (BOT.__setitem__('ia', BOT['ia'] + 1), 'resposta do bot')[1]
evo.send_text = lambda *a, **k: (BOT.__setitem__('envios', BOT['envios'] + 1), True)[1]

FALHAS = []


def check(label, ok, det=''):
    print(f"  [{'OK ' if ok else 'FALHA'}] {label}" + (f"  ({det})" if det else ''))
    if not ok:
        FALHAS.append(label)


def usuario(nome, papel='operador'):
    with app.app_context():
        u = User.query.filter_by(username=nome).first()
        if u is None:
            u = User(username=nome, email=f'{nome}@teste.local', password_hash='x',
                     role=papel, active=True)
            db.session.add(u)
            db.session.commit()
        return SimpleNamespace(id=u.id, role=u.role, nome=u.username)


def navegador(u):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
        s['_csrf_token'] = f'csrf-{u.nome}'
    c.cab = {'X-CSRFToken': f'csrf-{u.nome}'}
    return c


def fone():
    return f"55119{time.time_ns() % 100000000:08d}"


def motorista(nome='Motorista Teste', com_sessao=False, telefone=None):
    with app.app_context():
        f = telefone or fone()
        d = Driver(name=nome, phone=f, whatsapp_mode='auto', active=True)
        db.session.add(d)
        db.session.flush()
        if com_sessao:
            db.session.add(EmaSession(driver_id=d.id, status='active', started_at=datetime.utcnow()))
        db.session.commit()
        return SimpleNamespace(id=d.id, fone=f)


def conversa(drv=None, telefone=None, modo='auto', nome=None):
    with app.app_context():
        f = drv.fone if drv else (telefone or fone())
        c = Conversation(contact_phone=f, driver_id=drv.id if drv else None, status='aberta',
                         handling_mode=modo, contact_name=nome, last_activity_at=datetime.utcnow())
        db.session.add(c)
        db.session.commit()
        return c.id


def webhook_ema(cliente, telefone, texto, mid):
    return cliente.post(f'/ema/webhook?token={TOKEN_EVO}', json={'event': 'messages.upsert', 'data': {
        'key': {'remoteJid': f'{telefone}@s.whatsapp.net', 'fromMe': False, 'id': mid},
        'messageType': 'conversation', 'message': {'conversation': texto}}})


def entrada_direta(telefone, texto):
    with app.app_context():
        c, _ = conversa_para_entrada(telefone)
        m = registrar_entrada(c, texto, source='teste')
        db.session.commit()
        return c.id, m.id


OP1, OP2 = usuario('f3_op1'), usuario('f3_op2')
CLIENTE = usuario('f3_cliente', 'cliente')
NAV1, NAV2 = navegador(OP1), navegador(OP2)
WEB = app.test_client()

print()
print('=' * 72)
print(f'FASE 3 — LEITURA, VÍNCULO, TEMPO REAL E BUSCA  |  banco: {DIALETO}')
print('=' * 72)


# ── L1. O defeito confirmado: reentrega depois de marcar como lida ──────────
print('\nL1. reentrega da Evolution depois de a conversa ser lida (defeito das Fases 1 e 2)')
d = motorista(com_sessao=True)
cid = conversa(d)
BOT.update(ia=0, envios=0)
webhook_ema(WEB, d.fone, 'oi', 'F3-L1-MSG')
check('controle: o bot respondeu a primeira entrega', BOT['envios'] == 1, str(BOT))

with app.app_context():
    fila.assumir(cid, OP1)
NAV1.get(f'/conversas/api/conversas/{cid}')
NAV1.get(f'/contracting/api/conversations/{d.id}')   # rota antiga, que também marcava
with app.app_context():
    fila.devolver_ao_bot(cid, OP1)
    m = WhatsAppMessage.query.filter_by(external_message_id='F3-L1-MSG').first()
    st, lida = m.status, m.read_at is not None
check('abrir marcou read_at', lida)
check('status continua processado, e não "lido"', st == 'processado', st)

webhook_ema(WEB, d.fone, 'oi', 'F3-L1-MSG')   # reentrega idêntica
check('reentrega NÃO faz o bot responder de novo', BOT['envios'] == 1 and BOT['ia'] == 1, str(BOT))


# ── L2. Quem marca como lida ─────────────────────────────────────────────────
print('\nL2. só o responsável marca como lida')
tel = fone()
cid, _ = entrada_direta(tel, 'primeira')
entrada_direta(tel, 'segunda')
with app.app_context():
    db.session.get(Conversation, cid).handling_mode = 'manual'
    db.session.commit()

NAV2.get(f'/conversas/api/conversas/{cid}')
det = NAV2.get(f'/conversas/api/conversas/{cid}').get_json()
check('colega sem posse abre e as duas continuam não lidas', det['conversa']['nao_lidas'] == 2,
      f"nao_lidas={det['conversa']['nao_lidas']}")
cont = NAV1.get('/conversas/api/contadores').get_json()
check('conversa livre fora do EMA conta no alerta da fila', cont['fila_humana'] >= 1, str(cont))

NAV1.post(f'/conversas/api/conversas/{cid}/assumir', headers=NAV1.cab)
det = NAV1.get(f'/conversas/api/conversas/{cid}').get_json()
check('responsável abre: zero não lidas', det['conversa']['nao_lidas'] == 0,
      f"nao_lidas={det['conversa']['nao_lidas']}")
with app.app_context():
    status = {m.status for m in WhatsAppMessage.query.filter_by(conversation_id=cid, direction='inbound')}
check('status das mensagens intocado', status == {'recebido'}, str(status))

entrada_direta(tel, 'terceira')
cont = NAV1.get('/conversas/api/contadores').get_json()
lst = NAV1.get('/conversas/api/conversas?aba=minhas').get_json()
item = next((c for c in lst['conversas'] if c['id'] == cid), {})
check('mensagem nova volta a contar como não lida', item.get('nao_lidas') == 1, str(item.get('nao_lidas')))
check('e acende o alerta de "minhas com não lidas"', cont['minhas_com_nao_lidas'] >= 1, str(cont))


# ── L3. Marca só o que foi exibido ───────────────────────────────────────────
print('\nL3. marcar_lidas marca só os ids exibidos')
tel = fone()
cid, m1 = entrada_direta(tel, 'vista')
_, m2 = entrada_direta(tel, 'chegou depois da tela carregar')
with app.app_context():
    n = marcar_lidas(cid, [m1])
    db.session.commit()
    r1 = db.session.get(WhatsAppMessage, m1).read_at
    r2 = db.session.get(WhatsAppMessage, m2).read_at
check('a exibida foi marcada e a outra não', n == 1 and r1 is not None and r2 is None,
      f'marcadas={n}')


# ── V1. Resposta do bot aparece na conversa ──────────────────────────────────
print('\nV1. resposta do bot entra na conversa')
d = motorista(com_sessao=True)
cid = conversa(d)
webhook_ema(WEB, d.fone, 'quero cadastrar', f'F3-V1-{time.time_ns()}')
with app.app_context():
    saidas = WhatsAppMessage.query.filter_by(driver_id=d.id, direction='outbound').all()
check('a resposta do bot ganhou conversation_id', bool(saidas) and all(s.conversation_id == cid for s in saidas),
      f'{[(s.id, s.conversation_id) for s in saidas]}')
det = NAV1.get(f'/conversas/api/conversas/{cid}').get_json()
direcoes = [m['direction'] for m in det['mensagens']]
check('a conversa mostra a pergunta e a resposta', direcoes.count('inbound') == 1 and direcoes.count('outbound') == 1,
      str(direcoes))


# ── V2. Oferta de frete sem conversa ─────────────────────────────────────────
print('\nV2. oferta de frete a motorista sem conversa')
d = motorista('Motorista Oferta')
with app.app_context():
    antes = Conversation.query.count()
    db.session.add(WhatsAppMessage(driver_id=d.id, phone_number=d.fone, message_content='oferta nova',
                                   direction='outbound', source='freight', status='enviado'))
    db.session.add(WhatsAppMessage(driver_id=d.id, phone_number=d.fone, message_content='oferta velha',
                                   direction='outbound', source='freight', status='enviado',
                                   sent_at=datetime.utcnow() - timedelta(days=9)))
    db.session.commit()
    depois = Conversation.query.count()
    sem = WhatsAppMessage.query.filter_by(driver_id=d.id, conversation_id=None).count()
check('oferta não abre conversa', depois == antes, f'{antes} -> {depois}')
check('oferta fica sem conversa por enquanto', sem == 2)

cid, _ = entrada_direta(d.fone, 'tenho interesse')
with app.app_context():
    nova = WhatsAppMessage.query.filter_by(driver_id=d.id, message_content='oferta nova').first()
    velha = WhatsAppMessage.query.filter_by(driver_id=d.id, message_content='oferta velha').first()
check('ao responder, a oferta recente entra na conversa', nova.conversation_id == cid)
check('oferta de mais de 7 dias não entra', velha.conversation_id is None)


# ── V3. Mensagem para quem já tem conversa ───────────────────────────────────
print('\nV3. mensagem enviada a quem já tem conversa ativa')
d = motorista()
cid = conversa(d)
with app.app_context():
    m = WhatsAppMessage(driver_id=d.id, phone_number=d.fone, message_content='aviso de coleta',
                        direction='outbound', source='freight', status='enviado')
    db.session.add(m)
    db.session.commit()
    vinculada = m.conversation_id
check('vinculada na hora', vinculada == cid, f'{vinculada} esperado {cid}')

tel = fone()
cid_tel = conversa(telefone=tel)
with app.app_context():
    m = WhatsAppMessage(phone_number=tel, message_content='sem motorista, só telefone',
                        direction='outbound', source='operator', status='enviado')
    db.session.add(m)
    db.session.commit()
    vinculada = m.conversation_id
check('sem motorista, vincula pelo telefone', vinculada == cid_tel)


# ── V4. Tempo real: um evento por mensagem, de qualquer canal ────────────────
print('\nV4. tempo real: um aviso por mensagem, de qualquer canal')
sock = socketio.test_client(app, flask_test_client=NAV2)
sock.emit('join_notifications')
sock.get_received()


def eventos_de(cid_alvo):
    return [e['args'][0] for e in sock.get_received()
            if e['name'] == 'conversa_atualizada' and e['args'][0].get('conversation_id') == cid_alvo]


tel = fone()
cid, _ = entrada_direta(tel, 'entrada direta')
ev = eventos_de(cid)
check('entrada gera exatamente 1 aviso com nova_mensagem', len(ev) == 1 and ev[0]['nova_mensagem'] is True,
      str(ev)[:160])

with app.app_context():
    db.session.add(WhatsAppMessage(phone_number=tel, message_content='saída', direction='outbound',
                                   source='operator', status='enviado'))
    db.session.commit()
ev = eventos_de(cid)
check('saída gera 1 aviso SEM nova_mensagem', len(ev) == 1 and ev[0]['nova_mensagem'] is False, str(ev)[:160])

d = motorista(com_sessao=True)
cid = conversa(d)
sock.get_received()
webhook_ema(WEB, d.fone, 'pelo EMA', f'F3-V4-{time.time_ns()}')
ev = eventos_de(cid)
entradas = [e for e in ev if e['nova_mensagem']]
check('webhook do EMA: 1 aviso de entrada, sem duplicata', len(entradas) == 1,
      f'{len(entradas)} aviso(s) de entrada, {len(ev)} no total')

tel = fone()
cid = conversa(telefone=tel, modo='manual')
sock.get_received()
params = {'From': f'whatsapp:+{tel}', 'Body': 'pela Twilio', 'MessageSid': f'SMF3{time.time_ns()}', 'NumMedia': '0'}
r = WEB.post('/conversas/webhook', data=params, headers={
    'X-Twilio-Signature': twilio_client.compute_signature('http://localhost/conversas/webhook', params)})
ev = eventos_de(cid)
check('webhook da Twilio: 1 aviso de entrada, sem duplicata',
      r.status_code == 200 and len([e for e in ev if e['nova_mensagem']]) == 1,
      f'HTTP {r.status_code}, {len(ev)} aviso(s)')
check('o aviso não carrega texto da mensagem',
      all('texto' not in e and 'message_content' not in e for e in ev))
sock.disconnect()


# ── B. Busca ─────────────────────────────────────────────────────────────────
print('\nB. busca por nome e telefone em qualquer formato')
tel = '5511988884444'
d = motorista('Carlos Pereira', telefone=tel)
cid = conversa(d, nome='Transportes Silva')


def achou(termo):
    lst = NAV1.get('/conversas/api/conversas?aba=todas&busca=' + termo).get_json()['conversas']
    return any(c['id'] == cid for c in lst)


check('telefone com máscara: (11) 98888-4444', achou('(11) 98888-4444'))
check('só dígitos: 988884444', achou('988884444'))
check('nome do motorista, minúsculo: pereira', achou('pereira'))
check('nome do motorista, maiúsculo: PEREIRA', achou('PEREIRA'))
check('nome do contato: silva', achou('silva'))
check('termo sem relação não traz a conversa', not achou('zzzinexistente'))


# ── N. Alerta global ─────────────────────────────────────────────────────────
print('\nN. alerta global só para atendentes')
with app.test_request_context('/'):
    with app.app_context():
        login_user(db.session.get(User, OP1.id))
        html_op = render_template_string('{% extends "dashboard_base.html" %}')
with app.test_request_context('/'):
    with app.app_context():
        login_user(db.session.get(User, CLIENTE.id))
        try:
            html_cli = render_template_string('{% extends "dashboard_base.html" %}')
        except Exception as exc:
            html_cli = f'ERRO {exc}'
check('operador recebe o script de alerta', 'conversas_alerta.js' in html_op)
check('cliente não recebe o script de alerta', 'conversas_alerta.js' not in html_cli)
r = app.test_client()
with r.session_transaction() as s:
    s['_user_id'] = str(CLIENTE.id)
    s['_fresh'] = True
check('cliente não lê os contadores', r.get('/conversas/api/contadores').status_code == 403)


print()
print('=' * 72)
if FALHAS:
    print(f'RESULTADO ({DIALETO}): {len(FALHAS)} FALHA(S)')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)
print(f'RESULTADO ({DIALETO}): TUDO OK')
