"""
Preparo comum dos testes da Fase 2. IMPORTAR ANTES DA APLICAÇÃO.

Trava de segurança
------------------
Estes testes criam usuários, motoristas e conversas, e trocam o envio de
WhatsApp por um falso. Rodar contra o banco de produção sujaria dados reais.

Importar infraestrutura_critica.main já sobe a aplicação, que roda
db.create_all() e a migração no banco configurado. Por isso a trava precisa
acontecer ANTES desse import, e é por isso que este módulo existe.
"""

import os
import sys
from urllib.parse import urlparse

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
os.chdir(RAIZ)

HOSTS_LOCAIS = {'localhost', '127.0.0.1', '::1'}
URL = os.environ.get('DATABASE_URL', '').strip()


def _parar(mensagem):
    print()
    print('!' * 72)
    print('TESTE ABORTADO: ' + mensagem)
    print('!' * 72)
    sys.exit(2)


if URL:
    host = urlparse(URL.replace('postgres://', 'postgresql://', 1)).hostname or ''
    if host not in HOSTS_LOCAIS:
        _parar(f'DATABASE_URL aponta para "{host}". Estes testes só rodam contra '
               'PostgreSQL local, nunca contra o banco de produção.')
    if 'sslmode=' not in URL:
        # app.py acrescenta sslmode=require quando falta. Um PostgreSQL local sem
        # SSL recusa a conexão, e a app cai para SQLite em silêncio: o teste
        # passaria, mas no banco errado.
        _parar('inclua ?sslmode=disable na DATABASE_URL local. Sem isso a app '
               'exige SSL, a conexão falha e ela cai para SQLite em silêncio.')
else:
    sqlite = os.path.join(RAIZ, 'instance', 'emalog.db')
    if os.path.exists(sqlite) and os.environ.get('TESTE_PODE_SUJAR_SQLITE') != '1':
        _parar(f'sem DATABASE_URL os testes gravam em {sqlite}, que já existe e '
               'pode ter dados seus. Apague o arquivo ou defina '
               'TESTE_PODE_SUJAR_SQLITE=1.')


def conferir_dialeto(app, db):
    """Confirma que a app está no banco pedido, e não no fallback."""
    with app.app_context():
        dialeto = db.engine.dialect.name
    if URL and dialeto != 'postgresql':
        _parar('DATABASE_URL foi informada, mas a app caiu para SQLite. '
               'Verifique se o PostgreSQL local está no ar.')
    return dialeto
