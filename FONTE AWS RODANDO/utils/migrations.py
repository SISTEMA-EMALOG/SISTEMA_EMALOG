"""
Auto-migration utility — detecta colunas faltantes no PostgreSQL e as adiciona.

Funciona comparando o schema real do banco (via information_schema) com as colunas
mapeadas pelos modelos SQLAlchemy, executando ALTER TABLE ADD COLUMN IF NOT EXISTS
para cada coluna ausente.

Só atua em PostgreSQL — SQLite é coberto pelo db.create_all() normalmente.
Nunca remove nem renomeia colunas; apenas adiciona. É seguro rodar em todo startup.
"""

import logging
from datetime import datetime
from sqlalchemy import text, inspect as sa_inspect

log = logging.getLogger(__name__)

# Mapeamento de tipos Python/SQLAlchemy → SQL do PostgreSQL
_TYPE_MAP = {
    'INTEGER':          'INTEGER',
    'BIGINT':           'BIGINT',
    'SMALLINT':         'SMALLINT',
    'VARCHAR':          'VARCHAR',
    'TEXT':             'TEXT',
    'BOOLEAN':          'BOOLEAN',
    'FLOAT':            'DOUBLE PRECISION',
    'NUMERIC':          'NUMERIC',
    'DATETIME':         'TIMESTAMP WITHOUT TIME ZONE',
    'DATE':             'DATE',
    'TIME':             'TIME WITHOUT TIME ZONE',
    'JSON':             'TEXT',
    'JSONB':            'JSONB',
    'LARGEBINARY':      'BYTEA',
    'NULLTYPE':         'TEXT',
}


def _pg_type(col) -> str:
    """Converte o tipo SQLAlchemy de uma coluna para uma declaração SQL PostgreSQL."""
    type_name = type(col.type).__name__.upper()

    if type_name == 'VARCHAR' or type_name == 'STRING':
        length = getattr(col.type, 'length', None)
        return f"VARCHAR({length})" if length else "TEXT"

    if type_name == 'NUMERIC' or type_name == 'DECIMAL':
        prec = getattr(col.type, 'precision', None)
        scale = getattr(col.type, 'scale', None)
        if prec and scale:
            return f"NUMERIC({prec},{scale})"
        return 'NUMERIC'

    return _TYPE_MAP.get(type_name, 'TEXT')


def _get_db_columns(conn, table_name: str) -> set:
    """Retorna o conjunto de nomes de colunas existentes na tabela no banco."""
    result = conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :tbl"
    ), {"tbl": table_name})
    return {row[0] for row in result}


def run_migrations(db) -> None:
    """
    Ponto de entrada principal.
    Deve ser chamado dentro de um app_context() após db.create_all().
    """
    engine = db.engine

    # Só faz sentido em PostgreSQL — SQLite é gerenciado pelo create_all
    if 'postgresql' not in str(engine.url):
        log.debug("⚡ Auto-migrate: banco não é PostgreSQL, ignorado.")
        return

    inspector = sa_inspect(engine)
    tables_in_db = set(inspector.get_table_names())

    added_total = 0

    with engine.connect() as conn:
        for mapper in db.Model.registry.mappers:
            table = mapper.local_table

            if table.name not in tables_in_db:
                # Tabela nova será criada pelo create_all; não precisa de ALTER
                continue

            db_cols = _get_db_columns(conn, table.name)

            for col in table.columns:
                if col.name in db_cols:
                    continue

                # Coluna existe no modelo mas não no banco — adicionar
                sql_type = _pg_type(col)
                nullable = '' if col.nullable else ' NOT NULL'
                default_clause = ''

                if col.server_default is not None:
                    default_clause = f" DEFAULT {col.server_default.arg}"
                elif col.nullable:
                    default_clause = ' DEFAULT NULL'

                ddl = (
                    f'ALTER TABLE "{table.name}" '
                    f'ADD COLUMN IF NOT EXISTS "{col.name}" '
                    f'{sql_type}{default_clause}{nullable};'
                )

                try:
                    conn.execute(text(ddl))
                    conn.commit()
                    log.info(f"🔧 Migration: +{table.name}.{col.name} ({sql_type})")
                    added_total += 1
                except Exception as exc:
                    conn.rollback()
                    log.error(f"❌ Migration falhou em {table.name}.{col.name}: {exc}")

    if added_total:
        log.info(f"✅ Auto-migration concluída: {added_total} coluna(s) adicionada(s).")
    else:
        log.info("✅ Auto-migration: schema já atualizado, nenhuma alteração necessária.")

    # ── Driver contracting Kanban compatibility ─────────────────────────────
    # The generic mapper historically represented new JSON columns as TEXT.
    # Normalize this column before SQLAlchemy starts writing stage histories.
    try:
        data_type = db.session.execute(text("""
            SELECT data_type
              FROM information_schema.columns
             WHERE table_name = 'driver_bids'
               AND column_name = 'kanban_history'
        """)).scalar()
        if data_type in ('text', 'character varying'):
            db.session.execute(text("""
                ALTER TABLE driver_bids
                ALTER COLUMN kanban_history TYPE JSON
                USING CASE
                    WHEN kanban_history IS NULL OR btrim(kanban_history) = '' THEN NULL
                    ELSE kanban_history::json
                END
            """))
        db.session.execute(text("""
            UPDATE driver_bids
               SET kanban_stage = CASE status
                   WHEN 'responded' THEN 'interested'
                   WHEN 'no_price' THEN 'conversation'
                   WHEN 'accepted' THEN 'contracted'
                   WHEN 'declined' THEN 'closed'
                   WHEN 'refused' THEN 'closed'
                   ELSE 'awaiting_response'
               END
             WHERE kanban_stage IS NULL
                OR kanban_stage = ''
        """))
        db.session.execute(text("""
            UPDATE driver_bids
               SET stage_changed_at = COALESCE(updated_at, created_at, NOW())
             WHERE stage_changed_at IS NULL
        """))
        db.session.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_driver_bids_active_driver
                ON driver_bids (driver_id)
             WHERE status IN ('sent', 'responded', 'no_price')
        """))
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.warning(f"⚠️ Migration Central de Contratação: {exc}")

    # Independent protection: a legacy bid-index issue must never prevent
    # WhatsApp webhook idempotency from being installed.
    try:
        db.session.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_external_message
                ON whatsapp_messages (external_message_id)
             WHERE external_message_id IS NOT NULL
        """))
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.error(f"❌ Índice de idempotência do WhatsApp indisponível: {exc}")
        raise

    # Migrar estágios obsoletos do pipeline CRM para 'prospeccao'
    # Os estágios 'contato' e 'reuniao' foram removidos do funil de vendas (são atividades, não estágios)
    try:
        with engine.connect() as conn:
            result = conn.execute(text(
                "UPDATE opportunities SET stage = 'prospeccao' "
                "WHERE stage IN ('contato', 'reuniao')"
            ))
            conn.commit()
            if result.rowcount:
                log.info(f"🔄 CRM: {result.rowcount} oportunidade(s) migrada(s) de 'contato'/'reuniao' → 'prospeccao'")
    except Exception as exc:
        log.warning(f"⚠️ CRM stage migration: {exc}")

    # ── Limpeza: remover leads/opps falsos criados por versão anterior da migração ─
    # A versão anterior criava leads artificiais para clientes diretos/frequentes,
    # poluindo o funil comercial. Removemos esses registros identificados pela nota.
    try:
        from models import Lead as _Lead, Opportunity as _Opp
        fake_leads = _Lead.query.filter(
            _Lead.notes == 'Lead gerado automaticamente (migração CRM)'
        ).all()
        removed_leads = removed_opps = 0
        for fl in fake_leads:
            # Desvincular cotações dessas oportunidades falsas antes de remover
            fake_opps = _Opp.query.filter_by(lead_id=fl.id).all()
            for fo in fake_opps:
                # Remover vínculo das cotações com essa oportunidade falsa
                with engine.connect() as conn:
                    conn.execute(text(
                        "UPDATE quotes SET opportunity_id = NULL WHERE opportunity_id = :oid"
                    ), {'oid': fo.id})
                    conn.commit()
                db.session.delete(fo)
                removed_opps += 1
            db.session.delete(fl)
            removed_leads += 1
        if removed_leads:
            db.session.commit()
            log.info(
                f"🧹 CRM: removidos {removed_leads} lead(s) e {removed_opps} "
                f"oportunidade(s) artificiais de clientes diretos/frequentes"
            )
    except Exception as exc:
        db.session.rollback()
        log.warning(f"⚠️ CRM cleanup: {exc}")

    # ── Auto-link CRM unificado: TODAS as cotações entram no pipeline ─────────────
    # Regra: toda cotação (origem direta ou via lead) aparece no CRM.
    #   • pendente / cotada / negociação → stage "Cotação Enviada" (proposta)
    #   • aprovada                       → stage "Ganho"
    # Para clientes sem lead: cria Lead(status='convertido') automaticamente.
    try:
        from models import Quote as _Quote, Lead as _Lead, Opportunity as _Opp, Client as _Client
        import re as _re

        orphan_quotes = _Quote.query.filter(
            _Quote.opportunity_id.is_(None),
            _Quote.status.notin_(['rejeitada']),
        ).all()

        crm_linked = 0
        for q in orphan_quotes:
            try:
                client = _Client.query.get(q.client_id)
                if not client:
                    continue

                # Buscar lead pré-existente do cliente
                lead = _Lead.query.filter_by(converted_client_id=client.id).first()
                if not lead and client.cnpj:
                    digits = _re.sub(r'\D', '', client.cnpj)
                    if len(digits) == 14:
                        lead = _Lead.query.filter(
                            db.func.regexp_replace(_Lead.cnpj, r'\D', '', 'g') == digits
                        ).first()

                # Sem lead → cliente direto → criar Lead convertido
                if not lead:
                    lead = _Lead(
                        company_name=client.company_name,
                        cnpj=client.cnpj,
                        contact_phone=client.phone,
                        contact_email=client.email,
                        city=client.city,
                        state=client.state,
                        source='cliente_direto',
                        status='convertido',
                        converted_client_id=client.id,
                        notes='Lead criado automaticamente via cotação direta',
                    )
                    db.session.add(lead)
                    db.session.flush()

                origin = ''
                dest   = ''
                if q.origin_city:
                    origin = f"{q.origin_city}, {q.origin_state}" if q.origin_state else q.origin_city
                if q.destination_city:
                    dest = f"{q.destination_city}, {q.destination_state}" if q.destination_state else q.destination_city

                title = f"Cotação {q.quote_number}"
                if origin and dest:
                    title += f" — {origin} → {dest}"

                stage = 'ganho'   if q.status == 'aprovada' else 'proposta'
                prob  = 100       if stage  == 'ganho'      else 50

                opp = _Opp(
                    lead_id=lead.id,
                    title=title,
                    stage=stage,
                    probability=prob,
                    value=q.sale_value or None,
                    freight_origin=origin,
                    freight_destination=dest,
                    vehicle_type=q.vehicle_type or '',
                )
                db.session.add(opp)
                db.session.flush()
                q.opportunity_id = opp.id
                crm_linked += 1
            except Exception as _inner:
                db.session.rollback()
                log.warning(f"⚠️ CRM migration: erro na cotação {q.id}: {_inner}")

        if crm_linked:
            db.session.commit()
            log.info(f"🔗 CRM: {crm_linked} cotação(ões) vinculada(s) ao pipeline (proposta/ganho)")
    except Exception as exc:
        log.warning(f"⚠️ CRM auto-link migration: {exc}")

    # ── Backfill: fretes faltantes para cotações aprovadas ────────────────────────
    # Cotações aprovadas mas sem frete ocorrem quando o commit do frete falhou
    # por erro de banco (ex: VARCHAR muito curto) mas o status já havia sido salvo.
    try:
        from models import Quote as _Quote, Freight as _Freight, Client as _Client
        from datetime import datetime as _dt

        orphan_approved = _Quote.query.filter(
            _Quote.status == 'aprovada',
        ).all()

        # filtrar as que não têm frete
        freights_backfill = 0
        for q in orphan_approved:
            existing = _Freight.query.filter_by(quote_id=q.id).first()
            if existing:
                continue
            try:
                fcount = _Freight.query.count() + 1
                fnum   = f"FRT-{_dt.now().strftime('%Y%m%d')}-{fcount:04d}"

                origin_parts  = [p for p in [q.origin_city, q.origin_state] if p]
                origin_str    = "/".join(origin_parts) if origin_parts else q.origin_cep or "N/I"
                dest_parts    = [p for p in [q.destination_city, q.destination_state] if p]
                dest_str      = "/".join(dest_parts) if dest_parts else q.destination_cep or "N/I"
                product_parts = [p for p in [q.load_type, q.vehicle_type] if p]
                product_str   = " - ".join(product_parts) if product_parts else "Carga geral"

                frt = _Freight(
                    freight_number    = fnum,
                    quote_id          = q.id,
                    client_id         = q.client_id,
                    created_by        = q.approved_by or 1,
                    origin            = origin_str,
                    destination       = dest_str,
                    product           = product_str,
                    weight            = q.load_weight or 0.0,
                    agreed_price      = q.sale_value,
                    driver_cost       = q.driver_cost,
                    origin_city       = q.origin_city,
                    origin_state      = q.origin_state,
                    destination_city  = q.destination_city,
                    destination_state = q.destination_state,
                    pickup_date       = q.pickup_date,
                    status            = 'ofertado',
                )
                db.session.add(frt)
                db.session.commit()
                freights_backfill += 1
                log.info(f"🚛 Backfill: frete {fnum} criado para cotação {q.quote_number}")

                # Atualizar CRM para GANHO junto com o frete
                try:
                    from models import Opportunity as _Opp
                    if q.opportunity_id:
                        opp = _Opp.query.get(q.opportunity_id)
                        if opp and opp.stage not in ('ganho', 'perdido'):
                            opp.stage = 'ganho'
                            opp.probability = 100
                            db.session.commit()
                            log.info(f"🏆 Backfill CRM: oportunidade #{opp.id} → GANHO (cotação {q.quote_number})")
                except Exception as _crm_e:
                    log.warning(f"⚠️ Backfill CRM para cotação {q.id}: {_crm_e}")

            except Exception as _ie:
                db.session.rollback()
                log.warning(f"⚠️ Backfill frete para cotação {q.id}: {_ie}")
        if freights_backfill:
            log.info(f"🚛 Backfill concluído: {freights_backfill} frete(s) criado(s) para cotações aprovadas sem frete")
    except Exception as exc:
        log.warning(f"⚠️ Backfill fretes: {exc}")

    # ── Backfill is_multi_stop NULL → False ───────────────────────────────────
    # is_multi_stop was originally nullable; existing rows may have NULL.
    # Also set the server default so future insertions without the flag get False.
    try:
        db.session.execute(db.text(
            "ALTER TABLE quotes ALTER COLUMN is_multi_stop SET DEFAULT false"
        ))
        result = db.session.execute(db.text(
            "UPDATE quotes SET is_multi_stop = false WHERE is_multi_stop IS NULL"
        ))
        updated = result.rowcount if result.rowcount else 0
        db.session.commit()
        if updated:
            log.info(f"✅ Backfill: {updated} cotação(ões) com is_multi_stop NULL → False")
    except Exception as exc:
        db.session.rollback()
        log.warning(f"⚠️ Backfill is_multi_stop: {exc}")

    # ── Sync CRM: cotações pendentes/cotadas devem estar em PROPOSTA ─────────────
    # Garante que oportunidades de cotações ainda aguardando aprovação fiquem
    # no stage correto ("Cotação Enviada") no pipeline.
    try:
        from models import Quote as _Quote, Opportunity as _Opp
        synced_proposta = 0
        pending_qs = _Quote.query.filter(
            _Quote.status.in_(['pendente', 'cotada']),
            _Quote.opportunity_id.isnot(None),
        ).all()
        for q in pending_qs:
            opp = _Opp.query.get(q.opportunity_id)
            if opp and opp.stage not in ('ganho', 'perdido', 'proposta'):
                opp.stage = 'proposta'
                opp.probability = 50
                synced_proposta += 1
        if synced_proposta:
            db.session.commit()
            log.info(f"📋 CRM sync: {synced_proposta} oportunidade(s) movida(s) para PROPOSTA (cotações pendentes/cotadas)")
    except Exception as exc:
        db.session.rollback()
        log.warning(f"⚠️ CRM sync pendentes: {exc}")

    # ── Follow-up activities: criar para cotações pendentes sem atividade ────────
    # Cotações pendentes são oportunidades abertas — o time precisa de lembrete.
    try:
        from models import Quote as _Quote, Opportunity as _Opp, CRMActivity as _Act, Lead as _Lead
        from datetime import timedelta as _td

        pending_qs = _Quote.query.filter(
            _Quote.status.in_(['pendente', 'cotada']),
            _Quote.opportunity_id.isnot(None),
        ).all()

        created_acts = 0
        for q in pending_qs:
            opp = _Opp.query.get(q.opportunity_id)
            if not opp:
                continue
            # Só cria se não há nenhuma atividade pendente para essa oportunidade
            has_act = _Act.query.filter_by(
                opportunity_id=opp.id, is_done=False
            ).first()
            if has_act:
                continue

            lead = _Lead.query.get(opp.lead_id)
            company = lead.company_name if lead else "cliente"

            act = _Act(
                lead_id        = opp.lead_id,
                opportunity_id = opp.id,
                activity_type  = 'tarefa',
                title          = f"Follow-up — {q.quote_number} aguarda aprovação do cliente",
                notes          = (
                    f"Cotação {q.quote_number} enviada para {company}. "
                    f"Verificar se cliente recebeu e tem interesse em aprovar."
                ),
                scheduled_at   = datetime.utcnow() + _td(days=1),
                is_done        = False,
                created_by     = q.approved_by or q.created_by or 1,
            )
            db.session.add(act)
            created_acts += 1

        if created_acts:
            db.session.commit()
            log.info(f"📌 CRM: {created_acts} atividade(s) de follow-up criada(s) para cotações pendentes")
    except Exception as exc:
        db.session.rollback()
        log.warning(f"⚠️ CRM follow-up activities: {exc}")

    # ── Reset sequências PostgreSQL para evitar UniqueViolation no INSERT ────────
    # Ocorre quando registros foram inseridos com IDs explícitos (seed/migração)
    # e o sequence ficou atrasado. Corrige resincronizando todas as sequências.
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT
                    n.nspname || '.' || c.relname AS seq_name,
                    a.attrelid::regclass AS table_name,
                    a.attname AS column_name
                FROM pg_class c
                JOIN pg_depend d ON d.objid = c.oid AND d.classid = 'pg_class'::regclass
                JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'S'
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            """))
            sequences = result.fetchall()
            reset_count = 0
            for seq in sequences:
                seq_name   = seq[0]
                table_name = str(seq[1]).strip('"')
                col_name   = seq[2]
                try:
                    conn.execute(text(
                        f"SELECT setval('{seq_name}', "
                        f"COALESCE((SELECT MAX({col_name}) FROM \"{table_name}\"), 0) + 1, false)"
                    ))
                    reset_count += 1
                except Exception:
                    pass
            conn.commit()
            if reset_count:
                log.info(f"🔁 Sequências PostgreSQL resincronizadas: {reset_count} tabela(s)")
    except Exception as exc:
        log.warning(f"⚠️ Reset de sequências: {exc}")

    # ── Sync CRM: cotações aprovadas com frete devem estar como GANHO ─────────────
    # Garante consistência mesmo quando o fluxo de aprovação teve falha parcial.
    try:
        from models import Quote as _Quote, Opportunity as _Opp, Freight as _Freight
        out_of_sync = 0
        approved_qs = _Quote.query.filter(
            _Quote.status == 'aprovada',
            _Quote.opportunity_id.isnot(None),
        ).all()
        for q in approved_qs:
            has_freight = _Freight.query.filter_by(quote_id=q.id).first() is not None
            if not has_freight:
                continue
            opp = _Opp.query.get(q.opportunity_id)
            if opp and opp.stage not in ('ganho', 'perdido'):
                opp.stage = 'ganho'
                opp.probability = 100
                out_of_sync += 1
        if out_of_sync:
            db.session.commit()
            log.info(f"🏆 CRM sync: {out_of_sync} oportunidade(s) movida(s) para GANHO (cotações aprovadas com frete)")
    except Exception as exc:
        db.session.rollback()
        log.warning(f"⚠️ CRM sync aprovadas: {exc}")

    # ── Ampliar freight_number de VARCHAR(20) para VARCHAR(30) ───────────────────
    # O formato FRT-YYYYMMDD-NNNN tem 17 chars, mas o UUID legado podia ter 21.
    # Garantir que a coluna suporte pelo menos 30 caracteres.
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT character_maximum_length
                FROM information_schema.columns
                WHERE table_name = 'freights'
                  AND column_name = 'freight_number'
            """))
            row = result.fetchone()
            if row and row[0] and row[0] < 30:
                conn.execute(text(
                    "ALTER TABLE freights ALTER COLUMN freight_number TYPE VARCHAR(30)"
                ))
                conn.commit()
                log.info("✅ Migration: freights.freight_number ampliado para VARCHAR(30)")
    except Exception as exc:
        log.warning(f"⚠️ Migration freight_number: {exc}")

    # Sincronizar valores das oportunidades com as cotações vinculadas
    # Garante que o funil sempre reflita o sale_value da cotação mais recente
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE opportunities o
                SET value = q.sale_value,
                    updated_at = NOW()
                FROM (
                    SELECT DISTINCT ON (opportunity_id)
                        opportunity_id, sale_value
                    FROM quotes
                    WHERE opportunity_id IS NOT NULL
                      AND sale_value IS NOT NULL
                      AND sale_value > 0
                    ORDER BY opportunity_id, created_at DESC
                ) q
                WHERE o.id = q.opportunity_id
                  AND (o.value IS NULL OR o.value != q.sale_value)
            """))
            conn.commit()
            if result.rowcount:
                log.info(f"💰 CRM: {result.rowcount} oportunidade(s) com valor sincronizado com cotação vinculada")
    except Exception as exc:
        log.warning(f"⚠️ CRM value sync: {exc}")

    # ── Tornar colunas do Driver opcionais (só nome+telefone obrigatórios) ────────
    # Permite cadastrar motorista com nome e telefone para que o Agente EMA colete
    # as demais informações via WhatsApp.
    _driver_optional_cols = [
        'cpf', 'rg', 'birth_date',
        'cep', 'street', 'number', 'neighborhood', 'city', 'state',
        'cnh_expiry', 'truck_type',
        'vehicle_plate', 'vehicle_model', 'vehicle_year',
    ]
    try:
        with engine.connect() as conn:
            # Find which columns still have NOT NULL
            result = conn.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'drivers'
                  AND is_nullable = 'NO'
                  AND column_name = ANY(:cols)
            """), {'cols': _driver_optional_cols})
            not_null_cols = [row[0] for row in result]
            for col in not_null_cols:
                conn.execute(text(
                    f'ALTER TABLE drivers ALTER COLUMN "{col}" DROP NOT NULL'
                ))
                log.info(f"🔧 Migration: drivers.{col} → nullable (NOT NULL removido)")
            if not_null_cols:
                conn.commit()
    except Exception as exc:
        log.warning(f"⚠️ Migration drivers nullable: {exc}")

    # ── Índices de performance: chat e financeiro ────────────────────────────────
    # Colunas frequentemente filtradas em routes/chat.py e routes/financial.py
    # que estavam sem índice explícito, causando full scans em tabelas grandes.
    _perf_indexes = [
        ('idx_chat_messages_room', 'chat_messages', 'room'),
        ('idx_chat_messages_is_read', 'chat_messages', 'is_read'),
        ('idx_payments_payment_type', 'payments', 'payment_type'),
        ('idx_payments_due_date', 'payments', 'due_date'),
    ]
    try:
        with engine.connect() as conn:
            for idx_name, table_name, col_name in _perf_indexes:
                conn.execute(text(
                    f'CREATE INDEX IF NOT EXISTS {idx_name} ON "{table_name}" ("{col_name}")'
                ))
            conn.commit()
            log.info("✅ Migration: índices de performance (chat/financeiro) verificados/criados")
    except Exception as exc:
        log.warning(f"⚠️ Migration índices de performance: {exc}")
