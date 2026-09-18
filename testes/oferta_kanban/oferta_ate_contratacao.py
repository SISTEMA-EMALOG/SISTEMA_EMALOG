"""
A oferta de frete sai pelo WhatsApp e chega ao Kanban de contratação.

A tela "Selecionar Motoristas" (`/freight/<id>/select-drivers`) mandava a
oferta por um `WHATSAPP_API_URL` que nunca existiu no ambiente: sem essas
variáveis ela gravava a mensagem como `enviado`, devolvia sucesso e nada saía
do servidor. E, como só gravava `WhatsAppMessage`, a oferta nunca aparecia no
quadro de `/contracting`, que lê apenas `DriverBid`.

Cenários:
  1. Envio bem-sucedido: a Evolution recebeu a mensagem, a DriverBid nasceu em
     "Aguardando resposta" e o card aparece no quadro.
  2. Falha no envio: nada de sucesso falso — a bid vai para "Encerrados", a
     WhatsAppMessage fica como `erro` e o frete não é marcado como ofertado.
  3. Motorista responde ACEITO: o card vai para "Interessados" com o valor da
     oferta, sem depender do Groq.
  4. Motorista responde RECUSO: o card vai para "Encerrados".
  5. "aceito por 3200" é contraproposta, não aceite: segue para o extrator de
     preço.
  6. Botão "Aceitar Frete": contrata pelo mesmo caminho da Central, move o
     card para "Contratados", gera os pagamentos 70/30 e encerra as outras
     ofertas do frete.

Como rodar: ver testes/oferta_kanban/LEIAME.md.
"""
import os
import sys
import uuid
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes de tocar o banco

import logging  # noqa: E402
logging.disable(logging.WARNING)

from infraestrutura_critica.main import app  # noqa: E402
from infraestrutura_critica.app import db  # noqa: E402
from infraestrutura_critica.models import (  # noqa: E402
    Client, Driver, DriverBid, Freight, Payment, User, WhatsAppMessage)

app.config['TESTING'] = True
app.config['WTF_CSRF_ENABLED'] = False
DIALETO = _ambiente.conferir_dialeto(app, db)

FALHAS = []
SUFIXO = 'SQLite' if DIALETO == 'sqlite' else 'PostgreSQL'


def check(label, ok, det=''):
    print(f"  [{'OK ' if ok else 'FALHA'}] {label}" + (f"  ({det})" if det else ''))
    if not ok:
        FALHAS.append(label)


# ── Envio falso: guarda o que a Evolution teria recebido ────────────────────
ENVIADOS = []
DEVE_FALHAR = {'sim': False}


def _send_text_falso(phone, message):
    ENVIADOS.append({'phone': phone, 'message': message})
    return not DEVE_FALHAR['sim']


# O envio da oferta e o webhook importam send_text de dentro da função, o que
# resolve pelo módulo: trocar aqui basta para os dois.
import infraestrutura_critica.utils.evolution_api as evo  # noqa: E402
evo.send_text = _send_text_falso


FRETES = []


def montar():
    """Cria cliente, frete novo e dois motoristas limpos. Devolve os ids."""
    with app.app_context():
        cliente = Client.query.filter_by(cnpj='00.000.000/0001-91').first()
        if cliente is None:
            cliente = Client(company_name='Cliente do teste', cnpj='00.000.000/0001-91',
                             phone='1140000000', email='cliente@teste.local')
            db.session.add(cliente)
            db.session.flush()

        motoristas = []
        for n, fone in ((1, '11974540001'), (2, '11974540002')):
            d = Driver.query.filter_by(phone=fone).first()
            if d is None:
                d = Driver(name=f'MOTORISTA TESTE {n}', phone=fone, truck_type='fiorino',
                           active=True, is_active=True, availability_status='disponivel',
                           whatsapp_mode='auto')
                db.session.add(d)
                db.session.flush()
            d.availability_status = 'disponivel'
            d.active = d.is_active = True
            motoristas.append(d.id)

        # Cada cenário usa um frete próprio, para não herdar estado do anterior.
        numero = f'FRT-TESTE-{len(FRETES) + 1:04d}'
        frete = Freight.query.filter_by(freight_number=numero).first()
        if frete is None:
            frete = Freight(freight_number=numero, client_id=cliente.id,
                            origin='SUZANO/SP', destination='SAO PAULO/SP',
                            product='dedicado - fiorino', weight=500,
                            agreed_price=3000.0, driver_cost=2500.0,
                            status='ofertado', pickup_date=date(2026, 9, 20))
            db.session.add(frete)
            db.session.flush()
        frete.status = 'ofertado'
        frete.assigned_driver_id = None
        frete.driver_cost = 2500.0
        frete.whatsapp_sent = False
        db.session.commit()

        # Começar sem bids nem pagamentos: os deste frete e os que tenham
        # sobrado nos motoristas, que travariam a oferta como "negociação ativa".
        for modelo in (Payment, WhatsAppMessage):
            modelo.query.filter_by(freight_id=frete.id).delete(synchronize_session=False)
        DriverBid.query.filter(DriverBid.driver_id.in_(motoristas)).delete(
            synchronize_session=False)
        db.session.commit()
        if frete.id not in FRETES:
            FRETES.append(frete.id)
        return frete.id, motoristas


def operador():
    with app.app_context():
        u = User.query.filter_by(username='oferta_operador').first()
        if u is None:
            u = User(username='oferta_operador', email='oferta_op@teste.local',
                     password_hash='x', role='admin', active=True)
            db.session.add(u)
            db.session.commit()
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
        s['_csrf_token'] = CSRF
    return c


CSRF = 'csrf-do-teste-de-oferta'
OP = operador()


def ofertar(frete_id, motorista_ids):
    return OP.post(f'/freight/{frete_id}/select-drivers',
                   data={'selected_drivers': [str(i) for i in motorista_ids],
                         '_csrf_token': CSRF},
                   follow_redirects=True)


# O webhook ignora mensagem cujo id já foi processado. Sem um id novo a cada
# execução, a segunda rodada contra o mesmo banco não testaria nada.
EXECUCAO = uuid.uuid4().hex[:8]


def responder(telefone, texto, marca):
    """Simula a resposta do motorista chegando pelo webhook da Evolution."""
    from prospeccao_captacao_motorista.ema_agent import _process_single_message
    with app.app_context():
        _process_single_message({
            'key': {'remoteJid': f'{telefone}@s.whatsapp.net', 'fromMe': False,
                    'id': f'TESTE-{EXECUCAO}-{marca}'},
            'message': {'conversation': texto},
            'pushName': 'Motorista Teste',
            'messageType': 'conversation',
        })


# ── 1. Envio bem-sucedido ───────────────────────────────────────────────────
print(f'\n1. a oferta sai pela Evolution e abre a demanda no Kanban [{SUFIXO}]')
FRETE, (M1, M2) = montar()
ENVIADOS.clear()
DEVE_FALHAR['sim'] = False
r = ofertar(FRETE, [M1])
html = r.get_data(as_text=True)

check('a tela responde', r.status_code == 200, str(r.status_code))
check('a Evolution recebeu 1 mensagem', len(ENVIADOS) == 1, f'{len(ENVIADOS)} envio(s)')
check('a mensagem é a oferta de frete',
      bool(ENVIADOS) and 'NOVA OFERTA DE FRETE' in ENVIADOS[0]['message'])
check('foi para o telefone do motorista',
      bool(ENVIADOS) and ENVIADOS[0]['phone'] == '11974540001',
      ENVIADOS[0]['phone'] if ENVIADOS else '—')
check('o aviso na tela não mente', 'Ofertas enviadas para 1 motorista' in html)

with app.app_context():
    bid = DriverBid.query.filter_by(freight_id=FRETE, driver_id=M1).one_or_none()
    check('nasceu a DriverBid', bid is not None)
    check('está em "Aguardando resposta"',
          bid is not None and bid.kanban_stage == 'awaiting_response',
          bid.kanban_stage if bid else '—')
    check('guarda o texto enviado no histórico',
          bid is not None and bool(bid.history) and bid.history[0]['role'] == 'ema')
    msg = WhatsAppMessage.query.filter_by(freight_id=FRETE, driver_id=M1).one_or_none()
    check('a WhatsAppMessage ficou como enviada',
          msg is not None and msg.status == 'enviado', msg.status if msg else '—')
    check('a mensagem é de saída e da esteira de frete',
          msg is not None and msg.direction == 'outbound' and msg.source == 'freight')
    check('o frete ficou marcado como ofertado por WhatsApp',
          Freight.query.get(FRETE).whatsapp_sent is True)

quadro = OP.get('/contracting/api/board').get_json()
aguardando = next(c for c in quadro['columns'] if c['id'] == 'awaiting_response')
check('o card aparece no quadro, em "Aguardando resposta"',
      any(c['freight']['id'] == FRETE for c in aguardando['cards']),
      f"{len(aguardando['cards'])} card(s)")


# ── 2. Falha no envio não vira sucesso ──────────────────────────────────────
print(f'\n2. quando a Evolution recusa, a tela não diz que enviou [{SUFIXO}]')
FRETE2, (M1, M2) = montar()
ENVIADOS.clear()
DEVE_FALHAR['sim'] = True
r = ofertar(FRETE2, [M1])
html = r.get_data(as_text=True)

check('a tela avisa que nada foi enviado', 'Nenhuma oferta foi enviada' in html)
check('não diz "Ofertas enviadas"', 'Ofertas enviadas para' not in html)
with app.app_context():
    bid = DriverBid.query.filter_by(freight_id=FRETE2, driver_id=M1).one_or_none()
    check('a bid foi encerrada', bid is not None and bid.kanban_stage == 'closed',
          bid.kanban_stage if bid else '—')
    check('o motivo do encerramento está registrado',
          bid is not None and 'Falha ao enviar' in (bid.closed_reason or ''))
    msg = WhatsAppMessage.query.filter_by(freight_id=FRETE2, driver_id=M1).one_or_none()
    check('a WhatsAppMessage ficou como erro',
          msg is not None and msg.status == 'erro', msg.status if msg else '—')
    check('o frete NÃO foi marcado como ofertado por WhatsApp',
          Freight.query.get(FRETE2).whatsapp_sent is not True)
DEVE_FALHAR['sim'] = False


# ── 3. ACEITO ───────────────────────────────────────────────────────────────
print(f'\n3. o motorista responde ACEITO [{SUFIXO}]')
FRETE3, (M1, M2) = montar()
ofertar(FRETE3, [M1])
ENVIADOS.clear()
responder('5511974540001', 'ACEITO', 'aceite')

with app.app_context():
    bid = DriverBid.query.filter_by(freight_id=FRETE3, driver_id=M1).one()
    check('o card foi para "Interessados"', bid.kanban_stage == 'interested', bid.kanban_stage)
    check('o valor da oferta ficou registrado',
          bid.driver_price is not None and float(bid.driver_price) == 2500.0,
          str(bid.driver_price))
    check('a resposta do motorista ficou guardada',
          (bid.driver_message or '').strip() == 'ACEITO')
check('o motorista recebeu a confirmação', len(ENVIADOS) == 1, f'{len(ENVIADOS)} envio(s)')
check('a confirmação cita o valor da oferta',
      bool(ENVIADOS) and 'R$ 2.500,00' in ENVIADOS[0]['message'],
      ENVIADOS[0]['message'][:60] if ENVIADOS else '—')


# ── 4. RECUSO ───────────────────────────────────────────────────────────────
print(f'\n4. o motorista responde RECUSO [{SUFIXO}]')
FRETE4, (M1, M2) = montar()
ofertar(FRETE4, [M2])
ENVIADOS.clear()
responder('5511974540002', 'recuso', 'recusa')

with app.app_context():
    bid = DriverBid.query.filter_by(freight_id=FRETE4, driver_id=M2).one()
    check('o card foi para "Encerrados"', bid.kanban_stage == 'closed', bid.kanban_stage)
    check('a bid ficou como recusada', bid.status == 'refused', bid.status)
check('o motorista recebeu o agradecimento', len(ENVIADOS) == 1, f'{len(ENVIADOS)} envio(s)')


# ── 5. Contraproposta não é aceite ──────────────────────────────────────────
print(f'\n5. "aceito por 3200" é contraproposta, não aceite [{SUFIXO}]')
from oferta_frete_motorista.utils.driver_bid_agent import _resposta_direta  # noqa: E402

FRETE5, (M1, M2) = montar()
ofertar(FRETE5, [M1])
with app.app_context():
    bid = DriverBid.query.filter_by(freight_id=FRETE5, driver_id=M1).one()
    check('mensagem com número não é resolvida direto',
          _resposta_direta(bid, 'aceito por 3200') is None)
    check('mensagem vazia não é resolvida direto', _resposta_direta(bid, '   ') is None)
    check('"aceito ou recuso?" não é resolvido direto',
          _resposta_direta(bid, 'aceito ou recuso?') is None)
    check('"ACEITO" limpo vira aceite',
          (_resposta_direta(bid, 'ACEITO') or {}).get('price') == 2500.0)
    check('"Recusar" limpo vira recusa',
          (_resposta_direta(bid, 'Recusar') or {}).get('refused') is True)


# ── 6. Aceitar Frete contrata pelo caminho da Central ───────────────────────
print(f'\n6. o botão "Aceitar Frete" move o card para "Contratados" [{SUFIXO}]')
FRETE6, (M1, M2) = montar()
ofertar(FRETE6, [M1, M2])
r = OP.post(f'/freight/{FRETE6}/assign-to-driver/{M1}', json={'driver_cost': '2500'},
            headers={'X-CSRFToken': CSRF})
corpo = r.get_json() or {}

check('a rota responde 200', r.status_code == 200, str(r.status_code))
check('a resposta diz que contratou', corpo.get('success') is True, str(corpo.get('message')))

with app.app_context():
    bid1 = DriverBid.query.filter_by(freight_id=FRETE6, driver_id=M1).one()
    bid2 = DriverBid.query.filter_by(freight_id=FRETE6, driver_id=M2).one()
    frete = Freight.query.get(FRETE6)
    check('o card do contratado foi para "Contratados"',
          bid1.kanban_stage == 'contracted', bid1.kanban_stage)
    check('a outra oferta foi encerrada', bid2.kanban_stage == 'closed', bid2.kanban_stage)
    check('o motivo do encerramento explica',
          'Outro motorista' in (bid2.closed_reason or ''), bid2.closed_reason or '—')
    check('o frete ficou aceito', frete.status == 'aceito', frete.status)
    check('o motorista foi atribuído', frete.assigned_driver_id == M1)
    check('o motorista ficou em frete',
          Driver.query.get(M1).availability_status == 'em_frete')
    pagamentos = Payment.query.filter_by(freight_id=FRETE6, driver_id=M1).all()
    valores = sorted(round(float(p.amount), 2) for p in pagamentos)
    check('gerou os dois pagamentos 70/30', valores == [750.0, 1750.0], str(valores))

quadro = OP.get('/contracting/api/board').get_json()
contratados = next(c for c in quadro['columns'] if c['id'] == 'contracted')
check('o card aparece no quadro, em "Contratados"',
      any(c['freight']['id'] == FRETE6 for c in contratados['cards']),
      f"{len(contratados['cards'])} card(s)")


# ── Resultado ───────────────────────────────────────────────────────────────
print()
print('=' * 72)
if FALHAS:
    print(f'RESULTADO ({DIALETO}): {len(FALHAS)} FALHA(S)')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)
print(f'RESULTADO ({DIALETO}): TUDO OK')
