"""
Fase 5 — disparo do chatbot para UM número.

É a fatia mínima da campanha (etapa 5.8) que torna o chatbot testável de ponta a
ponta: abre a `BotSession`, manda a mensagem [1] de entrada e registra tudo na
Central (como `WhatsAppMessage source='bot'` numa conversa), para o operador
acompanhar. O disparo em lote, com folga e teto diário, fica para a 5.8.

A mensagem [1] é enviada AQUI (é o disparo da campanha), não pela máquina de
estados — o estado `entrada` só reage à RESPOSTA do motorista.
"""
import logging

from infraestrutura_critica.app import db
from infraestrutura_critica.models import (
    WhatsAppMessage, OptOut, BotSession,
    BOT_SESSAO_ATIVA, BOT_ORIGEM_CAMPANHA,
)
from atendimento_conversas.utils.phone import (
    normalize_contact_key, find_driver_by_phone,
)
from chatbot_regras.utils import mensagens
from chatbot_regras.utils.maquina import criar_sessao_bot

logger = logging.getLogger(__name__)


def iniciar_para_numero(telefone, *, criado_por=None):
    """Inicia o chatbot para um número. Devolve (ok, motivo, bot_session_id).

    motivo ∈ {numero_invalido, optout, ja_ativa, enviado, erro_envio}.
    """
    chave = normalize_contact_key(telefone)
    if not chave:
        return False, 'numero_invalido', None

    # Opt-out é checado ANTES do disparo, não só durante a conversa (seção 6).
    if OptOut.query.filter_by(telefone=chave).first() is not None:
        return False, 'optout', None

    # Uma sessão ativa por telefone: não abre uma segunda.
    ativa = BotSession.query.filter_by(
        telefone=chave, status=BOT_SESSAO_ATIVA).first()
    if ativa is not None:
        return False, 'ja_ativa', ativa.id

    driver = find_driver_by_phone(chave)

    # Conversa da Central, para o histórico aparecer lá desde a primeira mensagem.
    conversa = None
    try:
        from atendimento_conversas.utils.conversas_service import conversa_para_entrada
        conversa, _nova = conversa_para_entrada(chave, driver=driver)
    except Exception as exc:
        db.session.rollback()
        logger.warning('[Chatbot] falha ao abrir conversa de %s: %s', chave, exc)

    sessao = criar_sessao_bot(chave, origem=BOT_ORIGEM_CAMPANHA,
                              conversa=conversa, driver=driver)

    # Manda a mensagem [1] de entrada e grava como WhatsAppMessage source='bot'
    # (sem isso some da Central — defeito 3.1).
    from infraestrutura_critica.utils.evolution_api import send_text
    ok = send_text(chave, mensagens.ENTRADA)
    db.session.add(WhatsAppMessage(
        driver_id=driver.id if driver else None,
        phone_number=chave, message_content=mensagens.ENTRADA,
        direction='outbound', source='bot',
        status='enviado' if ok else 'erro',
        conversation_id=conversa.id if conversa is not None else None,
        created_by=criado_por,
    ))
    db.session.commit()
    logger.info('[Chatbot] iniciado para %s — envio ok=%s sessao=%s',
                chave, ok, sessao.id)
    return (ok, 'enviado' if ok else 'erro_envio', sessao.id)
