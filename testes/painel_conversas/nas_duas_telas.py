"""
O painel da Central de Atendimento nas duas telas, e o Chatwoot fora.

A fila de WhatsApp é atendida no mesmo painel em dois lugares: na página
`/conversas` e na aba Conversas da Central de Contratação, ao lado do Kanban.
O corpo dele está em `templates/conversas/_painel.html` e é incluído pelos
dois templates, com os mesmos ids. Este teste prova que:

  1. as duas telas trazem o painel, uma vez só em cada uma, com o CSS e o
     JavaScript que o fazem funcionar;
  2. a aba Conversas não tem mais nada do Chatwoot, e a rota que gerava o
     login dele respondeu 404;
  3. a política de segurança não libera iframe de terceiro, mesmo com
     CHATWOOT_URL ainda definida no ambiente;
  4. o Kanban continua na mesma página, e o botão Relatórios do painel só
     aparece para administrador;
  5. quem não é atendente continua sem acesso às duas telas.

Como rodar: ver testes/painel_conversas/LEIAME.md.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes de tocar o banco

# Fica no ambiente de propósito: o sistema tem de ignorar.
os.environ['CHATWOOT_URL'] = 'http://chatwoot-que-nao-deve-mais-valer.invalido:3000'
os.environ['CHATWOOT_ACCOUNT_ID'] = '1'

import logging  # noqa: E402
logging.disable(logging.WARNING)

from infraestrutura_critica.main import app  # noqa: E402
from infraestrutura_critica.app import db  # noqa: E402
from infraestrutura_critica.models import User  # noqa: E402

app.config['TESTING'] = True
DIALETO = _ambiente.conferir_dialeto(app, db)

FALHAS = []


def check(label, ok, det=''):
    print(f"  [{'OK ' if ok else 'FALHA'}] {label}" + (f"  ({det})" if det else ''))
    if not ok:
        FALHAS.append(label)


def navegador(nome, papel):
    with app.app_context():
        u = User.query.filter_by(username=nome).first()
        if u is None:
            u = User(username=nome, email=f'{nome}@teste.local', password_hash='x',
                     role=papel, active=True)
            db.session.add(u)
            db.session.commit()
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    return c


ADMIN = navegador('painel_admin', 'admin')
OPERADOR = navegador('painel_operador', 'operador')
CLIENTE = navegador('painel_cliente', 'cliente')

PAGINAS = (('/conversas/', 'página da Central'),
           ('/contracting/?tab=conversations', 'aba da Contratação'))


# ── 1. O painel nas duas telas ───────────────────────────────────────────────
print('\n1. o painel aparece nas duas telas')
for url, nome in PAGINAS:
    r = ADMIN.get(url)
    html = r.get_data(as_text=True)
    check(f'{nome}: responde', r.status_code == 200, str(r.status_code))
    check(f'{nome}: tem a fila', 'id="convApp"' in html)
    check(f'{nome}: um painel só', html.count('id="convApp"') == 1,
          str(html.count('id="convApp"')))
    check(f'{nome}: carrega o estilo do painel', 'css/conversas.css' in html)
    check(f'{nome}: carrega o script do painel', 'js/conversas.js' in html)
    check(f'{nome}: tem a caixa de resposta', 'id="convInput"' in html
          and 'id="convSend"' in html)
    check(f'{nome}: tem as abas da fila', 'data-aba="fila"' in html
          and 'data-aba="minhas"' in html)


# ── 2. Chatwoot fora ─────────────────────────────────────────────────────────
print('\n2. nada de Chatwoot')
html_contratacao = ADMIN.get('/contracting/?tab=conversations').get_data(as_text=True)
check('a aba não fala de Chatwoot', 'chatwoot' not in html_contratacao.lower())
check('o botão de abrir janela sumiu', 'openChatwoot' not in html_contratacao)
check('o CSS órfão da aba antiga sumiu', 'conversation-layout' not in html_contratacao)
r = ADMIN.get('/contracting/api/chatwoot/sso')
check('a rota de login do Chatwoot não existe mais', r.status_code == 404,
      str(r.status_code))


# ── 3. Nenhum iframe de terceiro ─────────────────────────────────────────────
print('\n3. política de segurança sem iframe de terceiro')
for url, nome in PAGINAS:
    politica = ' '.join(ADMIN.get(url).headers.getlist('Content-Security-Policy'))
    check(f'{nome}: só o próprio site em frame-src', "frame-src 'self';" in politica)
    check(f'{nome}: o endereço do Chatwoot não entra', 'chatwoot' not in politica.lower())


# ── 4. Kanban junto, e Relatórios só para admin ──────────────────────────────
print('\n4. Kanban na mesma página e Relatórios por papel')
check('o quadro continua na página', 'id="board"' in html_contratacao
      and 'data-view="board"' in html_contratacao)
check('as três abas continuam', html_contratacao.count('data-tab-link=') == 3,
      str(html_contratacao.count('data-tab-link=')))
check('admin vê Relatórios', '/conversas/relatorios' in html_contratacao)
for url, nome in PAGINAS:
    html = OPERADOR.get(url).get_data(as_text=True)
    check(f'{nome}: operador atende', 'id="convApp"' in html)
    check(f'{nome}: operador não vê Relatórios', '/conversas/relatorios' not in html)


# ── 5. Quem não é atendente não entra ────────────────────────────────────────
print('\n5. cliente não entra')
for url, nome in PAGINAS:
    r = CLIENTE.get(url)
    check(f'{nome}: acesso negado ao cliente', r.status_code == 403, str(r.status_code))

print()
print('=' * 72)
if FALHAS:
    print(f'RESULTADO ({DIALETO}): {len(FALHAS)} FALHA(S)')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)
print(f'RESULTADO ({DIALETO}): TUDO OK')
