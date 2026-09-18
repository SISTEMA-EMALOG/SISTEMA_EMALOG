"""
Fase 5 — [3] coleta_minima: coleta NATIVA do mínimo, sem agente EMA.

O que está sendo provado, e por que importa:

  - Sem cadastro: o bot cria o registro mínimo e pergunta, por seleção de opções,
    nome → veículo → cidade/UF; grava cada um no Driver e transfere ao atendente
    (handoff 'coleta_minima_ok'). O EMA NUNCA é acionado.
  - Só pergunta o que FALTA: motorista que já existe e só não tem UF é perguntado
    apenas da cidade/UF; nome e veículo não são tocados.
  - Motorista que já tem os 3 mínimos (mas está incompleto por outros campos) vai
    direto ao atendente, sem pergunta nenhuma.
  - Opção de veículo inválida re-pergunta (conta tentativa); na 3ª sem entender,
    handoff. Uma resposta válida zera o contador (cada pergunta tem 2 tentativas).
  - Veículo aceito pelo nome ("carreta"); cidade/UF aceita só com a sigla ("MG").
  - Saída global no meio da coleta: "SAIR" vira opt-out na hora.
  - O veículo é gravado no vocabulário canônico do sistema (truck/carreta/...),
    e o cadastro nasce validated=False (um operador confere depois).

Como rodar: ver testes/fase5/LEIAME.md (mesma trava de _ambiente.py).
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
    BotSession, Driver, OptOut,
    BOT_SESSAO_ATIVA, BOT_SESSAO_HANDOFF, BOT_SESSAO_OPTOUT,
)
from chatbot_regras import processar_mensagem_bot, criar_sessao_bot  # noqa: E402
from chatbot_regras.utils import mensagens                           # noqa: E402
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
    return f'5511{SUFIXO[:5]}{final:04d}'


def driver_incompleto(final, **campos):
    """Motorista que EXISTE mas está incompleto (falta required de FIELD_DEFS).
    Só nome real + telefone, mais os campos passados."""
    d = Driver(name=f'Real {SUFIXO} {final}', phone=tel(final), **campos)
    db.session.add(d)
    db.session.commit()
    return d


def sessao_e_primeira(p, **kw):
    """Cria a sessão e manda a primeira resposta ('oi'), devolvendo o resultado."""
    criar_sessao_bot(p, **kw)
    return processar_mensagem_bot(p, 'oi')


# ══════════════════════════════════════════════════════════════════════════
print(f'\n=== Fase 5.3 — coleta mínima nativa ({DIALETO}) ===\n')

# ── 1. Sem cadastro: coleta completa nome→veículo→cidade/UF → handoff ──────
print('1. Sem cadastro: nome, veículo e cidade/UF, depois transfere ao atendente')

with app.app_context():
    p = tel(1)
    r1 = sessao_e_primeira(p)
    check('primeiro pergunta o nome', r1['replies'] == [mensagens.COLETA_NOME])

    r2 = processar_mensagem_bot(p, 'Maria Souza')
    check('depois pergunta o veículo', r2['replies'] == [mensagens.COLETA_VEICULO])

    r3 = processar_mensagem_bot(p, '6')       # 6 = carreta
    check('depois pergunta a cidade/UF', r3['replies'] == [mensagens.COLETA_CIDADE_UF])

    r4 = processar_mensagem_bot(p, 'Sao Paulo SP')
    check('termina em handoff (coleta_minima_ok)',
          r4['status'] == BOT_SESSAO_HANDOFF and r4['handoff'] is True)
    check('a última mensagem é a de transferência',
          r4['replies'] == [mensagens.COLETA_TRANSFERE])

    d = Driver.query.filter_by(phone=normalize_contact_key(p)).first()
    check('gravou o nome real (trocou o provisório)', d and d.name == 'Maria Souza',
          d.name if d else 'sem driver')
    check('gravou o veículo no vocabulário canônico', d and d.truck_type == 'carreta',
          d.truck_type if d else '-')
    check('gravou cidade e UF', d and d.city == 'Sao Paulo' and d.state == 'SP',
          f'{d.city}/{d.state}' if d else '-')
    check('cadastro nasce validated=False', d and d.validated is False)
    s = BotSession.query.filter_by(telefone=normalize_contact_key(p)).first()
    check('a trilha passou por coleta_minima',
          'coleta_minima' in [m['estado'] for m in (s.trilha or [])])

# ── 2. Incompleto, só falta UF: pergunta SÓ a cidade/UF ───────────────────
print('\n2. Motorista incompleto só sem UF: pergunta apenas a cidade/UF')

with app.app_context():
    d0 = driver_incompleto(2, truck_type='truck')   # tem nome e veículo, falta UF
    p = tel(2)
    r1 = sessao_e_primeira(p, driver=d0)
    check('pula nome e veículo, pergunta a cidade/UF',
          r1['replies'] == [mensagens.COLETA_CIDADE_UF], str(r1['replies'])[:50])

    r2 = processar_mensagem_bot(p, 'Curitiba PR')
    check('termina em handoff', r2['status'] == BOT_SESSAO_HANDOFF)
    d = db.session.get(Driver, d0.id)
    check('gravou a UF nova', d.state == 'PR' and d.city == 'Curitiba', f'{d.city}/{d.state}')
    check('não mexeu no nome', d.name == f'Real {SUFIXO} 2', d.name)
    check('não mexeu no veículo', d.truck_type == 'truck', d.truck_type)

# ── 3. Já tem os 3 mínimos (incompleto por outros campos): direto ao humano ─
print('\n3. Já tem nome+veículo+UF: nenhuma pergunta, transfere direto')

with app.app_context():
    d0 = driver_incompleto(3, truck_type='truck', state='SP', city='Campinas')
    p = tel(3)
    r1 = sessao_e_primeira(p, driver=d0)
    check('não pergunta nada, já transfere',
          r1['replies'] == [mensagens.COLETA_TRANSFERE] and r1['status'] == BOT_SESSAO_HANDOFF,
          str(r1['replies'])[:40])

# ── 4a. Veículo inválido: re-pergunta e uma resposta válida destrava ───────
print('\n4a. Veículo inválido re-pergunta; resposta válida destrava')

with app.app_context():
    p = tel(4)
    sessao_e_primeira(p)
    processar_mensagem_bot(p, 'Joao')                 # nome ok → pergunta veículo
    r = processar_mensagem_bot(p, 'zebra')            # inválido
    check('re-pergunta o veículo', r['replies'] == [mensagens.COLETA_VEICULO_INVALIDO])
    check('continua ativo', r['status'] == BOT_SESSAO_ATIVA)
    r = processar_mensagem_bot(p, 'carreta')          # válido pelo NOME
    check('resposta válida avança para a cidade/UF',
          r['replies'] == [mensagens.COLETA_CIDADE_UF])
    d = Driver.query.filter_by(phone=normalize_contact_key(p)).first()
    check('veículo gravado pelo nome', d.truck_type == 'carreta', d.truck_type)

# ── 4b. Veículo inválido 3x → handoff por não entender ────────────────────
print('\n4b. Veículo inválido três vezes vira handoff')

with app.app_context():
    p = tel(5)
    sessao_e_primeira(p)
    processar_mensagem_bot(p, 'Pedro')                # nome ok → veículo
    processar_mensagem_bot(p, 'aaa')                  # inválido 1
    processar_mensagem_bot(p, 'bbb')                  # inválido 2
    r = processar_mensagem_bot(p, '99')               # inválido 3 → handoff
    check('na 3ª sem entender vira handoff', r['status'] == BOT_SESSAO_HANDOFF)
    s = BotSession.query.filter_by(telefone=normalize_contact_key(p)).first()
    check('motivo é nao_entendido', s.motivo_fim == 'nao_entendido', str(s.motivo_fim))

# ── 5. Cidade/UF só com a sigla ───────────────────────────────────────────
print('\n5. Cidade/UF aceita só a sigla do estado')

with app.app_context():
    p = tel(6)
    sessao_e_primeira(p)
    processar_mensagem_bot(p, 'Ana')                  # nome
    processar_mensagem_bot(p, '4')                    # veículo = truck
    r = processar_mensagem_bot(p, 'MG')               # só a UF
    check('aceita só a sigla e transfere', r['status'] == BOT_SESSAO_HANDOFF)
    d = Driver.query.filter_by(phone=normalize_contact_key(p)).first()
    check('gravou a UF; cidade fica vazia', d.state == 'MG' and not d.city,
          f'{d.city}/{d.state}')

# ── 6. SAIR no meio da coleta vira opt-out ────────────────────────────────
print('\n6. Saída global: SAIR no meio da coleta vira opt-out')

with app.app_context():
    p = tel(7)
    sessao_e_primeira(p)                              # está pedindo o nome
    r = processar_mensagem_bot(p, 'SAIR')
    check('vira opt-out', r['status'] == BOT_SESSAO_OPTOUT, r['status'])
    check('gravou a recusa', OptOut.query.filter_by(telefone=normalize_contact_key(p)).first() is not None)

# ══════════════════════════════════════════════════════════════════════════
print()
if FALHAS:
    print(f'FALHOU em {len(FALHAS)} verificação(ões), no {DIALETO}:')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)

print(f'Todas as verificações passaram no {DIALETO}.')
