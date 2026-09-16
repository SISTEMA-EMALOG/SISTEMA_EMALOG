"""
Camada de abstração para armazenamento de arquivos.

Usa Amazon S3 (bucket configurado via S3_BUCKET_NAME) como armazenamento
persistente — os arquivos sobrevivem a deploys, restarts e múltiplas
instâncias da aplicação, ao contrário do filesystem local do EC2/Elastic
Beanstalk, que é substituído a cada deploy.

Autenticação via AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (variáveis de
ambiente padrão do boto3 — não são lidas manualmente aqui, o SDK já faz
isso sozinho). Caso o bucket não esteja configurado (ex: ambiente de
desenvolvimento local), faz fallback para o filesystem local (uploads/).
"""
import os
import io
import logging

logger = logging.getLogger(__name__)

_client = None
_use_s3 = False
_bucket = os.environ.get("S3_BUCKET_NAME", "").strip()

if _bucket:
    try:
        import boto3
        _client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        _use_s3 = True
        logger.info(f"✅ S3 ativo → bucket {_bucket}")
    except Exception as e:
        logger.warning(f"⚠️ S3 indisponível ({e}) — usando filesystem local")
        _client = None
        _use_s3 = False
else:
    logger.info("📁 Armazenamento local (filesystem) — S3_BUCKET_NAME não configurado")


def _normalize_key(key: str) -> str:
    """Normaliza a chave removendo prefixos relativos."""
    return key.lstrip("./")


def save_file(file_stream, key: str) -> str:
    """
    Salva um arquivo. Retorna a chave (key) usada no banco para recuperação.
    `key` deve ser um path relativo, ex: 'uploads/drivers/20250801_cnh.pdf'
    """
    data = file_stream.read() if hasattr(file_stream, 'read') else file_stream
    norm_key = _normalize_key(key)

    if _use_s3:
        try:
            _client.put_object(Bucket=_bucket, Key=norm_key, Body=data)
            logger.debug(f"Storage (S3): salvo {key} ({len(data)} bytes)")
            return key
        except Exception as e:
            logger.error(f"Storage: falha ao salvar {key} no S3: {e}")
            raise

    path = key
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    logger.debug(f"Storage (local): salvo {key} ({len(data)} bytes) → {path}")
    return key


def get_file(key: str) -> "io.BytesIO | None":
    """Retorna BytesIO com o conteúdo do arquivo, ou None se não existir."""
    if not key:
        return None
    norm_key = _normalize_key(key)

    if _use_s3:
        try:
            obj = _client.get_object(Bucket=_bucket, Key=norm_key)
            return io.BytesIO(obj['Body'].read())
        except _client.exceptions.NoSuchKey:
            pass
        except Exception as e:
            logger.error(f"Storage: erro ao ler {key} do S3: {e}")
        # fallback: tenta filesystem local (compatibilidade com registros antigos)
        if os.path.exists(key):
            with open(key, 'rb') as f:
                return io.BytesIO(f.read())
        logger.warning(f"Storage: arquivo não encontrado (S3): {key}")
        return None

    if os.path.exists(key):
        with open(key, 'rb') as f:
            return io.BytesIO(f.read())
    logger.warning(f"Storage: arquivo não encontrado (local): {key}")
    return None


def file_exists(key: str) -> bool:
    """Verifica se o arquivo existe no armazenamento."""
    if not key:
        return False
    norm_key = _normalize_key(key)

    if _use_s3:
        try:
            _client.head_object(Bucket=_bucket, Key=norm_key)
            return True
        except Exception:
            pass
        return os.path.exists(key)

    return os.path.exists(key)


def delete_file(key: str):
    """Remove o arquivo do armazenamento (sem erro se não existir)."""
    if not key:
        return
    norm_key = _normalize_key(key)

    if _use_s3:
        try:
            _client.delete_object(Bucket=_bucket, Key=norm_key)
            return
        except Exception as e:
            logger.error(f"Storage: erro ao deletar {key} do S3: {e}")

    try:
        if os.path.exists(key):
            os.remove(key)
    except OSError:
        pass


def get_file_size(key: str) -> int:
    """Retorna tamanho do arquivo em bytes (0 se não existir)."""
    if not key:
        return 0
    norm_key = _normalize_key(key)

    if _use_s3:
        try:
            resp = _client.head_object(Bucket=_bucket, Key=norm_key)
            return resp.get('ContentLength', 0)
        except Exception as e:
            logger.error(f"Storage: erro ao obter tamanho de {key} no S3: {e}")

    try:
        if os.path.exists(key):
            return os.path.getsize(key)
    except OSError:
        pass
    return 0
