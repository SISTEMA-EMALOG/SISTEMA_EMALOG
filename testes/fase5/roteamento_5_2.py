"""
Fase 5, etapa 5.2 — o gancho de roteamento (passo 5) em _process_single_message.

Isto prova a COSTURA entre o webhook e a máquina de estados — o pedaço que faltava
e por causa do qual "nenhuma etapa era acionada". A máquina em si já é provada por
maquina_5_2.py; aqui o foco é o que o roteador faz com o resultado dela:

  - Número novo com sessão ativa → a mensagem é entregue à máquina, cai em handoff,
    e a CONVERSA entra na fila da Central (handling_mode='manual'). O bot não manda
    texto no handoff genérico, então nenhuma WhatsAppMessage de saída é criada.
  - Quando a máquina devolve texto (opt-out, boa viagem), o roteador ENVIA por
    send_text e grava a resposta como WhatsAppMessage direction='outbound',
    source='bot' — sem isso a resposta some da Central (defeito 3.1). E, sem
    handoff, a conversa continua no bot.
  - Telefone sem sessão ativa cai no ramo "nada": nenhuma resposta do bot, a
    conversa não é escalada — os quatro caminhos antigos seguem intactos.

send_text é trocado por um falso (não toca a Evolution). Trava de _ambiente.py.

Como rodar: ver testes/fase5/LEIAME.md.
"""
import os
import sys
import uuid
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes da app

import logging  # noqa: E402
logging.disable(logging.WARNING)

from infraestrutura_critica.main import app                          # noqa: E402
from infraestrutura_critica.app import db                            # noqa: E402
from infraestrutura_critica.models import (                          # noqa: E402
    BotSession, Conversation, Driver, WhatsAppMessage,
    BOT_SESSAO_ATIVA, BOT_SESSAO_HANDOFF, BOT_SESSAO_ENCERRADA, BOT_SESSAO_OPTOUT,
)
from chatbot_regras import criar_sessao_bot                          # noqa: E402
from chatbot_regras.utils import mensagens                           # noqa: E402
from atendimento_conversas.utils.phone import normalize_contact_key  # noqa: E402

# Troca o envio real por um falso ANTES de acionar o webhook. O gancho importa
# send_text de dentro da função (from ... import send_text), então basta
# substituir o atributo no módulo de origem.
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


def msg(phone, texto):
    """Payload de mensagem recebida no formato Evolution (conversation)."""
    return {
        'key': {'id': uuid.uuid4().hex, 'remoteJid': f'{phone}@s.whatsapp.net',
                'fromMe': False},
        'message': {'conversation': texto},
        'messageType': 'conversation',
    }


def saidas_bot(phone):
    return WhatsAppMessage.query.filter_by(
        phone_number=phone, direction='outbound', source='bot').all()


def conversa_do_telefone(phone):
    inbound = (WhatsAppMessage.query
               .filter_by(phone_number=phone, direction='inbound')
               .order_by(WhatsAppMessage.id.desc()).first())
    if inbound is None or inbound.conversation_id is None:
        return None
    return db.session.get(Conversation, inbound.conversation_id)


def driver_completo_em_viagem(final):
    """Cadastro COMPLETO (todos os required de FIELD_DEFS + CNH válida) e em
    viagem. Precisa ser completo, senão a máquina para em identificacao (handoff)
    e nunca chega ao estado [4]. Mesmos campos do driver_completo da maquina_5_2."""
    d = Driver(
        name=f'Motorista Viagem {SUFIXO} {final}',
        cpf=f'{final:03d}.{SUFIXO[:3]}.{SUFIXO[3:6]}-00',
        rg=f'RG{SUFIXO[:6]}',
        birth_date=date(1990, 1, 1),
        phone=tel(final),
        cep='13000-000',
        number='100',
        cnh_expiry=date(2035, 1, 1),
        truck_type='truck',
        has_tracker=False,
        vehicle_plate='ABC1D23',
        vehicle_model='Volvo FH',
        vehicle_year=2020,
        cnh_document='cnh.jpg',
        address_proof='residencia.jpg',
        crlv_document='crlv.jpg',
        availability_status='em_frete',
    )
    db.session.add(d)
    db.session.commit()
    return d


# ══════════════════════════════════════════════════════════════════════════
print(f'\n=== Fase 5.2 — roteamento (passo 5) do chatbot ({DIALETO}) ===\n')

# ── 1. Número novo: webhook aciona a máquina, o bot pede o nome, sem escalar ─
print('1. Número novo: o webhook entrega à máquina, o bot pede o nome '
      '(source=bot) e ainda NÃO escala')

with app.app_context():
    p = tel(1)
    criar_sessao_bot(p)                       # simula o disparo da campanha
    ENVIADOS.clear()
    _process_single_message(msg(p, 'Sim, trabalho com carga'))

    lida = BotSession.query.filter_by(telefone=normalize_contact_key(p)).first()
    check('a máquina foi acionada (coleta_minima, ainda ativa)',
          lida is not None and lida.estado == 'coleta_minima'
          and lida.status == BOT_SESSAO_ATIVA,
          f'{lida.estado}/{lida.status}' if lida else 'sem sessão')
    saidas = saidas_bot(p)
    check('o bot pediu o nome como WhatsAppMessage source=bot',
          len(saidas) == 1 and saidas[0].message_content == mensagens.COLETA_NOME,
          f'{len(saidas)} saídas')
    check('send_text foi chamado uma vez', len(ENVIADOS) == 1, str(ENVIADOS))
    conv = conversa_do_telefone(p)
    check('conversa ainda NÃO escalada (o bot está coletando)',
          conv is not None and conv.handling_mode != 'manual',
          conv.handling_mode if conv else 'sem conversa')

# ── 1b. Completada a coleta pelo webhook, a conversa é escalada à Central ──
print('\n1b. Terminada a coleta mínima, o bot transfere e a conversa é escalada')

with app.app_context():
    p = tel(1)                                # continua a MESMA sessão de (1)
    for resposta in ['Joao da Silva', '4', 'Campinas SP']:
        ENVIADOS.clear()
        _process_single_message(msg(p, resposta))

    lida = BotSession.query.filter_by(telefone=normalize_contact_key(p)).first()
    check('a sessão terminou em handoff (coleta_minima_ok)',
          lida.status == BOT_SESSAO_HANDOFF and lida.motivo_fim == 'coleta_minima_ok',
          f'{lida.status}/{lida.motivo_fim}')
    conv = conversa_do_telefone(p)
    check('agora a conversa foi escalada para a Central (manual)',
          conv is not None and conv.handling_mode == 'manual',
          conv.handling_mode if conv else 'sem conversa')
    saidas = saidas_bot(p)
    check('a última mensagem do bot foi a de transferência',
          bool(saidas) and saidas[-1].message_content == mensagens.COLETA_TRANSFERE)
    d = Driver.query.filter(Driver.phone == normalize_contact_key(p)).first()
    check('o cadastro mínimo foi preenchido (nome + veículo + UF)',
          bool(d) and d.name == 'Joao da Silva' and d.truck_type == 'truck'
          and d.city == 'Campinas' and d.state == 'SP',
          f'{d.name}/{d.truck_type}/{d.city}/{d.state}' if d else 'sem driver')

# ── 2. Máquina com texto: roteador envia e grava WhatsAppMessage source='bot' ─
print('\n2. Resposta da máquina vira send_text + WhatsAppMessage source=bot')

with app.app_context():
    p = tel(2)
    criar_sessao_bot(p)
    ENVIADOS.clear()
    _process_single_message(msg(p, 'SAIR'))

    saidas = saidas_bot(p)
    check('gravou uma WhatsAppMessage de saída do bot', len(saidas) == 1,
          f'{len(saidas)} saídas')
    check('o texto gravado é a confirmação de opt-out',
          bool(saidas) and saidas[0].message_content == mensagens.OPTOUT_CONFIRMACAO)
    check('send_text foi chamado uma vez para o telefone certo',
          ENVIADOS == [(p, mensagens.OPTOUT_CONFIRMACAO)], str(ENVIADOS))
    lida = BotSession.query.filter_by(telefone=normalize_contact_key(p)).first()
    check('a sessão virou optout', lida.status == BOT_SESSAO_OPTOUT, lida.status)
    conv = conversa_do_telefone(p)
    check('opt-out NÃO escala a conversa para humano',
          conv is not None and conv.handling_mode != 'manual',
          conv.handling_mode if conv else 'sem conversa')

# ── 3. Motorista em viagem: boa viagem enviada, sem handoff ───────────────
print('\n3. Estado [4] em viagem: boa viagem é enviada, sem escalar')

with app.app_context():
    d = driver_completo_em_viagem(3)
    p = d.phone
    criar_sessao_bot(p, driver=d)
    ENVIADOS.clear()
    _process_single_message(msg(p, 'oi'))

    saidas = saidas_bot(p)
    check('enviou a mensagem de boa viagem',
          len(saidas) == 1 and saidas[0].message_content == mensagens.VIAGEM_ATIVA_OCUPADO,
          f'{len(saidas)} saídas')
    check('vinculou o motorista na saída',
          bool(saidas) and saidas[0].driver_id == d.id)
    lida = BotSession.query.filter_by(telefone=normalize_contact_key(p)).first()
    check('a sessão encerrou (sem handoff)', lida.status == BOT_SESSAO_ENCERRADA,
          lida.status)
    conv = conversa_do_telefone(p)
    check('conversa continua no bot (não escalada)',
          conv is not None and conv.handling_mode != 'manual',
          conv.handling_mode if conv else 'sem conversa')

# ── 4. Sem sessão ativa: ramo "nada" intacto ──────────────────────────────
print('\n4. Telefone sem sessão ativa cai no ramo "nada" (caminhos antigos '
      'intactos)')

with app.app_context():
    p = tel(4)                                # nenhuma BotSession criada
    ENVIADOS.clear()
    _process_single_message(msg(p, 'oi, tem carga?'))

    check('o bot não respondeu', saidas_bot(p) == [] and ENVIADOS == [],
          f'saidas={len(saidas_bot(p))} enviados={len(ENVIADOS)}')
    check('nenhuma BotSession foi criada',
          BotSession.query.filter_by(telefone=normalize_contact_key(p)).count() == 0)
    conv = conversa_do_telefone(p)
    check('a conversa não foi escalada para humano',
          conv is not None and conv.handling_mode != 'manual',
          conv.handling_mode if conv else 'sem conversa')

# ══════════════════════════════════════════════════════════════════════════
print()
if FALHAS:
    print(f'FALHOU em {len(FALHAS)} verificação(ões), no {DIALETO}:')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)

print(f'Todas as verificações passaram no {DIALETO}.')
