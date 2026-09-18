"""
Fase 5 — o disparo para um número (fatia mínima da campanha) e o round-trip.

Prova o que torna o chatbot testável de ponta a ponta:
  - iniciar_para_numero abre a BotSession, manda a mensagem [1] de entrada e a
    grava como WhatsAppMessage source='bot' numa conversa da Central;
  - depois do disparo, o motorista respondendo (via webhook) faz a máquina
    avançar para a coleta mínima (pede o nome) — o círculo completo;
  - recusa número inválido, opt-out e número que já tem sessão ativa;
  - o blueprint 'chatbot' e suas rotas estão registrados.

send_text é trocado por um falso (não toca a Evolution). Trava de _ambiente.py.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes da app

import logging  # noqa: E402
logging.disable(logging.WARNING)

from infraestrutura_critica.main import app                          # noqa: E402
from infraestrutura_critica.app import db                            # noqa: E402
from infraestrutura_critica.models import (                          # noqa: E402
    BotSession, Conversation, WhatsAppMessage, OptOut,
    BOT_SESSAO_ATIVA, BOT_ESTADO_ENTRADA,
)
from chatbot_regras.utils import campanha, mensagens                 # noqa: E402
from atendimento_conversas.utils.phone import normalize_contact_key  # noqa: E402

import infraestrutura_critica.utils.evolution_api as _evo            # noqa: E402
ENVIADOS = []
_evo.send_text = lambda phone, msg: (ENVIADOS.append((phone, msg)) or True)

from prospeccao_captacao_motorista.ema_agent import _process_single_message  # noqa: E402

app.config['TESTING'] = True
DIALETO = _ambiente.conferir_dialeto(app, db)

FALHAS = []
SUFIXO = uuid.uuid4().hex[:8]


def check(label, ok, det=''):
    print(f"  [{'OK ' if ok else 'FALHA'}] {label}" + (f"  ({det})" if det else ''))
    if not ok:
        FALHAS.append(label)


def tel(final):
    return f'5511{SUFIXO[:5]}{final:04d}'


def msg_webhook(phone, texto):
    return {
        'key': {'id': uuid.uuid4().hex, 'remoteJid': f'{phone}@s.whatsapp.net',
                'fromMe': False},
        'message': {'conversation': texto},
        'messageType': 'conversation',
    }


# ══════════════════════════════════════════════════════════════════════════
print(f'\n=== Fase 5 — painel/disparo do chatbot ({DIALETO}) ===\n')

# ── 1. Disparo: abre sessão, manda a entrada, grava na Central ─────────────
print('1. Iniciar para um número abre a sessão e manda a mensagem de entrada')

with app.app_context():
    p = tel(1)
    ENVIADOS.clear()
    ok, motivo, sid = campanha.iniciar_para_numero(p, criado_por=None)
    check('devolve ok/enviado', ok is True and motivo == 'enviado', motivo)

    chave = normalize_contact_key(p)
    s = BotSession.query.filter_by(telefone=chave).first()
    check('criou a BotSession ativa em entrada',
          s is not None and s.status == BOT_SESSAO_ATIVA and s.estado == BOT_ESTADO_ENTRADA,
          f'{s.status}/{s.estado}' if s else 'sem sessão')
    check('mandou a mensagem [1] de entrada', ENVIADOS == [(chave, mensagens.ENTRADA)],
          str(ENVIADOS)[:40])
    out = WhatsAppMessage.query.filter_by(
        phone_number=chave, direction='outbound', source='bot').all()
    check('gravou a entrada como WhatsAppMessage source=bot',
          len(out) == 1 and out[0].message_content == mensagens.ENTRADA,
          f'{len(out)} saídas')
    check('vinculou a uma conversa da Central',
          out and out[0].conversation_id is not None
          and db.session.get(Conversation, out[0].conversation_id) is not None)

# ── 2. Round-trip: o motorista responde e a máquina avança para a coleta ──
print('\n2. Respondendo pelo webhook, a máquina avança para a coleta mínima')

with app.app_context():
    p = tel(1)                                    # mesma sessão do passo 1
    ENVIADOS.clear()
    _process_single_message(msg_webhook(p, 'Sim, trabalho com carga'))
    s = BotSession.query.filter_by(telefone=normalize_contact_key(p)).first()
    check('a máquina avançou para coleta_minima', s.estado == 'coleta_minima', s.estado)
    check('o bot pediu o nome', ENVIADOS == [(p, mensagens.COLETA_NOME)], str(ENVIADOS)[:40])

# ── 3. Não reenvia para número com sessão ativa ───────────────────────────
print('\n3. Número com sessão ativa não é reenviado')

with app.app_context():
    p = tel(1)
    ENVIADOS.clear()
    ok, motivo, _sid = campanha.iniciar_para_numero(p)
    check('recusa com ja_ativa', ok is False and motivo == 'ja_ativa', motivo)
    check('não mandou nada', ENVIADOS == [])

# ── 4. Opt-out e número inválido são recusados ────────────────────────────
print('\n4. Opt-out e número inválido são recusados')

with app.app_context():
    p = tel(4)
    db.session.add(OptOut(telefone=normalize_contact_key(p), origem='manual', motivo='teste'))
    db.session.commit()
    ENVIADOS.clear()
    ok, motivo, _sid = campanha.iniciar_para_numero(p)
    check('recusa opt-out', ok is False and motivo == 'optout', motivo)

    ok, motivo, _sid = campanha.iniciar_para_numero('abc')
    check('recusa número inválido', ok is False and motivo == 'numero_invalido', motivo)
    check('nada foi enviado nos dois casos', ENVIADOS == [])

# ── 4b. 9º dígito: dispara com o 9, responde sem o 9, e ainda acha a sessão ─
print('\n4b. Resposta na forma sem o 9º dígito ainda encontra a sessão')

with app.app_context():
    tail = f'{int(SUFIXO, 16) % 100000000:08d}'   # 8 dígitos determinísticos
    com9 = '5511' + '9' + tail                    # 13 dígitos, com o 9 (o disparo)
    sem9 = '5511' + tail                          # 12 dígitos, sem o 9 (a resposta)
    ENVIADOS.clear()
    ok, motivo, _sid = campanha.iniciar_para_numero(com9)
    check('disparou para a forma com o 9', ok is True, motivo)

    ENVIADOS.clear()
    _process_single_message(msg_webhook(sem9, 'sim, trabalho'))   # responde sem o 9
    s = BotSession.query.filter_by(telefone=normalize_contact_key(com9)).first()
    check('a máquina achou a sessão e avançou (não ficou muda)',
          s is not None and s.estado == 'coleta_minima', s.estado if s else 'sem sessão')
    check('o bot respondeu pedindo o nome', ENVIADOS == [(sem9, mensagens.COLETA_NOME)],
          str(ENVIADOS)[:40])


# ── 5. O blueprint e as rotas estão registrados ───────────────────────────
print('\n5. Blueprint do chatbot registrado')

endpoints = {r.endpoint for r in app.url_map.iter_rules()}
for ep in ('chatbot.index', 'chatbot.iniciar', 'chatbot.encerrar'):
    check(f'rota {ep} registrada', ep in endpoints)

# ══════════════════════════════════════════════════════════════════════════
print()
if FALHAS:
    print(f'FALHOU em {len(FALHAS)} verificação(ões), no {DIALETO}:')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)

print(f'Todas as verificações passaram no {DIALETO}.')
