"""
Fase 5, etapa 5.1 — os modelos do chatbot de regras existem e se comportam
igual nos dois bancos.

O que está sendo provado, e por que cada coisa importa:

  - As seis tabelas novas nascem do db.create_all(), sem migração de coluna.
  - As colunas acrescentadas a tabelas que JÁ existem (drivers, driver_bids)
    entram pelo caminho portável de utils/migrations.py. É onde o SQLite e o
    PostgreSQL discordam, e onde a Fase 0 já tinha tropeçado.
  - Um telefone só pode ter UMA sessão de bot em andamento. Sem essa garantia
    no banco, dois webhooks concorrentes abrem duas máquinas de estado para o
    mesmo número e o motorista recebe a conversa em dobro. Checagem em Python
    não resolve — é preciso o índice parcial único.
  - Uma sessão encerrada NÃO trava a abertura de uma nova para o mesmo
    telefone, senão o motorista nunca mais seria abordado.
  - contexto e trilha voltam do banco como dict e list de verdade. É aqui que
    a página de fretes guardada sobrevive: o motorista responde "2" pensando
    na lista que recebeu.
  - Um frete aceita as três posições de reserva, e o relógio nasce zerado.

Como rodar: ver testes/fase5/LEIAME.md.
"""
import os
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes da app

import logging  # noqa: E402
logging.disable(logging.WARNING)

from sqlalchemy import inspect as sa_inspect                      # noqa: E402
from sqlalchemy.exc import IntegrityError                         # noqa: E402

from infraestrutura_critica.main import app                       # noqa: E402
from infraestrutura_critica.app import db                         # noqa: E402
from infraestrutura_critica.utils.migrations import run_migrations  # noqa: E402
from infraestrutura_critica.models import (                       # noqa: E402
    BotCampanha, BotContato, BotSession, Client, Driver, DriverBid,
    Freight, InteresseRegiao, OptOut, ReservaFrete,
    BOT_ESTADO_ENTRADA, BOT_ESTADO_LISTA_FRETES,
    BOT_ORIGEM_CAMPANHA, BOT_SESSAO_ATIVA, BOT_SESSAO_ENCERRADA,
    RESERVA_ATIVA, RESERVA_MINUTOS_LIMITE,
)

app.config['TESTING'] = True
DIALETO = _ambiente.conferir_dialeto(app, db)

FALHAS = []
SUFIXO = uuid.uuid4().hex[:8]


def check(label, ok, det=''):
    print(f"  [{'OK ' if ok else 'FALHA'}] {label}" + (f"  ({det})" if det else ''))
    if not ok:
        FALHAS.append(label)


def tel(final):
    """Telefone de teste, único por execução, para o script poder repetir."""
    return f'5511{SUFIXO[:5]}{final:04d}'


# ══════════════════════════════════════════════════════════════════════════
print(f'\n=== Fase 5.1 — esquema do chatbot de regras ({DIALETO}) ===\n')

# ── 1. As tabelas novas existem ───────────────────────────────────────────
print('1. Tabelas criadas pelo create_all')

TABELAS = {
    'bot_sessoes': [
        'telefone', 'conversation_id', 'driver_id', 'campanha_id', 'origem',
        'estado', 'estado_anterior', 'contexto', 'trilha', 'tentativas',
        'status', 'motivo_fim', 'ultima_msg_em', 'lembrete_em',
    ],
    'bot_campanhas': [
        'nome', 'arquivo', 'total', 'validos', 'invalidos', 'duplicados',
        'status', 'criado_por',
    ],
    'bot_contatos': [
        'campanha_id', 'telefone_bruto', 'telefone', 'status',
        'motivo_recusa', 'bot_session_id', 'erro', 'enviado_em',
    ],
    'bot_reservas_frete': [
        'freight_id', 'driver_id', 'bid_id', 'posicao', 'status', 'motivo',
        'minutos_limite', 'minutos_consumidos', 'contando_desde',
        'criada_em', 'fechada_em',
    ],
    'bot_interesses_regiao': ['driver_id', 'uf', 'cidade', 'ativo'],
    'bot_optouts': ['telefone', 'origem', 'motivo'],
}

with app.app_context():
    inspector = sa_inspect(db.engine)
    existentes = set(inspector.get_table_names())

    for tabela, colunas in TABELAS.items():
        if tabela not in existentes:
            check(f'tabela {tabela}', False, 'não existe')
            continue
        reais = {c['name'] for c in inspector.get_columns(tabela)}
        faltando = [c for c in colunas if c not in reais]
        check(f'tabela {tabela}', not faltando,
              f'faltam: {", ".join(faltando)}' if faltando else f'{len(reais)} colunas')

# ── 2. Colunas novas em tabelas que já existiam ───────────────────────────
print('\n2. Colunas acrescentadas pela migração portável')

with app.app_context():
    inspector = sa_inspect(db.engine)

    drivers_cols = {c['name'] for c in inspector.get_columns('drivers')}
    for coluna in ('bot_ultima_oferta_em', 'bot_ofertas_ignoradas', 'bot_pausado'):
        check(f'drivers.{coluna}', coluna in drivers_cols)

    bids_cols = {c['name'] for c in inspector.get_columns('driver_bids')}
    check('driver_bids.preco_fixo', 'preco_fixo' in bids_cols)

# ── 3. Uma sessão ativa por telefone ──────────────────────────────────────
print('\n3. Índice parcial único: uma sessão de bot em andamento por telefone')

with app.app_context():
    telefone = tel(1)

    db.session.add(BotSession(telefone=telefone, estado=BOT_ESTADO_ENTRADA,
                              origem=BOT_ORIGEM_CAMPANHA, status=BOT_SESSAO_ATIVA))
    db.session.commit()

    # A segunda sessão ATIVA para o mesmo número tem de ser recusada pelo
    # banco. É a corrida de dois webhooks chegando junto.
    barrou = False
    try:
        db.session.add(BotSession(telefone=telefone, estado=BOT_ESTADO_ENTRADA,
                                  status=BOT_SESSAO_ATIVA))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        barrou = True
    check('segunda sessão ativa no mesmo telefone é recusada', barrou)

    # Encerrada a primeira, uma nova precisa poder abrir: senão o motorista
    # nunca mais seria abordado.
    primeira = BotSession.query.filter_by(telefone=telefone,
                                          status=BOT_SESSAO_ATIVA).one()
    primeira.status = BOT_SESSAO_ENCERRADA
    primeira.motivo_fim = 'inatividade'
    db.session.commit()

    reabriu = True
    try:
        db.session.add(BotSession(telefone=telefone, estado=BOT_ESTADO_ENTRADA,
                                  status=BOT_SESSAO_ATIVA))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        reabriu = False
    check('sessão encerrada não impede uma nova no mesmo telefone', reabriu)

    total = BotSession.query.filter_by(telefone=telefone).count()
    check('as duas sessões convivem no histórico', total == 2, f'{total} sessões')

# ── 4. contexto e trilha voltam como dict e list ──────────────────────────
print('\n4. JSON de contexto e trilha sobrevive ao banco')

with app.app_context():
    sessao = BotSession(
        telefone=tel(2), estado=BOT_ESTADO_LISTA_FRETES,
        contexto={'uf': 'SP', 'cidade': 'Campinas', 'pagina': 1,
                  'fretes_pagina': [11, 22, 33]},
        trilha=[{'estado': 'entrada', 'em': '2026-09-18T09:00:00'}],
    )
    db.session.add(sessao)
    db.session.commit()
    sessao_id = sessao.id
    db.session.expunge_all()

    lida = db.session.get(BotSession, sessao_id)
    check('contexto volta como dict', isinstance(lida.contexto, dict),
          type(lida.contexto).__name__)
    check('a página de fretes volta na ordem gravada',
          lida.contexto.get('fretes_pagina') == [11, 22, 33],
          str(lida.contexto.get('fretes_pagina')))
    check('trilha volta como list', isinstance(lida.trilha, list))
    check('tentativas nasce em zero', lida.tentativas == 0, str(lida.tentativas))

# ── 5. Reserva: três posições no mesmo frete ──────────────────────────────
print('\n5. Reserva de frete: titular e dois de reserva')

with app.app_context():
    cliente = Client(company_name=f'Cliente Teste {SUFIXO}',
                     cnpj=f'{SUFIXO[:2]}.{SUFIXO[2:5]}.{SUFIXO[5:8]}/0001-00',
                     phone='1133334444', email=f'{SUFIXO}@teste.local')
    db.session.add(cliente)
    db.session.flush()

    frete = Freight(freight_number=f'FR-{SUFIXO}', client_id=cliente.id,
                    origin='Campinas/SP', destination='Curitiba/PR',
                    product='Carga geral', weight=12000, agreed_price=6000.0,
                    driver_cost=4200.0, origin_state='SP', destination_state='PR',
                    status='ofertado')
    db.session.add(frete)
    db.session.flush()

    motoristas = []
    for i in range(3):
        m = Driver(name=f'Motorista Teste {SUFIXO} {i}', phone=tel(100 + i))
        db.session.add(m)
        motoristas.append(m)
    db.session.flush()

    for posicao, motorista in enumerate(motoristas, start=1):
        bid = DriverBid(freight_id=frete.id, driver_id=motorista.id,
                        kanban_stage='interested', driver_price=frete.driver_cost,
                        preco_fixo=True, sent_at=datetime.utcnow())
        db.session.add(bid)
        db.session.flush()
        db.session.add(ReservaFrete(freight_id=frete.id, driver_id=motorista.id,
                                    bid_id=bid.id, posicao=posicao,
                                    status=RESERVA_ATIVA,
                                    contando_desde=datetime.utcnow()))
    db.session.commit()

    reservas = ReservaFrete.query.filter_by(freight_id=frete.id).order_by(
        ReservaFrete.posicao).all()
    check('o frete tem as três posições', len(reservas) == 3, f'{len(reservas)}')
    check('as posições saíram 1, 2 e 3',
          [r.posicao for r in reservas] == [1, 2, 3])
    check('o relógio nasce com 3 horas úteis e nada consumido',
          all(r.minutos_limite == RESERVA_MINUTOS_LIMITE and
              r.minutos_consumidos == 0 for r in reservas))
    check('a reserva chega ao frete pela relação',
          reservas[0].freight.freight_number == f'FR-{SUFIXO}')
    check('a bid do bot nasce com preço fechado',
          all(r.bid.preco_fixo is True for r in reservas))

# ── 6. Campanha, contato, interesse e opt-out ─────────────────────────────
print('\n6. Campanha, interesse de região e opt-out')

with app.app_context():
    campanha = BotCampanha(nome=f'Lista teste {SUFIXO}', total=3, validos=2,
                           invalidos=1, duplicados=0)
    db.session.add(campanha)
    db.session.flush()

    db.session.add_all([
        BotContato(campanha_id=campanha.id, telefone_bruto='(11) 99999-0001',
                   telefone=tel(201)),
        BotContato(campanha_id=campanha.id, telefone_bruto='abc',
                   telefone=None, status='invalido',
                   erro='sem dígitos utilizáveis'),
    ])
    db.session.commit()

    check('a campanha guarda os contatos', campanha.contatos.count() == 2)
    check('contato sem telefone utilizável é aceito como inválido',
          BotContato.query.filter_by(campanha_id=campanha.id,
                                     status='invalido').count() == 1)
    check('o contato nasce pendente',
          BotContato.query.filter_by(campanha_id=campanha.id,
                                     status='pendente').count() == 1)

    motorista = Driver.query.filter_by(name=f'Motorista Teste {SUFIXO} 0').one()
    db.session.add(InteresseRegiao(driver_id=motorista.id, uf='SP'))
    db.session.commit()
    check('interesse de região nasce ativo',
          InteresseRegiao.query.filter_by(driver_id=motorista.id).one().ativo is True)

    db.session.add(OptOut(telefone=tel(300), origem='bot', motivo='pediu SAIR'))
    db.session.commit()

    repetiu = False
    try:
        db.session.add(OptOut(telefone=tel(300), origem='manual'))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        repetiu = True
    check('o mesmo telefone não entra duas vezes no opt-out', repetiu)

# ── 7. O ALTER num banco que JÁ existia ───────────────────────────────────
print('\n7. Migração de coluna em banco já existente (o caso da produção)')

# Até aqui as colunas vieram do create_all, num banco vazio. Em produção o
# banco já existe e as colunas precisam entrar por ALTER TABLE — que é onde o
# SQLite e o PostgreSQL divergem, e onde a Fase 0 já tinha tropeçado. Este
# bloco derruba as quatro colunas e manda a migração rodar de novo.

COLUNAS_MIGRADAS = [
    ('drivers', 'bot_ultima_oferta_em'),
    ('drivers', 'bot_ofertas_ignoradas'),
    ('drivers', 'bot_pausado'),
    ('driver_bids', 'preco_fixo'),
]

with app.app_context():
    suporta_drop = True
    if db.engine.dialect.name == 'sqlite':
        versao = db.session.execute(db.text('select sqlite_version()')).scalar()
        suporta_drop = tuple(int(x) for x in versao.split('.')[:2]) >= (3, 35)

    if not suporta_drop:
        print('  [PULADO] SQLite desta máquina não faz DROP COLUMN; '
              'a prova do ALTER roda no PostgreSQL')
    else:
        for tabela, coluna in COLUNAS_MIGRADAS:
            db.session.execute(db.text(
                f'ALTER TABLE "{tabela}" DROP COLUMN "{coluna}"'))
        db.session.commit()

        inspector = sa_inspect(db.engine)
        inspector.info_cache.clear()
        sumiram = all(
            coluna not in {c['name'] for c in inspector.get_columns(tabela)}
            for tabela, coluna in COLUNAS_MIGRADAS
        )
        check('as quatro colunas foram mesmo derrubadas', sumiram)

        # É esta chamada que roda no boot da produção, depois do create_all.
        run_migrations(db)

        inspector = sa_inspect(db.engine)
        inspector.info_cache.clear()
        for tabela, coluna in COLUNAS_MIGRADAS:
            reais = {c['name'] for c in inspector.get_columns(tabela)}
            check(f'migração recriou {tabela}.{coluna}', coluna in reais)

        # Rodar duas vezes não pode falhar nem duplicar: o boot roda sempre.
        run_migrations(db)
        inspector = sa_inspect(db.engine)
        inspector.info_cache.clear()
        idempotente = all(
            coluna in {c['name'] for c in inspector.get_columns(tabela)}
            for tabela, coluna in COLUNAS_MIGRADAS
        )
        check('rodar a migração de novo é inofensivo', idempotente)

        # A coluna nova entra NULL nas linhas que já existiam. O código trata
        # NULL como False e como zero — se isso mudar, o teste avisa.
        antigo = Driver.query.filter_by(name=f'Motorista Teste {SUFIXO} 0').one()
        check('linha antiga fica com a coluna nova em NULL',
              antigo.bot_pausado is None, repr(antigo.bot_pausado))

# ══════════════════════════════════════════════════════════════════════════
print()
if FALHAS:
    print(f'FALHOU em {len(FALHAS)} verificação(ões), no {DIALETO}:')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)

print(f'Todas as verificações passaram no {DIALETO}.')
