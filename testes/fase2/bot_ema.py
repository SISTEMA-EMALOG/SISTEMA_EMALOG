"""
Fase 2 — o bot EMA nunca fala numa conversa assumida. Testado pela porta
real: POST /ema/webhook com payload da Evolution e token válido.

A IA e o envio são interceptados; nada sai da máquina.
"""
import hashlib
import os
import sys
import time
from datetime import datetime
from types import SimpleNamespace

import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes da app

CHAVE = 'chave-de-teste-ema'
os.environ['EVOLUTION_API_KEY'] = CHAVE
TOKEN = hashlib.sha256(CHAVE.encode()).hexdigest()

import logging  # noqa: E402
logging.disable(logging.WARNING)

from infraestrutura_critica.main import app                         # noqa: E402
from infraestrutura_critica.app import db                           # noqa: E402
from infraestrutura_critica.models import (                         # noqa: E402
    Conversation, ConversationEvent, Driver, EmaSession, User, WhatsAppMessage,
)
from atendimento_conversas.utils import fila                        # noqa: E402
import prospeccao_captacao_motorista.utils.ema_agent as ema_utils   # noqa: E402
import infraestrutura_critica.utils.evolution_api as evo            # noqa: E402
from sqlalchemy import text                                         # noqa: E402

app.config['TESTING'] = True
DIALETO = _ambiente.conferir_dialeto(app, db)

CHAMADAS = {'ia': 0, 'envios': []}


def ia_falsa(session, driver, texto, media_path=None):
    CHAMADAS['ia'] += 1
    return 'resposta do bot'


def envio_falso(fone, msg):
    CHAMADAS['envios'].append((fone, msg))
    return True


ema_utils.process_message = ia_falsa
evo.send_text = envio_falso

FALHAS = []


def check(label, ok, det=''):
    print(f"  [{'OK ' if ok else 'FALHA'}] {label}" + (f"  ({det})" if det else ''))
    if not ok:
        FALHAS.append(label)


def zerar():
    CHAMADAS['ia'] = 0
    CHAMADAS['envios'].clear()


with app.app_context():
    u = User.query.filter_by(username='ema_teste_atendente').first()
    if u is None:
        u = User(username='ema_teste_atendente', email='emat@teste.local',
                 password_hash='x', role='operador', active=True)
        db.session.add(u)
        db.session.commit()
    ATENDENTE = SimpleNamespace(id=u.id, role=u.role)


def cenario(com_sessao=True, com_conversa=True):
    """Motorista em modo automático, com sessão EMA ativa e conversa aberta."""
    with app.app_context():
        fone = f"55119{time.time_ns() % 100000000:08d}"
        d = Driver(name=f'Motorista EMA {fone[-4:]}', phone=fone, whatsapp_mode='auto', active=True)
        db.session.add(d)
        db.session.flush()
        if com_sessao:
            db.session.add(EmaSession(driver_id=d.id, status='active',
                                      started_at=datetime.utcnow()))
        cid = None
        if com_conversa:
            c = Conversation(contact_phone=fone, driver_id=d.id, status='aberta',
                             handling_mode='auto', last_activity_at=datetime.utcnow())
            db.session.add(c)
            db.session.flush()
            cid = c.id
        db.session.commit()
        return {'fone': fone, 'driver_id': d.id, 'conv_id': cid}


def mensagem(cliente, fone, texto):
    mid = f'EVT{time.time_ns()}'
    corpo = {'event': 'messages.upsert', 'data': {
        'key': {'remoteJid': f'{fone}@s.whatsapp.net', 'fromMe': False, 'id': mid},
        'messageType': 'conversation',
        'message': {'conversation': texto},
    }}
    r = cliente.post(f'/ema/webhook?token={TOKEN}', json=corpo)
    return r, mid


cliente = app.test_client()

print()
print('=' * 72)
print(f'FASE 2 — BOT EMA x CONVERSA ASSUMIDA  |  banco: {DIALETO}')
print('=' * 72)

# ── E1. Controle ─────────────────────────────────────────────────────────────
print('\nE1. CONTROLE: conversa livre, motorista em automático, sessão ativa')
cen = cenario()
zerar()
r, mid = mensagem(cliente, cen['fone'], 'oi, quero continuar o cadastro')
check('webhook aceita', r.status_code == 200, f'HTTP {r.status_code}')
check('o bot RESPONDE — o cenário aciona a IA de verdade',
      CHAMADAS['ia'] == 1 and len(CHAMADAS['envios']) == 1,
      f"ia={CHAMADAS['ia']} envios={len(CHAMADAS['envios'])}")
with app.app_context():
    m = WhatsAppMessage.query.filter_by(external_message_id=mid).first()
    check('mensagem vinculada à conversa existente', m is not None and m.conversation_id == cen['conv_id'],
          f"conversation_id={getattr(m, 'conversation_id', None)} esperado={cen['conv_id']}")

# ── E2. Conversa assumida ────────────────────────────────────────────────────
print('\nE2. atendente assumiu a conversa: o bot se cala')
cen = cenario()
with app.app_context():
    res = fila.assumir(cen['conv_id'], ATENDENTE)
check('atendente assumiu', res.ok, res.codigo)
zerar()
r, mid = mensagem(cliente, cen['fone'], 'alguém aí?')
check('webhook aceita', r.status_code == 200, f'HTTP {r.status_code}')
check('o bot NÃO chama a IA nem envia nada',
      CHAMADAS['ia'] == 0 and not CHAMADAS['envios'],
      f"ia={CHAMADAS['ia']} envios={len(CHAMADAS['envios'])}")
with app.app_context():
    m = WhatsAppMessage.query.filter_by(external_message_id=mid).first()
    check('mensagem gravada, vinculada e marcada como processada',
          m is not None and m.conversation_id == cen['conv_id'] and m.status == 'processado',
          f"conv={getattr(m, 'conversation_id', None)} status={getattr(m, 'status', None)}")

# ── E3. Espelho desatualizado ────────────────────────────────────────────────
print('\nE3. conversa assumida, mas o cadastro do motorista voltou para automático')
cen = cenario()
with app.app_context():
    fila.assumir(cen['conv_id'], ATENDENTE)
    db.session.execute(text("UPDATE drivers SET whatsapp_mode='auto', whatsapp_assigned_to=NULL WHERE id=:d"),
                       {'d': cen['driver_id']})
    db.session.commit()
    modo = db.session.get(Driver, cen['driver_id']).whatsapp_mode
check('pré-condição: motorista em automático', modo == 'auto', modo)
zerar()
r, mid = mensagem(cliente, cen['fone'], 'e agora?')
check('o bot continua calado, pela conversa',
      CHAMADAS['ia'] == 0 and not CHAMADAS['envios'],
      f"ia={CHAMADAS['ia']} envios={len(CHAMADAS['envios'])}")

# ── E4. Contato novo ─────────────────────────────────────────────────────────
print('\nE4. mensagem de número sem conversa abre conversa e vincula')
cen = cenario(com_sessao=False, com_conversa=False)
zerar()
r, mid = mensagem(cliente, cen['fone'], 'primeiro contato')
with app.app_context():
    m = WhatsAppMessage.query.filter_by(external_message_id=mid).first()
    c = db.session.get(Conversation, m.conversation_id) if m and m.conversation_id else None
check('conversa aberta e mensagem vinculada', c is not None and c.status == 'aberta'
      and c.contact_phone == cen['fone'], f"conversa={getattr(c, 'id', None)}")
check('conversa vinculada ao motorista cadastrado', c is not None and c.driver_id == cen['driver_id'])

# ── E5. Devolver ao bot religa ───────────────────────────────────────────────
print('\nE5. atendente devolve a conversa ao EMA: o bot volta a responder')
cen = cenario()
with app.app_context():
    fila.assumir(cen['conv_id'], ATENDENTE)
    res = fila.devolver_ao_bot(cen['conv_id'], ATENDENTE)
check('devolução aceita', res.ok, res.codigo)
zerar()
r, mid = mensagem(cliente, cen['fone'], 'voltei')
check('o bot responde de novo', CHAMADAS['ia'] == 1 and len(CHAMADAS['envios']) == 1,
      f"ia={CHAMADAS['ia']} envios={len(CHAMADAS['envios'])}")

print()
print('=' * 72)
if FALHAS:
    print(f'RESULTADO ({DIALETO}): {len(FALHAS)} FALHA(S)')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)
print(f'RESULTADO ({DIALETO}): TUDO OK')
