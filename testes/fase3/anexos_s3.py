"""
Fase 3 — anexos recebidos: S3, validação de tipo e tamanho, acesso.

O S3 é simulado pelo Stubber do botocore: nenhuma chamada sai da máquina, e
cada chamada é conferida contra o modelo oficial da API do S3. O teste falha
se uma gravação esperada não acontecer ou se acontecer uma inesperada.

Como rodar: ver testes/fase3/LEIAME.md.
"""
import hashlib
import io
import os
import sys
import tempfile
import time
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _ambiente  # noqa: E402  trava de segurança, SEMPRE antes da app

CHAVE_EVO = 'chave-teste-anexos'
os.environ['EVOLUTION_API_KEY'] = CHAVE_EVO
TOKEN_EVO = hashlib.sha256(CHAVE_EVO.encode()).hexdigest()
os.environ['TWILIO_ACCOUNT_SID'] = 'ACtesteanexos00000000000000000000'
os.environ['TWILIO_AUTH_TOKEN'] = 'token-teste-anexos'
os.environ['TWILIO_WHATSAPP_NUMBER'] = 'whatsapp:+14155550100'
os.environ['TWILIO_WEBHOOK_URL'] = 'http://localhost/conversas/webhook'
BUCKET = 'bucket-teste-emalog'

import logging  # noqa: E402
logging.disable(logging.WARNING)

import boto3                                                        # noqa: E402
from botocore.response import StreamingBody                         # noqa: E402
from botocore.stub import ANY, Stubber                              # noqa: E402

from infraestrutura_critica.main import app                         # noqa: E402
from infraestrutura_critica.app import db                           # noqa: E402
from infraestrutura_critica.models import (                         # noqa: E402
    Conversation, Driver, EmaSession, MessageAttachment, User, WhatsAppMessage,
)
from atendimento_conversas.utils import anexos, twilio_client       # noqa: E402
import prospeccao_captacao_motorista.ema_agent as ema_rotas         # noqa: E402
import prospeccao_captacao_motorista.utils.ema_agent as ema_utils   # noqa: E402
import infraestrutura_critica.utils.evolution_api as evo            # noqa: E402

app.config['TESTING'] = True
DIALETO = _ambiente.conferir_dialeto(app, db)

FALHAS = []


def check(label, ok, det=''):
    print(f"  [{'OK ' if ok else 'FALHA'}] {label}" + (f"  ({det})" if det else ''))
    if not ok:
        FALHAS.append(label)


JPEG = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 2048
PDF = b'%PDF-1.4\n' + b'0' * 1024
EXE = b'MZ\x90\x00' + b'\x00' * 1024


# ── Simuladores ──────────────────────────────────────────────────────────────

def s3_configurado(sim=True):
    if sim:
        os.environ['S3_BUCKET_NAME'] = BUCKET
        os.environ['AWS_ACCESS_KEY_ID'] = 'AKIATESTEANEXOS00000'
        os.environ['AWS_SECRET_ACCESS_KEY'] = 'segredo-de-teste'
    else:
        os.environ['S3_BUCKET_NAME'] = 'PREENCHER-nome-do-bucket'
        os.environ['AWS_ACCESS_KEY_ID'] = 'PREENCHER-access-key-do-usuario-IAM'
        os.environ['AWS_SECRET_ACCESS_KEY'] = 'PREENCHER-secret-key-do-usuario-IAM'


def novo_stub():
    cliente = boto3.client('s3', region_name='us-east-1',
                           aws_access_key_id='AKIATESTEANEXOS00000',
                           aws_secret_access_key='segredo-de-teste')
    stub = Stubber(cliente)
    stub.activate()
    anexos._cliente = cliente
    return stub


def espera_put(stub, tipo):
    stub.add_response('put_object', {'ETag': '"teste"'}, {
        'Bucket': BUCKET, 'Key': ANY, 'Body': ANY,
        'ContentType': tipo, 'ServerSideEncryption': 'AES256',
    })


DOWNLOADS = []


class RespostaFalsa:
    def __init__(self, dados, status=200):
        self.dados, self.status_code = dados, status

    def iter_content(self, tamanho):
        for i in range(0, len(self.dados), tamanho):
            yield self.dados[i:i + tamanho]


def servir(dados, status=200):
    def get(url, **kw):
        DOWNLOADS.append({'url': url, 'auth': kw.get('auth')})
        return RespostaFalsa(dados, status)
    anexos.requests.get = get


# ── Preparo ──────────────────────────────────────────────────────────────────

def usuario(nome, papel='operador'):
    with app.app_context():
        u = User.query.filter_by(username=nome).first()
        if u is None:
            u = User(username=nome, email=f'{nome}@teste.local', password_hash='x', role=papel, active=True)
            db.session.add(u)
            db.session.commit()
        return SimpleNamespace(id=u.id, nome=u.username)


def navegador(u):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def fone():
    return f"55119{time.time_ns() % 100000000:08d}"


def twilio_com_midia(url, tipo='image/jpeg'):
    tel = fone()
    with app.app_context():
        db.session.add(Conversation(contact_phone=tel, status='aberta', handling_mode='manual',
                                    last_activity_at=datetime.utcnow()))
        db.session.commit()
    sid = f'SMANEXO{time.time_ns()}'
    params = {'From': f'whatsapp:+{tel}', 'Body': '', 'MessageSid': sid, 'NumMedia': '1',
              'MediaUrl0': url, 'MediaContentType0': tipo}
    r = app.test_client().post('/conversas/webhook', data=params, headers={
        'X-Twilio-Signature': twilio_client.compute_signature('http://localhost/conversas/webhook', params)})
    with app.app_context():
        msg = WhatsAppMessage.query.filter_by(external_message_id=sid).first()
        anexo = MessageAttachment.query.filter_by(message_id=msg.id).first() if msg else None
        return r, msg and SimpleNamespace(id=msg.id, texto=msg.message_content), \
            anexo and SimpleNamespace(id=anexo.id, status=anexo.status, tipo=anexo.content_type,
                                      chave=anexo.storage_key, tamanho=anexo.size_bytes,
                                      erro=anexo.error_msg, conv=anexo.conversation_id)


OP = usuario('anexo_op')
CLI = usuario('anexo_cliente', 'cliente')
NAV = navegador(OP)
URL_TWILIO = 'https://api.twilio.com/2010-04-01/Accounts/AC/Messages/MM/Media/ME'

print()
print('=' * 72)
print(f'FASE 3 — ANEXOS NO S3  |  banco: {DIALETO}')
print('=' * 72)


# ── A1. S3 com placeholder ───────────────────────────────────────────────────
print('\nA1. configuração com placeholder "PREENCHER": anexo registrado, arquivo não guardado')
s3_configurado(False)
anexos._reiniciar_cliente()
DOWNLOADS.clear()
servir(JPEG)
r, msg, anx = twilio_com_midia(URL_TWILIO)
check('webhook aceita a mensagem', r.status_code == 200, f'HTTP {r.status_code}')
check('mensagem gravada com marcador de anexo', msg is not None and 'anexo' in (msg.texto or ''))
check('anexo registrado como indisponível', anx is not None and anx.status == 'indisponivel',
      getattr(anx, 'status', None))
check('nada foi baixado: sem S3 não adianta buscar o arquivo', not DOWNLOADS, f'{len(DOWNLOADS)} download(s)')


# ── A2. Caminho feliz ────────────────────────────────────────────────────────
print('\nA2. S3 configurado, foto JPEG pela Twilio')
s3_configurado(True)
stub = novo_stub()
espera_put(stub, 'image/jpeg')
DOWNLOADS.clear()
servir(JPEG)
r, msg, anx = twilio_com_midia(URL_TWILIO)
check('anexo armazenado', anx is not None and anx.status == 'armazenado', getattr(anx, 'erro', None))
check('uma gravação no S3, com tipo e criptografia conferidos pelo modelo da API',
      not stub._queue, f'{len(stub._queue)} chamada(s) esperada(s) não feita(s)')
check('download autenticado com a conta Twilio',
      len(DOWNLOADS) == 1 and DOWNLOADS[0]['auth'] == (twilio_client.account_sid(), twilio_client.auth_token()))
check('chave no S3 organizada por conversa', anx and anx.chave.startswith(f'conversas/{anx.conv}/'),
      getattr(anx, 'chave', None))
check('tamanho registrado', anx and anx.tamanho == len(JPEG))

stub.add_response('get_object',
                  {'Body': StreamingBody(io.BytesIO(JPEG), len(JPEG)), 'ContentLength': len(JPEG)},
                  {'Bucket': BUCKET, 'Key': anx.chave})
resp = NAV.get(f'/conversas/api/anexos/{anx.id}')
check('atendente abre o anexo', resp.status_code == 200 and resp.data == JPEG, f'HTTP {resp.status_code}')
check('servido com o tipo detectado', resp.mimetype == 'image/jpeg', resp.mimetype)
check('com nosniff', resp.headers.get('X-Content-Type-Options') == 'nosniff')
politicas = resp.headers.getlist('Content-Security-Policy')
check('com sandbox, que a política global não apaga', 'sandbox' in politicas, str(politicas)[:120])
check('e a política global continua presente', any("default-src 'self'" in p for p in politicas))

det = NAV.get(f'/conversas/api/conversas/{anx.conv}').get_json()
lista = [a for m in det['mensagens'] for a in m['anexos']]
check('a thread traz o anexo para a tela exibir', any(a['id'] == anx.id and a['imagem'] for a in lista))


# ── A3. Tipo falsificado ─────────────────────────────────────────────────────
print('\nA3. executável declarado como image/jpeg')
stub = novo_stub()
servir(EXE)
r, msg, anx = twilio_com_midia(URL_TWILIO, 'image/jpeg')
check('recusado pelo conteúdo real', anx and anx.status == 'bloqueado', getattr(anx, 'erro', None))
check('nada gravado no S3', not stub._queue)


# ── A4. Grande demais ────────────────────────────────────────────────────────
print('\nA4. arquivo acima de 10 MB')
stub = novo_stub()
servir(b'\xff\xd8\xff' + b'\x00' * (anexos.LIMITE_BYTES + 10))
r, msg, anx = twilio_com_midia(URL_TWILIO)
check('recusado por tamanho', anx and anx.status == 'bloqueado' and 'MB' in (anx.erro or ''),
      getattr(anx, 'erro', None))


# ── A5. Endereço fora da Twilio ──────────────────────────────────────────────
print('\nA5. URL de mídia fora do domínio da Twilio')
stub = novo_stub()
DOWNLOADS.clear()
servir(JPEG)
r, msg, anx = twilio_com_midia('https://servidor-qualquer.example.com/foto.jpg')
check('recusado sem nem tentar baixar', anx and anx.status == 'bloqueado' and not DOWNLOADS,
      f"status={getattr(anx, 'status', None)} downloads={len(DOWNLOADS)}")


# ── A6. PDF pela Evolution, com o EMA ainda usando o arquivo ────────────────
print('\nA6. PDF pela Evolution: vai para o S3 e o EMA ainda recebe o arquivo')
stub = novo_stub()
espera_put(stub, 'application/pdf')
USO = {}


def baixar_falso(msg_data, tipo):
    fd, caminho = tempfile.mkstemp(suffix='.pdf')
    with os.fdopen(fd, 'wb') as f:
        f.write(PDF)
    USO['caminho'] = caminho
    return caminho


def ia_falsa(session, driver, texto, media_path=None):
    USO['ia_recebeu'] = media_path
    USO['arquivo_existia'] = bool(media_path) and os.path.exists(media_path)
    return 'recebi seu documento'


ema_rotas._download_media_decrypted = baixar_falso
ema_utils.process_message = ia_falsa
evo.send_text = lambda *a, **k: True

with app.app_context():
    tel = fone()
    d = Driver(name='Mot Anexo', phone=tel, whatsapp_mode='auto', active=True)
    db.session.add(d)
    db.session.flush()
    db.session.add(EmaSession(driver_id=d.id, status='active', started_at=datetime.utcnow()))
    db.session.commit()
mid = f'EVOPDF{time.time_ns()}'
r = app.test_client().post(f'/ema/webhook?token={TOKEN_EVO}', json={'event': 'messages.upsert', 'data': {
    'key': {'remoteJid': f'{tel}@s.whatsapp.net', 'fromMe': False, 'id': mid},
    'messageType': 'documentMessage',
    'message': {'documentMessage': {'mimetype': 'application/pdf', 'fileName': 'CRLV.pdf', 'caption': 'meu CRLV'}}}})
with app.app_context():
    m = WhatsAppMessage.query.filter_by(external_message_id=mid).first()
    a = MessageAttachment.query.filter_by(message_id=m.id).first() if m else None
    estado = (a.status, a.original_name, a.content_type) if a else None
check('webhook aceita', r.status_code == 200, f'HTTP {r.status_code}')
check('anexo armazenado como PDF com o nome original', estado == ('armazenado', 'CRLV.pdf', 'application/pdf'),
      str(estado))
check('gravação no S3 conferida', not stub._queue)
check('o EMA recebeu o arquivo e ele ainda existia', USO.get('arquivo_existia') is True, str(USO.get('ia_recebeu')))
if USO.get('caminho') and os.path.exists(USO['caminho']):
    os.remove(USO['caminho'])


# ── A7. Acesso ───────────────────────────────────────────────────────────────
print('\nA7. quem pode abrir anexo')
with app.app_context():
    qualquer = MessageAttachment.query.filter_by(status='armazenado').first().id
check('cliente recebe 403', navegador(CLI).get(f'/conversas/api/anexos/{qualquer}').status_code == 403)
r = app.test_client().get(f'/conversas/api/anexos/{qualquer}')
check('sem login não acessa', r.status_code in (302, 401), f'HTTP {r.status_code}')
check('anexo não armazenado dá 404, sem tocar no S3',
      NAV.get(f'/conversas/api/anexos/{anx.id}').status_code == 404)


# ── A8. Falha ao obter os bytes não perde o registro ─────────────────────────
print('\nA8. falha no meio do processamento')
with app.app_context():
    tel = fone()
    c = Conversation(contact_phone=tel, status='aberta', handling_mode='manual', last_activity_at=datetime.utcnow())
    db.session.add(c)
    db.session.flush()
    m = WhatsAppMessage(conversation_id=c.id, phone_number=tel, message_content='x', direction='inbound',
                        source='teste', status='recebido')
    db.session.add(m)
    a = anexos.registrar_pendente(m, 'twilio', URL_TWILIO, 'image/jpeg')
    db.session.commit()
    aid, cid = a.id, c.id


def explode():
    raise RuntimeError('conexão caiu no meio')


with app.app_context():
    final = anexos.processar(aid, cid, explode)
    linha = db.session.get(MessageAttachment, aid)
check('anexo fica registrado como erro, não some', final == 'erro' and linha is not None and linha.status == 'erro',
      f'{final} / {getattr(linha, "error_msg", None)}')


# ── A9. Nenhuma transação aberta durante a gravação no S3 ────────────────────
if DIALETO == 'postgresql':
    print('\nA9. durante a gravação no S3, nenhuma transação fica aberta')
    import psycopg2

    with app.app_context():
        dsn = db.engine.url.render_as_string(hide_password=False).replace('postgresql+psycopg2://', 'postgresql://')
    stub = novo_stub()
    espera_put(stub, 'image/jpeg')
    SONDA = {}
    real_put = anexos._cliente.put_object

    def put_com_sonda(**kw):
        conn = psycopg2.connect(dsn)
        try:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND state LIKE 'idle in transaction%%' AND pid <> pg_backend_pid()")
            SONDA['ociosas'] = cur.fetchone()[0]
        finally:
            conn.close()
        return real_put(**kw)

    anexos._cliente.put_object = put_com_sonda
    servir(JPEG)
    r, msg, anx = twilio_com_midia(URL_TWILIO)
    check('anexo armazenado', anx and anx.status == 'armazenado')
    check('nenhuma conexão "idle in transaction" durante o upload', SONDA.get('ociosas') == 0, str(SONDA))


print()
print('=' * 72)
if FALHAS:
    print(f'RESULTADO ({DIALETO}): {len(FALHAS)} FALHA(S)')
    for f in FALHAS:
        print(f'  - {f}')
    sys.exit(1)
print(f'RESULTADO ({DIALETO}): TUDO OK')
