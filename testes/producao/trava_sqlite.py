"""
Produção — o sistema não sobe em SQLite no Elastic Beanstalk.

Cada boot roda num processo novo, como em produção, porque a aplicação escolhe
o banco ao ser importada. O Elastic Beanstalk é simulado apontando
_MARCA_ELASTIC_BEANSTALK, de infraestrutura_critica/app.py, para uma pasta
temporária.

Cenários:
  1. No Elastic Beanstalk, sem DATABASE_URL: não sobe e não cria o SQLite.
  2. No Elastic Beanstalk, DATABASE_URL que não é PostgreSQL: não sobe, e a
     senha não aparece no log.
  3. No Elastic Beanstalk, PostgreSQL fora do ar: insiste todas as vezes,
     não sobe, não cria o SQLite e não mostra a senha no log.
  4. Fora do Elastic Beanstalk, sem DATABASE_URL: sobe em SQLite, como antes,
     e /conversas/api/status diz que a trava não vale.
  5. Fora do Elastic Beanstalk, PostgreSQL fora do ar: cai para SQLite, como
     antes.
  6. Só com PostgreSQL local: no Elastic Beanstalk, com o banco no ar, sobe
     em PostgreSQL, e /conversas/api/status diz que a trava vale.

Como rodar: ver testes/producao/LEIAME.md.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes de tocar o banco

RAIZ = _ambiente.RAIZ
URL = _ambiente.URL
SQLITE = os.path.join(RAIZ, 'instance', 'emalog.db')
DIALETO = 'postgresql' if URL else 'sqlite'
SENHA = 'senha-do-teste-nao-pode-vazar'
PG_FORA_DO_AR = f'postgresql://emalog_admin:{SENHA}@127.0.0.1:1/emalog?sslmode=disable'
RECUSA = 'O sistema não sobe em SQLite aqui'

FALHAS = []


def check(label, ok, det=''):
    print(f"  [{'OK ' if ok else 'FALHA'}] {label}" + (f"  ({det})" if det else ''))
    if not ok:
        FALHAS.append(label)


if os.path.exists(SQLITE):
    # Este teste apaga o SQLite entre os cenários.
    _ambiente._parar(f'{SQLITE} já existe e pode ter dados seus. Apague o arquivo antes.')

TMP = tempfile.mkdtemp(prefix='trava_sqlite_')
MARCA_EB = os.path.join(TMP, 'elasticbeanstalk')
os.makedirs(MARCA_EB)
MARCA_AUSENTE = os.path.join(TMP, 'nao-existe')


# Roda no processo novo: sobe a aplicação e, se subir, lê /conversas/api/status
# como administrador.
CODIGO_BOOT = r'''
import json, os, sys
sys.path.insert(0, os.environ['TESTE_RAIZ'])
import infraestrutura_critica.app as modulo
modulo._MARCA_ELASTIC_BEANSTALK = os.environ['TESTE_MARCA']
from infraestrutura_critica.main import app
from infraestrutura_critica.app import db
from infraestrutura_critica.models import User
app.config['TESTING'] = True
with app.app_context():
    u = User.query.filter_by(username='trava_admin').first()
    if u is None:
        u = User(username='trava_admin', email='trava_admin@teste.local', password_hash='x',
                 role='admin', active=True)
        db.session.add(u)
        db.session.commit()
    uid = u.id
c = app.test_client()
with c.session_transaction() as s:
    s['_user_id'] = str(uid)
    s['_fresh'] = True
print('STATUS=' + json.dumps(c.get('/conversas/api/status').get_json()))
'''


def boot(elastic_beanstalk, database_url):
    """Sobe a aplicação num processo novo. Devolve (código, saída, segundos)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('DATABASE_URL', 'EVOLUTION', 'TWILIO'))}
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', TESTE_RAIZ=RAIZ,
               TESTE_MARCA=MARCA_EB if elastic_beanstalk else MARCA_AUSENTE)
    if database_url is not None:
        env['DATABASE_URL'] = database_url
    if os.path.exists(SQLITE):
        os.remove(SQLITE)
    inicio = time.monotonic()
    r = subprocess.run([sys.executable, '-c', CODIGO_BOOT], cwd=RAIZ, env=env, capture_output=True,
                       text=True, encoding='utf-8', errors='replace', timeout=300)
    return r.returncode, r.stdout + r.stderr, time.monotonic() - inicio


def status(saida):
    """O JSON de /conversas/api/status impresso pelo boot, ou {}."""
    for linha in saida.splitlines():
        if linha.startswith('STATUS='):
            return json.loads(linha.split('=', 1)[1]) or {}
    return {}


try:
    # ── 1 ────────────────────────────────────────────────────────────────────
    print('\n1. Elastic Beanstalk sem DATABASE_URL')
    codigo, saida, _ = boot(True, None)
    check('não sobe', codigo != 0, f'código {codigo}')
    check('explica que recusou SQLite', RECUSA in saida and 'DATABASE_URL está vazio' in saida)
    check('não cria o SQLite', not os.path.exists(SQLITE))
    if codigo == 0 or RECUSA not in saida:
        print(saida[-1500:])

    # ── 2 ────────────────────────────────────────────────────────────────────
    print('\n2. Elastic Beanstalk com DATABASE_URL que não é PostgreSQL')
    codigo, saida, _ = boot(True, f'mysql://emalog:{SENHA}@127.0.0.1/emalog')
    check('não sobe', codigo != 0, f'código {codigo}')
    check('explica que recusou SQLite', RECUSA in saida and 'não começa com postgresql://' in saida)
    check('senha fora do log', SENHA not in saida)
    check('não cria o SQLite', not os.path.exists(SQLITE))

    # ── 3 ────────────────────────────────────────────────────────────────────
    print('\n3. Elastic Beanstalk com PostgreSQL fora do ar')
    codigo, saida, segundos = boot(True, PG_FORA_DO_AR)
    tentativas = 10
    check('não sobe', codigo != 0, f'código {codigo}')
    check(f'tenta {tentativas} vezes', f'(tentativa {tentativas}/{tentativas})' in saida
          and f'(tentativa {tentativas + 1}/' not in saida)
    check('espera entre as tentativas', segundos >= 3 * (tentativas - 1), f'{segundos:.0f} s')
    check('termina antes do timeout de 120 s do gunicorn', segundos < 100, f'{segundos:.0f} s')
    check('explica que recusou SQLite', RECUSA in saida and 'não respondeu em 10 tentativas' in saida)
    check('senha fora do log', SENHA not in saida)
    check('não cria o SQLite', not os.path.exists(SQLITE))
    check('não usa o fallback', 'Usando SQLite' not in saida and 'SQLite local (fallback)' not in saida)

    # ── 4 ────────────────────────────────────────────────────────────────────
    print('\n4. Fora do Elastic Beanstalk, sem DATABASE_URL')
    codigo, saida, _ = boot(False, None)
    st = status(saida)
    check('sobe', codigo == 0, f'código {codigo}')
    check('em SQLite', st.get('dialeto') == 'sqlite', st.get('dialeto'))
    check('status diz que a trava não vale aqui', st.get('trava_sqlite') is False,
          st.get('trava_sqlite'))
    if codigo != 0 or not st:
        print(saida[-1500:])

    # ── 5 ────────────────────────────────────────────────────────────────────
    print('\n5. Fora do Elastic Beanstalk, PostgreSQL fora do ar')
    codigo, saida, _ = boot(False, PG_FORA_DO_AR)
    st = status(saida)
    check('sobe', codigo == 0, f'código {codigo}')
    check('cai para SQLite, como antes', st.get('dialeto') == 'sqlite', st.get('dialeto'))
    check('uma tentativa só', 'tentativa' not in saida)

    # ── 6 ────────────────────────────────────────────────────────────────────
    if URL:
        print('\n6. Elastic Beanstalk com PostgreSQL no ar')
        codigo, saida, _ = boot(True, URL)
        st = status(saida)
        check('sobe', codigo == 0, f'código {codigo}')
        check('em PostgreSQL', st.get('dialeto') == 'postgresql', st.get('dialeto'))
        check('status diz que a trava vale', st.get('trava_sqlite') is True, st.get('trava_sqlite'))
        check('não cria o SQLite', not os.path.exists(SQLITE))
        if codigo != 0 or not st:
            print(saida[-1500:])
    else:
        print('\n6. Elastic Beanstalk com PostgreSQL no ar: só roda com DATABASE_URL local')
finally:
    if os.path.exists(SQLITE):
        os.remove(SQLITE)
    shutil.rmtree(TMP, ignore_errors=True)

print()
print('=' * 72)
if FALHAS:
    print(f'RESULTADO ({DIALETO}): {len(FALHAS)} FALHA(S)')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)
print(f'RESULTADO ({DIALETO}): TUDO OK')
