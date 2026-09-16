import json
import logging
import os

log = logging.getLogger(__name__)

SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'seed_data.json')


def run_seed_if_empty(app, db):
    """Popula o banco com dados do seed_data.json se estiver vazio."""
    if not os.path.exists(SEED_FILE):
        return

    import sqlalchemy as sa
    from sqlalchemy import text

    with app.app_context():
        engine = db.engine
        try:
            with engine.connect() as conn:
                count = conn.execute(text('SELECT COUNT(*) FROM clients')).scalar()
                if count and count > 5:
                    log.info(f"🌱 Banco já populado ({count} clientes) — seed ignorado")
                    return
        except Exception:
            return

        log.info("🌱 Banco vazio detectado — iniciando seed de dados...")

        try:
            with open(SEED_FILE, 'r', encoding='utf-8') as f:
                seed = json.load(f)
        except Exception as e:
            log.warning(f"🌱 Falha ao ler seed_data.json: {e}")
            return

        table_order = seed.get('tables', [])
        bool_cols_map = seed.get('bool_cols', {})
        data = seed.get('data', {})

        total = 0
        # Insert in declared order (parents before children) — no superuser needed
        for table in table_order:
            rows = data.get(table, [])
            if not rows:
                continue
            try:
                bcols = bool_cols_map.get(table, [])
                cols = list(rows[0].keys())
                col_names = ', '.join([f'"{c}"' for c in cols])
                placeholders = ', '.join([f':{c}' for c in cols])

                inserted = 0
                with engine.begin() as conn:
                    for row in rows:
                        vals = {}
                        for col, val in row.items():
                            if col in bcols and val is not None:
                                vals[col] = bool(val) if not isinstance(val, bool) else val
                            else:
                                vals[col] = val
                        conn.execute(
                            text(f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'),
                            vals
                        )
                        inserted += 1
                log.info(f"  🌱 {table}: {inserted} registros inseridos")
                total += inserted
            except Exception as e:
                log.warning(f"  🌱 {table}: erro — {str(e)[:120]}")

        # Reset sequences after all inserts
        with engine.begin() as conn:
            for table in table_order:
                try:
                    conn.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('\"{table}\"','id'), "
                        f"COALESCE((SELECT MAX(id) FROM \"{table}\"),1))"
                    ))
                except Exception:
                    pass

        log.info(f"🌱 Seed concluído: {total} registros inseridos")
