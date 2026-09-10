"""Exporta o schema SQLAlchemy atual como DDL compatível com PostgreSQL."""

from pathlib import Path
import sys

from sqlalchemy import create_mock_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db
import models  # noqa: F401


def render_schema() -> str:
    statements = []

    def collect(sql, *multiparams, **params):
        compiled = sql.compile(dialect=engine.dialect)
        statement = str(compiled).strip()
        if statement:
            statements.append(statement.rstrip(";") + ";")

    engine = create_mock_engine("postgresql+psycopg2://", collect)
    db.metadata.create_all(engine, checkfirst=False)
    statements.extend(
        [
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_driver_bids_active_driver "
                "ON driver_bids (driver_id) "
                "WHERE status IN ('sent', 'responded', 'no_price');"
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_external_message "
                "ON whatsapp_messages (external_message_id) "
                "WHERE external_message_id IS NOT NULL;"
            ),
        ]
    )
    header = (
        "-- EMALOG - Schema PostgreSQL/AWS Aurora\n"
        "-- Gerado exclusivamente dos modelos SQLAlchemy; não contém dados.\n"
        "-- Execute em um banco vazio com um usuário autorizado a criar tabelas.\n\n"
    )
    return header + "\n\n".join(statements) + "\n"


if __name__ == "__main__":
    destination = ROOT / "database" / "schema_postgresql.sql"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_schema(), encoding="utf-8")
    print(f"Schema gerado em {destination}")