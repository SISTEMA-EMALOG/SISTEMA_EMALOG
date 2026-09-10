"""
Camada de abstração para armazenamento de arquivos.

Usa o Replit Object Storage (bucket configurado via DEFAULT_OBJECT_STORAGE_BUCKET_ID)
como armazenamento persistente — os arquivos sobrevivem a deploys, restarts e múltiplas
instâncias da aplicação.

Caso o Object Storage não esteja disponível (ex: ambiente sem bucket configurado),
faz fallback para o filesystem local (uploads/) — usado apenas em desenvolvimento.
"""
import os
import io
import logging

logger = logging.getLogger(__name__)

_client = None
_use_objstore = False

try:
    from replit.object_storage import Client
    _bucket_id = os.environ.get("DEFAULT_OBJECT_STORAGE_BUCKET_ID", "").strip()
    if _bucket_id:
        _client = Client(bucket_id=_bucket_id)
        _use_objstore = True
        logger.info(f"✅ Object Storage ativo → bucket {_bucket_id}")
    else:
        logger.info("📁 Armazenamento local (filesystem) — DEFAULT_OBJECT_STORAGE_BUCKET_ID não configurado")
except Exception as e:
    logger.warning(f"⚠️ Object Storage indisponível ({e}) — usando filesystem local")
    _client = None
    _use_objstore = False


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

    if _use_objstore:
        try:
            _client.upload_from_bytes(norm_key, data)
            logger.debug(f"Storage (objstore): salvo {key} ({len(data)} bytes)")
            return key
        except Exception as e:
            logger.error(f"Storage: falha ao salvar {key} no Object Storage: {e}")
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

    if _use_objstore:
        try:
            if _client.exists(norm_key):
                data = _client.download_as_bytes(norm_key)
                return io.BytesIO(data)
        except Exception as e:
            logger.error(f"Storage: erro ao ler {key} do Object Storage: {e}")
        # fallback: tenta filesystem local (compatibilidade com registros antigos)
        if os.path.exists(key):
            with open(key, 'rb') as f:
                return io.BytesIO(f.read())
        logger.warning(f"Storage: arquivo não encontrado (objstore): {key}")
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

    if _use_objstore:
        try:
            if _client.exists(norm_key):
                return True
        except Exception as e:
            logger.error(f"Storage: erro ao verificar {key} no Object Storage: {e}")
        return os.path.exists(key)

    return os.path.exists(key)


def delete_file(key: str):
    """Remove o arquivo do armazenamento (sem erro se não existir)."""
    if not key:
        return
    norm_key = _normalize_key(key)

    if _use_objstore:
        try:
            if _client.exists(norm_key):
                _client.delete(norm_key)
                return
        except Exception as e:
            logger.error(f"Storage: erro ao deletar {key} do Object Storage: {e}")

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

    if _use_objstore:
        try:
            if _client.exists(norm_key):
                data = _client.download_as_bytes(norm_key)
                return len(data)
        except Exception as e:
            logger.error(f"Storage: erro ao obter tamanho de {key} no Object Storage: {e}")

    try:
        if os.path.exists(key):
            return os.path.getsize(key)
    except OSError:
        pass
    return 0
