"""
Fase 3 — migração de whatsapp_messages.read_at num banco que já tem dados.

Cada boot roda num processo novo, como em produção, porque a aplicação
executa a migração ao ser importada.

Cenários:
  1. Banco no formato antigo, com mensagens marcadas status='lido' pela
     Central das Fases 1 e 2.
  2. Falha forçada no meio do preenchimento, por gatilho no banco: a
     coluna NÃO pode ficar criada sem o preenchimento, nem ser criada
     depois pelo migrador genérico.
  3. Sem a falha: coluna criada, histórico marcado como lido, status 'lido'
     desfeito só nas mensagens recebidas.
  4. Boot seguinte: o preenchimento não se repete sobre mensagem nova.

Como rodar: ver testes/fase3/LEIAME.md.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes de tocar o banco

from sqlalchemy import create_engine, inspect, text  # noqa: E402

RAIZ = _ambiente.RAIZ
URL = _ambiente.URL
SQLITE = os.path.join(RAIZ, 'instance', 'emalog.db')
DIALETO = 'postgresql' if URL else 'sqlite'

FALHAS = []


def check(label, ok, det=''):
    print(f"  [{'OK ' if ok else 'FALHA'}] {label}" + (f"  ({det})" if det else ''))
    if not ok:
        FALHAS.append(label)


def boot():
    codigo = (f"import sys; sys.path.insert(0, r'{RAIZ}'); "
              "from infraestrutura_critica.main import app")
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    r = subprocess.run([sys.executable, '-c', codigo], cwd=RAIZ, env=env, capture_output=True,
                       text=True, encoding='utf-8', errors='replace', timeout=300)
    if r.returncode != 0:
        print(r.stdout[-1500:], r.stderr[-1500:])
    return r


def motor():
    return create_engine(URL.replace('postgres://', 'postgresql://', 1) if URL else f'sqlite:///{SQLITE}')


def colunas():
    e = motor()
    try:
        return {c['name'] for c in inspect(e).get_columns('whatsapp_messages')}
    finally:
        e.dispose()


def sql(comando, **params):
    e = motor()
    try:
        with e.begin() as c:
            r = c.execute(text(comando), params)
            return r.fetchall() if r.returns_rows else r.rowcount
    finally:
        e.dispose()


print()
print('=' * 72)
print(f'FASE 3 — MIGRAÇÃO read_at SOBRE DADOS EXISTENTES  |  banco: {DIALETO}')
print('=' * 72)

# ── Preparo: schema atual, depois regride para o antigo e popula ────────────
if not URL and os.path.exists(SQLITE):
    os.remove(SQLITE)
r = boot()
check('boot inicial', r.returncode == 0)
sql('ALTER TABLE whatsapp_messages DROP COLUMN read_at')
check('pré-condição: read_at removida', 'read_at' not in colunas())

LINHAS = [
    ('m1', 'inbound', 'ema', 'lido'),         # estragada pela Central antiga
    ('m2', 'inbound', 'twilio', 'lido'),      # estragada pela Central antiga
    ('m3', 'inbound', 'ema', 'processado'),
    ('m4', 'outbound', 'twilio', 'lido'),     # recibo de leitura da Twilio: é legítimo
    ('m5', 'inbound', 'general', 'erro'),
]
for marca, direcao, origem, status in LINHAS:
    sql("INSERT INTO whatsapp_messages (message_content, phone_number, direction, source, status, sent_at, "
        "external_message_id) VALUES (:m, '5511900000000', :d, :o, :s, CURRENT_TIMESTAMP, :m)",
        m=marca, d=direcao, o=origem, s=status)


def estado():
    return {m: (d, s) for m, d, s in sql(
        "SELECT external_message_id, direction, status FROM whatsapp_messages "
        "WHERE external_message_id LIKE 'm_'")}


# ── 1+2. Falha forçada no preenchimento ──────────────────────────────────────
print('\n2. falha forçada no meio do preenchimento')
if DIALETO == 'postgresql':
    sql("CREATE OR REPLACE FUNCTION teste_bloqueia() RETURNS trigger AS $$ BEGIN "
        "IF NEW.status = 'processado' THEN RAISE EXCEPTION 'falha simulada'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql")
    sql("CREATE TRIGGER teste_bloqueia BEFORE UPDATE ON whatsapp_messages "
        "FOR EACH ROW EXECUTE FUNCTION teste_bloqueia()")
else:
    sql("CREATE TRIGGER teste_bloqueia BEFORE UPDATE OF status ON whatsapp_messages "
        "WHEN NEW.status = 'processado' BEGIN SELECT RAISE(ABORT, 'falha simulada'); END")

antes = estado()
r = boot()
check('a aplicação sobe mesmo com a migração falhando', r.returncode == 0)
check('coluna NÃO ficou criada pela metade', 'read_at' not in colunas(), str(sorted(colunas()))[-80:])
check('nenhuma linha alterada', estado() == antes)

if DIALETO == 'postgresql':
    sql('DROP TRIGGER teste_bloqueia ON whatsapp_messages')
    sql('DROP FUNCTION teste_bloqueia()')
else:
    sql('DROP TRIGGER teste_bloqueia')


# ── 3. Migração completa ─────────────────────────────────────────────────────
print('\n3. migração sem falha')
r = boot()
check('boot', r.returncode == 0)
check('coluna criada', 'read_at' in colunas())
depois = estado()
check("m1 recebida pelo EMA: 'lido' volta a 'processado'", depois['m1'][1] == 'processado', depois['m1'][1])
check("m2 recebida pela Twilio: 'lido' volta a 'recebido'", depois['m2'][1] == 'recebido', depois['m2'][1])
check('m3 já processada: intocada', depois['m3'][1] == 'processado')
check("m4 ENVIADA com 'lido' da Twilio: intocada", depois['m4'][1] == 'lido', depois['m4'][1])
check("m5 com 'erro': intocada", depois['m5'][1] == 'erro')
lidas = {m: v for m, v in sql("SELECT external_message_id, read_at FROM whatsapp_messages "
                              "WHERE external_message_id LIKE 'm_'")}
check('toda recebida antiga marcada como lida', all(lidas[m] is not None for m in ('m1', 'm2', 'm3', 'm5')))
check('enviada não recebe read_at', lidas['m4'] is None)


# ── 4. Idempotência ──────────────────────────────────────────────────────────
print('\n4. boot seguinte não repete o preenchimento')
sql("INSERT INTO whatsapp_messages (message_content, phone_number, direction, source, status, sent_at, "
    "external_message_id) VALUES ('nova', '5511900000000', 'inbound', 'twilio', 'recebido', "
    "CURRENT_TIMESTAMP, 'm6')")
r = boot()
m6 = sql("SELECT read_at FROM whatsapp_messages WHERE external_message_id = 'm6'")[0][0]
check('mensagem nova continua não lida depois do boot', r.returncode == 0 and m6 is None, str(m6))

print()
print('=' * 72)
if FALHAS:
    print(f'RESULTADO ({DIALETO}): {len(FALHAS)} FALHA(S)')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)
print(f'RESULTADO ({DIALETO}): TUDO OK')
