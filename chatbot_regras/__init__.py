"""
Chatbot de regras (Fase 5) — raiz "Oferta de frete por região".

Máquina de estados sem IA, com handoff automático para humano quando sai do
previsto. O desenho completo está em atendimento_conversas/FASE5_CHATBOT.md.

Regra de arquitetura: a lógica de atendimento fica AQUI, fora de
infraestrutura_critica; os modelos (BotSession etc.) moram em
infraestrutura_critica/models.py.

Interface pública (é o que o gancho de roteamento chama):
    processar_mensagem_bot(phone, texto, *, conversa, driver, media)
    criar_sessao_bot(phone, *, origem, campanha_id, conversa, driver)
"""
from chatbot_regras.utils.maquina import (
    processar_mensagem_bot, criar_sessao_bot,
)

__all__ = ['processar_mensagem_bot', 'criar_sessao_bot']
