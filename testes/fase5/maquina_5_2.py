"""
Fase 5, etapa 5.2 — a máquina de estados com os estados [1] entrada,
[2] identificacao, [4] viagem_ativa, [11] encerramento e [12] handoff.

O que está sendo provado, e por que importa:

  - Número novo entra em 'entrada', responde e o bot INICIA a coleta mínima
    nativa ([3] coleta_minima, sem EMA): cria o cadastro mínimo (nome provisório)
    e pede o nome. A trilha guarda o caminho ({estado, em}). O fluxo completo da
    coleta é provado em coleta_minima_5_3.py.
  - Uma BotSession ativa por telefone: criar_sessao_bot devolve a existente em
    vez de abrir uma segunda. E, encerrada a primeira, uma nova pode abrir.
  - processar_mensagem_bot devolve None quando não há sessão ativa (o roteador
    segue para o ramo 'nada') — e NÃO cria sessão sozinho.
  - Saídas globais (seção 6): "atendente" → handoff; "SAIR" → opt-out gravado e
    status optout; mídia → handoff.
  - Estado [4]: motorista em viagem recebe "boa viagem" e a sessão encerra;
    motorista livre segue para geolocalizacao, que não existe, e vira handoff.
  - A máquina NÃO envia WhatsApp: devolve os textos em 'replies'.

Como rodar: ver testes/fase5/LEIAME.md (mesma trava de _ambiente.py da 5.1).
"""
import os
import sys
import uuid
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes da app

import logging  # noqa: E402
logging.disable(logging.WARNING)

from infraestrutura_critica.main import app                       # noqa: E402
from infraestrutura_critica.app import db                         # noqa: E402
from infraestrutura_critica.models import (                       # noqa: E402
    BotSession, Driver, OptOut,
    BOT_ESTADO_ENTRADA, BOT_SESSAO_ATIVA, BOT_SESSAO_HANDOFF,
    BOT_SESSAO_ENCERRADA, BOT_SESSAO_OPTOUT, BOT_ORIGEM_CAMPANHA,
)
from chatbot_regras import processar_mensagem_bot, criar_sessao_bot  # noqa: E402
from chatbot_regras.utils import mensagens                        # noqa: E402
from atendimento_conversas.utils.phone import normalize_contact_key  # noqa: E402

app.config['TESTING'] = True
DIALETO = _ambiente.conferir_dialeto(app, db)

FALHAS = []
SUFIXO = uuid.uuid4().hex[:8]


def check(label, ok, det=''):
    print(f"  [{'OK ' if ok else 'FALHA'}] {label}" + (f"  ({det})" if det else ''))
    if not ok:
        FALHAS.append(label)


def tel(final):
    """Telefone de teste, único por execução (13 dígitos, E.164 sem '+')."""
    return f'5511{SUFIXO[:5]}{final:04d}'


def estados_da_trilha(sessao):
    return [m.get('estado') for m in (sessao.trilha or [])]


def driver_completo(final, **extra):
    """Cria um Driver com todos os campos required de FIELD_DEFS preenchidos e
    CNH na validade (truck 'truck', sem rastreador → sem cavalinho/tracker)."""
    d = Driver(
        name=f'Motorista Completo {SUFIXO} {final}',
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
    )
    for k, v in extra.items():
        setattr(d, k, v)
    db.session.add(d)
    db.session.commit()
    return d


# ══════════════════════════════════════════════════════════════════════════
print(f'\n=== Fase 5.2 — máquina de estados do chatbot ({DIALETO}) ===\n')

# ── 1. Número novo: entra, responde, começa a coleta mínima ───────────────
print('1. Número novo entra e o bot inicia a coleta mínima (pede o nome)')

with app.app_context():
    p = tel(1)

    sessao = criar_sessao_bot(p, origem=BOT_ORIGEM_CAMPANHA)
    check('a sessão nasce em entrada', sessao.estado == BOT_ESTADO_ENTRADA, sessao.estado)
    check('a sessão nasce ativa', sessao.status == BOT_SESSAO_ATIVA, sessao.status)
    sid = sessao.id

    res = processar_mensagem_bot(p, 'Sim, trabalho com carga hoje')
    check('processar devolve um dicionário', isinstance(res, dict), type(res).__name__)
    check('a mesma sessão foi avançada', res.get('bot_session_id') == sid)
    check('NÃO caiu em handoff — está coletando',
          res.get('handoff') is False and res.get('status') == BOT_SESSAO_ATIVA,
          str(res.get('status')))
    check('o bot pergunta o nome', res.get('replies') == [mensagens.COLETA_NOME],
          str(res.get('replies'))[:60])

    lida = db.session.get(BotSession, sid)
    check('o estado é coleta_minima', lida.estado == 'coleta_minima', lida.estado)
    check('criou o cadastro mínimo vinculado à sessão', lida.driver_id is not None)
    d = db.session.get(Driver, lida.driver_id) if lida.driver_id else None
    check('nome provisório "Motorista <4 dígitos>"',
          bool(d) and d.name == f'Motorista {normalize_contact_key(p)[-4:]}',
          d.name if d else 'sem driver')
    check('a trilha vai entrada→identificacao→coleta_minima',
          estados_da_trilha(lida) == ['entrada', 'identificacao', 'coleta_minima'],
          str(estados_da_trilha(lida)))
    check('ultima_msg_em foi gravada', lida.ultima_msg_em is not None)

# ── 2. Uma sessão ativa por telefone; encerrada não trava ─────────────────
print('\n2. Uma BotSession ativa por telefone')

with app.app_context():
    p = tel(2)
    s1 = criar_sessao_bot(p)
    s2 = criar_sessao_bot(p)
    check('criar_sessao_bot devolve a ativa existente', s1.id == s2.id, f'{s1.id} vs {s2.id}')
    ativas = BotSession.query.filter_by(telefone=normalize_contact_key(p),
                                        status=BOT_SESSAO_ATIVA).count()
    check('só existe uma sessão ativa', ativas == 1, f'{ativas} ativas')

    # Encerra a primeira (opt-out é terminal) e confirma que uma nova pode abrir.
    processar_mensagem_bot(p, 'SAIR')
    s3 = criar_sessao_bot(p)
    check('sessão encerrada não impede abrir uma nova', s3.id != s1.id, f'{s3.id} vs {s1.id}')
    ativas = BotSession.query.filter_by(telefone=normalize_contact_key(p),
                                        status=BOT_SESSAO_ATIVA).count()
    check('continua só uma ativa por telefone', ativas == 1, f'{ativas} ativas')

# ── 3. Sem sessão ativa → None (não cria sozinho) ─────────────────────────
print('\n3. Sem sessão ativa, processar devolve None')

with app.app_context():
    p = tel(3)
    res = processar_mensagem_bot(p, 'oi, tem carga?')
    check('devolve None quando não há sessão ativa', res is None, repr(res))
    criadas = BotSession.query.filter_by(telefone=normalize_contact_key(p)).count()
    check('e não criou nenhuma sessão', criadas == 0, f'{criadas} sessões')

# ── 4. Opt-out: "SAIR" grava a recusa e encerra em optout ─────────────────
print('\n4. Saída global: SAIR vira opt-out')

with app.app_context():
    p = tel(4)
    criar_sessao_bot(p)
    res = processar_mensagem_bot(p, 'SAIR')
    check('status vira optout', res.get('status') == BOT_SESSAO_OPTOUT, str(res.get('status')))
    check('não é handoff', res.get('handoff') is False)
    check('confirma o opt-out em uma mensagem', res.get('replies') == [mensagens.OPTOUT_CONFIRMACAO])
    reg = OptOut.query.filter_by(telefone=normalize_contact_key(p)).first()
    check('gravou a recusa em OptOut', reg is not None and reg.origem == 'bot',
          reg.origem if reg else 'sem registro')

# ── 5. Opt-out antes: "atendente" vira handoff imediato ───────────────────
print('\n5. Saída global: pedir atendente vira handoff')

with app.app_context():
    p = tel(5)
    criar_sessao_bot(p)
    res = processar_mensagem_bot(p, 'quero falar com um atendente')
    check('pedir humano cai em handoff', res.get('handoff') is True)
    lida = db.session.get(BotSession, res['bot_session_id'])
    check('motivo do handoff é pediu_humano', lida.motivo_fim == 'pediu_humano', str(lida.motivo_fim))

# ── 6. Mídia fora de hora vira handoff ────────────────────────────────────
print('\n6. Saída global: mídia vira handoff')

with app.app_context():
    p = tel(6)
    criar_sessao_bot(p)
    res = processar_mensagem_bot(p, '', media='/tmp/foto_cnh.jpg')
    check('mídia cai em handoff', res.get('handoff') is True)
    lida = db.session.get(BotSession, res['bot_session_id'])
    check('motivo do handoff é midia_recebida', lida.motivo_fim == 'midia_recebida', str(lida.motivo_fim))

# ── 7. Motorista completo em viagem: boa viagem e encerra ─────────────────
print('\n7. Estado [4]: motorista em viagem recebe boa viagem e encerra')

with app.app_context():
    d = driver_completo(7, availability_status='em_frete')
    p = d.phone
    criar_sessao_bot(p)
    res = processar_mensagem_bot(p, 'oi')
    check('sessão encerra (sem handoff)',
          res.get('status') == BOT_SESSAO_ENCERRADA and res.get('handoff') is False,
          str(res.get('status')))
    check('devolve a mensagem de boa viagem', res.get('replies') == [mensagens.VIAGEM_ATIVA_OCUPADO])
    lida = db.session.get(BotSession, res['bot_session_id'])
    check('motivo do fim é em_viagem', lida.motivo_fim == 'em_viagem', str(lida.motivo_fim))
    check('o motorista foi vinculado à sessão', lida.driver_id == d.id, str(lida.driver_id))
    check('a trilha passou por viagem_ativa', 'viagem_ativa' in estados_da_trilha(lida),
          str(estados_da_trilha(lida)))

# ── 8. Motorista completo e livre: segue para geolocalizacao → handoff ────
print('\n8. Estado [4]: motorista livre segue para geolocalizacao (não existe) → handoff')

with app.app_context():
    d = driver_completo(8, availability_status='disponivel')
    p = d.phone
    criar_sessao_bot(p)
    res = processar_mensagem_bot(p, 'to livre')
    check('cai em handoff', res.get('handoff') is True)
    lida = db.session.get(BotSession, res['bot_session_id'])
    check('motivo é etapa_nao_implementada',
          lida.motivo_fim == 'etapa_nao_implementada', str(lida.motivo_fim))
    check('a trilha vai entrada→identificacao→viagem_ativa→handoff',
          estados_da_trilha(lida) == ['entrada', 'identificacao', 'viagem_ativa', 'handoff'],
          str(estados_da_trilha(lida)))

# ══════════════════════════════════════════════════════════════════════════
print()
if FALHAS:
    print(f'FALHOU em {len(FALHAS)} verificação(ões), no {DIALETO}:')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)

print(f'Todas as verificações passaram no {DIALETO}.')
