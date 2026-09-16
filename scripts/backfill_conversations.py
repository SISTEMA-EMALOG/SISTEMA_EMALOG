"""
Backfill da Fase 0 da Central de Atendimento.

Agrupa as mensagens já existentes em whatsapp_messages em linhas de
conversations e preenche whatsapp_messages.conversation_id.

NÃO roda no boot da app de propósito. Percorrer a tabela inteira dentro do
laço de inicialização de infraestrutura_critica/app.py bloquearia o worker e
poderia reprovar o health check do Elastic Beanstalk. É uma operação
deliberada, rodada uma vez por banco.

É idempotente: só toca em mensagens com conversation_id nulo, então pode ser
repetido com segurança e retomado se for interrompido.

Uso:
    python3 scripts/backfill_conversations.py --dry-run
    python3 scripts/backfill_conversations.py
    python3 scripts/backfill_conversations.py --reabrir-dias 0   # tudo resolvido

Rode a partir da RAIZ do projeto. A app resolve templates/ e static/ a partir
do diretório-pai de infraestrutura_critica/.
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infraestrutura_critica.app import create_app, db
from infraestrutura_critica.models import (
    Conversation,
    Driver,
    WhatsAppMessage,
    CONVERSATION_STATUS_OPEN,
    CONVERSATION_STATUS_RESOLVED,
)
from atendimento_conversas.utils.phone import (
    normalize_contact_key,
    find_driver_by_phone,
)

LOTE = 500

# Carimbo para conversa sem nenhuma data de envio conhecida.
EPOCA = datetime(1970, 1, 1)


def _resolver_motorista(msg, cache):
    """Motorista da mensagem: o já ligado, senão busca pelo telefone."""
    if msg.driver_id:
        return msg.driver_id

    chave = normalize_contact_key(msg.phone_number)
    if not chave:
        return None

    if chave not in cache:
        motorista = find_driver_by_phone(chave)
        cache[chave] = motorista.id if motorista else None

    return cache[chave]


def executar(reabrir_dias, dry_run):
    corte = datetime.utcnow() - timedelta(days=reabrir_dias) if reabrir_dias > 0 else None

    conversas = {}      # chave -> Conversation
    atividade = {}      # chave -> maior sent_at visto, ou None se nenhum
    cache_motorista = {}
    total_msgs = 0
    ignoradas = 0

    pendentes = (
        WhatsAppMessage.query
        .filter(WhatsAppMessage.conversation_id.is_(None))
        .count()
    )
    print(f"Mensagens sem conversa: {pendentes}")
    if not pendentes:
        print("Nada a fazer.")
        return

    ultimo_id = 0
    while True:
        lote = (
            WhatsAppMessage.query
            .filter(WhatsAppMessage.conversation_id.is_(None))
            .filter(WhatsAppMessage.id > ultimo_id)
            .order_by(WhatsAppMessage.id)
            .limit(LOTE)
            .all()
        )
        if not lote:
            break

        for msg in lote:
            ultimo_id = msg.id
            chave = normalize_contact_key(msg.phone_number)
            if not chave:
                ignoradas += 1
                continue

            conversa = conversas.get(chave)
            if conversa is None:
                conversa = Conversation.query.filter_by(contact_phone=chave).first()

            if conversa is None:
                driver_id = _resolver_motorista(msg, cache_motorista)
                conversa = Conversation(
                    contact_phone=chave,
                    driver_id=driver_id,
                    status=CONVERSATION_STATUS_RESOLVED,
                    last_activity_at=msg.sent_at or EPOCA,
                )
                # Atribuição e modo vêm do cadastro do motorista, que era a
                # fonte de verdade antes desta fase.
                if driver_id:
                    motorista = db.session.get(Driver, driver_id)
                    if motorista is not None:
                        conversa.assigned_agent_id = motorista.whatsapp_assigned_to
                        conversa.handling_mode = motorista.whatsapp_mode or 'auto'
                        if motorista.whatsapp_assigned_to:
                            conversa.assigned_at = motorista.created_at
                if not dry_run:
                    db.session.add(conversa)
                    db.session.flush()
                conversas[chave] = conversa

            # Atividade mais recente da conversa. Registrada fora do objeto
            # porque Conversation.last_activity_at tem default=utcnow: deixar
            # a coluna nula faria o default disparar e uma conversa sem
            # nenhum carimbo de tempo pareceria ativa hoje, entrando na fila.
            quando = msg.sent_at
            anterior = atividade.get(chave)
            if quando and (anterior is None or quando > anterior):
                atividade[chave] = quando
            elif chave not in atividade:
                atividade[chave] = anterior

            if conversa.driver_id is None and msg.driver_id:
                conversa.driver_id = msg.driver_id

            if not dry_run:
                msg.conversation_id = conversa.id

            total_msgs += 1

        if not dry_run:
            db.session.commit()
        print(f"  ... {total_msgs}/{pendentes} mensagens, "
              f"{len(conversas)} conversas, último id {ultimo_id}")

    # Carimba a atividade real e decide quais conversas entram na fila.
    # Sem carimbo de tempo nenhum, a conversa continua resolvida: é histórico
    # velho, não trabalho pendente.
    reabertas = 0
    sem_data = 0
    for chave, conversa in conversas.items():
        ultima = atividade.get(chave)
        if ultima is None:
            sem_data += 1
            # last_activity_at é NOT NULL. Sem carimbo conhecido, a época
            # afunda a conversa na ordenação em vez de fingir atividade hoje.
            conversa.last_activity_at = EPOCA
        else:
            conversa.last_activity_at = ultima

        if corte is not None and ultima is not None and ultima >= corte:
            conversa.status = CONVERSATION_STATUS_OPEN
            conversa.resolved_at = None
            reabertas += 1
        else:
            conversa.status = CONVERSATION_STATUS_RESOLVED
            conversa.resolved_at = ultima
    if not dry_run:
        db.session.commit()

    print()
    print(f"Conversas criadas ....... {len(conversas)}")
    print(f"Mensagens ligadas ....... {total_msgs}")
    print(f"Sem telefone utilizável . {ignoradas}")
    print(f"Sem data de envio ....... {sem_data} (mantidas resolvidas)")
    print(f"Reabertas ............... {reabertas} "
          f"(atividade nos últimos {reabrir_dias} dias)")
    if dry_run:
        print()
        print("DRY-RUN: nada foi gravado.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='simula sem gravar')
    parser.add_argument('--reabrir-dias', type=int, default=7,
                        help='conversas com atividade nos últimos N dias ficam '
                             'abertas; 0 deixa tudo resolvido (padrão: 7)')
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        executar(args.reabrir_dias, args.dry_run)


if __name__ == '__main__':
    sys.exit(main())
