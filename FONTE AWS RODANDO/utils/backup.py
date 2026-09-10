"""
Backup automático do banco EMALOG.
Exporta todas as tabelas via SQLAlchemy para JSON legível.
Funciona com PostgreSQL (produção) e SQLite (desenvolvimento).
Backups salvos localmente + no Object Storage quando disponível.
"""
import os
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")
MAX_BACKUPS = 30

_SENSITIVE = {"password_hash", "password"}

# Object Storage: diretório privado montado em produção
_private_dir = os.environ.get("PRIVATE_OBJECT_DIR", "").strip()
_obj_backup_dir = os.path.join(_private_dir, "backups") if _private_dir else None


def _serialize(val):
    if isinstance(val, (bytes, bytearray)):
        return None
    if hasattr(val, 'isoformat'):
        return val.isoformat()
    return val


def create_backup():
    """
    Cria backup completo via SQLAlchemy (PostgreSQL ou SQLite).
    Retorna dict com {'ok': bool, 'filename': str, ...}
    """
    try:
        from app import db
        engine = db.engine
        with engine.connect() as conn:
            from sqlalchemy import inspect, text
            inspector = inspect(engine)
            tables = inspector.get_table_names()

            export = {
                "meta": {
                    "created_at": datetime.now().isoformat(),
                    "source": str(engine.url).split("@")[-1] if "@" in str(engine.url) else "local",
                    "version": "2.0",
                    "tables": tables
                },
                "data": {}
            }

            total_rows = 0
            for table in tables:
                try:
                    rows = conn.execute(text(f'SELECT * FROM "{table}"')).mappings().all()
                    table_data = []
                    for row in rows:
                        record = {}
                        for col, val in row.items():
                            if col in _SENSITIVE:
                                record[col] = "***REDACTED***"
                            else:
                                record[col] = _serialize(val)
                        table_data.append(record)
                    export["data"][table] = table_data
                    total_rows += len(table_data)
                except Exception as e:
                    logger.warning(f"Backup: erro ao exportar tabela {table}: {e}")
                    export["data"][table] = []

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"emalog_backup_{ts}.json"
        content = json.dumps(export, ensure_ascii=False, indent=2, default=str).encode("utf-8")

        # Salvar localmente
        os.makedirs(BACKUP_DIR, exist_ok=True)
        local_path = os.path.join(BACKUP_DIR, filename)
        with open(local_path, "wb") as f:
            f.write(content)

        # Salvar também no Object Storage se disponível e gravável
        if _obj_backup_dir:
            try:
                os.makedirs(_obj_backup_dir, exist_ok=True)
                obj_path = os.path.join(_obj_backup_dir, filename)
                with open(obj_path, "wb") as f:
                    f.write(content)
                logger.info(f"☁️ Backup salvo no Object Storage: {obj_path}")
            except OSError:
                pass  # Normal em ambiente de desenvolvimento
            except Exception as e:
                logger.debug(f"Backup Object Storage: {e}")

        _rotate_old_backups()

        logger.info(f"✅ Backup criado: {filename} ({total_rows} linhas, {len(tables)} tabelas)")
        return {
            "ok": True,
            "filename": filename,
            "path": local_path,
            "tables": len(tables),
            "rows": total_rows,
            "created_at": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"❌ Erro ao criar backup: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


def _rotate_old_backups():
    """Remove backups mais antigos, mantendo apenas os últimos MAX_BACKUPS."""
    for backup_dir in [d for d in [BACKUP_DIR, _obj_backup_dir] if d]:
        try:
            if not os.path.exists(backup_dir):
                continue
            files = sorted([
                f for f in os.listdir(backup_dir)
                if f.startswith("emalog_backup_") and f.endswith(".json")
            ])
            while len(files) > MAX_BACKUPS:
                old = os.path.join(backup_dir, files.pop(0))
                os.remove(old)
                logger.info(f"🗑️ Backup antigo removido: {old}")
        except Exception as e:
            logger.warning(f"Rotação de backups falhou em {backup_dir}: {e}")


def list_backups():
    """Retorna lista de backups disponíveis, mesclando local e Object Storage."""
    seen = {}

    for backup_dir, source in [(BACKUP_DIR, "local"), (_obj_backup_dir, "cloud")]:
        if not backup_dir or not os.path.exists(backup_dir):
            continue
        for f in os.listdir(backup_dir):
            if not (f.startswith("emalog_backup_") and f.endswith(".json")):
                continue
            if f in seen:
                continue
            path = os.path.join(backup_dir, f)
            try:
                stat = os.stat(path)
                size_kb = round(stat.st_size / 1024, 1)
                seen[f] = {
                    "filename": f,
                    "size_kb": size_kb,
                    "size_human": f"{size_kb} KB",
                    "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M"),
                    "path": path,
                    "source": source
                }
            except OSError:
                pass

    return sorted(seen.values(), key=lambda x: x["filename"], reverse=True)


def download_backup(filename: str) -> bytes | None:
    """Retorna o conteúdo do backup como bytes (local ou Object Storage)."""
    for backup_dir in [d for d in [BACKUP_DIR, _obj_backup_dir] if d]:
        path = os.path.join(backup_dir, filename)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
    return None


def get_backup_stats():
    """Retorna estatísticas gerais de backup."""
    backups = list_backups()
    obj_available = bool(_obj_backup_dir and os.path.isdir(os.path.dirname(_obj_backup_dir or "")))
    return {
        "total_backups": len(backups),
        "latest": backups[0] if backups else None,
        "db_size_kb": 0,
        "backup_dir": BACKUP_DIR,
        "sqlite_available": False,
        "object_storage": obj_available
    }
