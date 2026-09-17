"""
Anexos recebidos pela Central de Atendimento (Fase 3): foto de CNH, CRLV,
comprovante em PDF.

Regras
------
Só S3. Nunca o disco local, que some a cada deploy. Por isso este módulo não
usa infraestrutura_critica/utils/storage.py: aquele cai para o disco quando
o bucket falta, e trata o placeholder 'PREENCHER-nome-do-bucket' como bucket
real, o que faz toda gravação falhar com InvalidAccessKeyId.

Sem S3 de verdade, o anexo é registrado como indisponível, com a referência do
provedor guardada. A mensagem continua chegando; só o arquivo não é guardado.

O tipo é decidido pelos primeiros bytes do arquivo, não pelo que o remetente
declarou. Um executável renomeado para .jpg é recusado.

Ordem de transação
------------------
A linha do anexo é gravada junto com a mensagem, como pendente. O download e
a gravação no S3 acontecem DEPOIS do commit, sem transação aberta, e o
resultado entra numa transação curta que só toca message_attachments. Essa
tabela não participa da ordem de lock conversa, depois motorista.
"""

import json
import logging
import os
import uuid
from urllib.parse import urlparse

import requests

from infraestrutura_critica.app import db
from infraestrutura_critica.models import (
    MessageAttachment,
    ANEXO_ARMAZENADO,
    ANEXO_BLOQUEADO,
    ANEXO_ERRO,
    ANEXO_INDISPONIVEL,
    ANEXO_PENDENTE,
)

log = logging.getLogger(__name__)

LIMITE_BYTES = 10 * 1024 * 1024
TIMEOUT = 20

EXTENSOES = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/webp': '.webp',
    'application/pdf': '.pdf',
}

_cliente = None


# ── Configuração ─────────────────────────────────────────────────────────────

def _placeholder(valor):
    return 'PREENCHER' in (valor or '').upper()


def s3_configurado():
    """
    Há S3 de verdade?

    Chaves de acesso vazias são aceitas: no Elastic Beanstalk as credenciais
    podem vir do perfil da instância. Chave com placeholder não é aceita,
    porque o boto3 a usaria antes do perfil e toda gravação falharia.
    """
    bucket = os.environ.get('S3_BUCKET_NAME', '').strip()
    if not bucket or _placeholder(bucket):
        return False
    for nome in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'):
        if _placeholder(os.environ.get(nome, '')):
            return False
    return True


def _bucket():
    return os.environ.get('S3_BUCKET_NAME', '').strip()


def _s3():
    global _cliente
    if _cliente is None:
        import boto3
        _cliente = boto3.client('s3', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
    return _cliente


def _reiniciar_cliente():
    """Para testes: força criar o cliente de novo com o ambiente atual."""
    global _cliente
    _cliente = None


# ── Validação ────────────────────────────────────────────────────────────────

def detectar_tipo(dados):
    """Tipo real pelo começo do arquivo, ou None se não for aceito."""
    if not dados:
        return None
    if dados.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if dados.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if len(dados) >= 12 and dados[:4] == b'RIFF' and dados[8:12] == b'WEBP':
        return 'image/webp'
    if dados.startswith(b'%PDF-'):
        return 'application/pdf'
    return None


# ── Obtenção dos bytes ───────────────────────────────────────────────────────

class AnexoRecusado(Exception):
    def __init__(self, status, mensagem):
        super().__init__(mensagem)
        self.status = status
        self.mensagem = mensagem


def _ler_limitado(resposta):
    partes, total = [], 0
    for bloco in resposta.iter_content(64 * 1024):
        total += len(bloco)
        if total > LIMITE_BYTES:
            raise AnexoRecusado(ANEXO_BLOQUEADO, f'Arquivo maior que {LIMITE_BYTES // 1024 // 1024} MB.')
        partes.append(bloco)
    return b''.join(partes)


def baixar_da_twilio(url):
    """
    Baixa a mídia da Twilio, autenticado com a conta.

    Só aceita endereço da própria Twilio: a URL vem de um webhook com
    assinatura válida, mas conferir o host impede que um erro de
    configuração faça o servidor buscar endereço arbitrário.
    """
    from atendimento_conversas.utils import twilio_client
    host = (urlparse(url).hostname or '').lower()
    if not (host == 'twilio.com' or host.endswith('.twilio.com')):
        raise AnexoRecusado(ANEXO_BLOQUEADO, 'Endereço de mídia fora da Twilio.')
    try:
        r = requests.get(url, auth=(twilio_client.account_sid(), twilio_client.auth_token()),
                         timeout=TIMEOUT, stream=True)
    except requests.RequestException as exc:
        raise AnexoRecusado(ANEXO_ERRO, f'Falha de rede ao baixar da Twilio: {exc}') from exc
    if r.status_code != 200:
        raise AnexoRecusado(ANEXO_ERRO, f'Twilio respondeu HTTP {r.status_code} ao baixar a mídia.')
    return _ler_limitado(r)


def ler_arquivo_local_temporario(caminho):
    """
    Lê a mídia que o webhook do EMA já baixou e decifrou num arquivo
    temporário. Só lê: o EMA ainda usa esse arquivo depois.
    """
    tamanho = os.path.getsize(caminho)
    if tamanho > LIMITE_BYTES:
        raise AnexoRecusado(ANEXO_BLOQUEADO, f'Arquivo maior que {LIMITE_BYTES // 1024 // 1024} MB.')
    with open(caminho, 'rb') as f:
        return f.read()


# ── Registro e processamento ────────────────────────────────────────────────

def registrar_pendente(mensagem, provider, provider_ref, content_type=None, nome=None):
    """
    Cria a linha do anexo na MESMA transação da mensagem. Sem commit.

    Assim, se o processo cair entre a mensagem e o arquivo, o anexo fica
    registrado como pendente em vez de sumir.
    """
    if isinstance(provider_ref, (dict, list)):
        provider_ref = json.dumps(provider_ref, ensure_ascii=False)[:4000]
    anexo = MessageAttachment(
        message=mensagem,
        conversation_id=mensagem.conversation_id,
        status=ANEXO_PENDENTE,
        provider=provider,
        provider_ref=provider_ref,
        content_type=(content_type or '')[:100] or None,
        original_name=(nome or '')[:200] or None,
    )
    db.session.add(anexo)
    return anexo


def _concluir(anexo_id, **campos):
    """Transação curta que só toca a linha do anexo."""
    try:
        anexo = db.session.get(MessageAttachment, anexo_id)
        if anexo is None:
            return None
        for k, v in campos.items():
            setattr(anexo, k, v)
        db.session.commit()
        return campos.get('status')
    except Exception as exc:
        db.session.rollback()
        log.error(f"❌ Anexos: falha ao atualizar anexo {anexo_id}: {exc}")
        return None


def processar(anexo_id, conversa_id, obter_bytes):
    """
    Obtém os bytes, valida e grava no S3. Chamar DEPOIS do commit da mensagem.

    `obter_bytes` é uma função sem argumentos, para que o download só aconteça
    se o S3 estiver configurado. Devolve o status final.
    """
    if not s3_configurado():
        return _concluir(anexo_id, status=ANEXO_INDISPONIVEL,
                         error_msg='S3 não configurado; arquivo não guardado.')
    try:
        dados = obter_bytes()
        tipo = detectar_tipo(dados)
        if tipo is None:
            raise AnexoRecusado(ANEXO_BLOQUEADO,
                                'Tipo não aceito. Aceitos: JPEG, PNG, WEBP e PDF.')
        chave = f'conversas/{conversa_id or "sem-conversa"}/{uuid.uuid4().hex}{EXTENSOES[tipo]}'
        _s3().put_object(Bucket=_bucket(), Key=chave, Body=dados, ContentType=tipo,
                         ServerSideEncryption='AES256')
    except AnexoRecusado as exc:
        return _concluir(anexo_id, status=exc.status, error_msg=exc.mensagem[:300])
    except Exception as exc:
        log.error(f"❌ Anexos: falha ao gravar anexo {anexo_id} no S3: {exc}")
        return _concluir(anexo_id, status=ANEXO_ERRO, error_msg=str(exc)[:300])

    return _concluir(anexo_id, status=ANEXO_ARMAZENADO, storage_key=chave,
                     content_type=tipo, size_bytes=len(dados), error_msg=None)


def ler_do_s3(chave):
    """Bytes de um anexo armazenado."""
    obj = _s3().get_object(Bucket=_bucket(), Key=chave)
    return obj['Body'].read()


def serializar(anexo):
    return {
        'id': anexo.id,
        'status': anexo.status,
        'content_type': anexo.content_type,
        'tamanho': anexo.size_bytes,
        'nome': anexo.original_name,
        'imagem': (anexo.content_type or '').startswith('image/'),
        'motivo': anexo.error_msg if anexo.status != ANEXO_ARMAZENADO else None,
    }
