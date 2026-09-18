"""
Fase 5 — os textos que a máquina de regras devolve ao motorista.

Os textos são os do contrato (atendimento_conversas/FASE5_CHATBOT.md), redigidos
literalmente e terminando em "EMA | EMALOG". As quebras de linha do documento que
caíam no meio de uma frase (limite de 80 colunas do markdown) foram desfeitas:
o que vale é a mensagem que chega no WhatsApp, não a largura da página.

A máquina NÃO envia WhatsApp e NÃO grava WhatsAppMessage — ela só devolve estes
textos para quem a chamou (o gancho de roteamento, em outra etapa).
"""

ASSINATURA = "EMA | EMALOG"


# ── [1] entrada ─────────────────────────────────────────────────────────────
# Disparada pela campanha (etapa 5.8). O "SAIR" não é gentileza: lista fria sem
# saída explícita vira denúncia e bloqueio do número da empresa no WhatsApp.
ENTRADA = (
    "Oi! Aqui é a EMA, da Emalog Transportes. 🚚\n"
    "A gente trabalha com cargas em todo o Brasil e está montando a lista de "
    "motoristas parceiros.\n"
    "Você trabalha com transporte de carga hoje?\n"
    "Se não quiser receber mensagens, responde SAIR que eu não te chamo mais.\n"
    "\n"
    "EMA | EMALOG"
)


# ── [4] viagem_ativa: motorista ocupado ─────────────────────────────────────
# Ofertar frete a quem já está carregado é o jeito mais rápido de o bot perder a
# confiança do motorista.
VIAGEM_ATIVA_OCUPADO = (
    "Vi aqui que você está com um frete nosso em andamento. Boa viagem! 🚚\n"
    "Quando entregar, me manda um \"livre\" que eu te mostro as próximas cargas.\n"
    "\n"
    "EMA | EMALOG"
)


# ── [11] encerramento ───────────────────────────────────────────────────────
# Fim do fluxo de reserva (estados 8-10, ainda não construídos). O bot NÃO fala
# de reserva: posição, prazo e vencimento são assunto do atendente humano. Aqui
# o bot só avisa que está passando a conversa e se cala.
ENCERRAMENTO = (
    "Boa, anotei aqui! ✅\n"
    "Já estou passando para um da equipe, que fala com você em seguida sobre "
    "essa carga.\n"
    "É só responder nesta mesma conversa.\n"
    "\n"
    "EMA | EMALOG"
)


# ── [3] coleta_minima ───────────────────────────────────────────────────────
# NATIVA, por seleção de opções — sem nenhum envolvimento do agente EMA. O bot só
# junta o mínimo (nome + veículo + cidade/UF) para INICIAR o cadastro; o atendente
# humano finaliza depois do handoff. Só pergunta o que falta.

# As opções de veículo usam o vocabulário canônico do sistema
# (TRUCK_TYPES em prospeccao_captacao_motorista/utils/ema_agent.py): é o mesmo
# valor que o cadastro, os filtros e o matching leem. Ordem e rótulos ficam aqui,
# junto do texto; estados.py importa esta lista para casar a resposta.
#   (numero, valor_canonico, rotulo)
OPCOES_VEICULO = [
    ('1', '3/4',     '3/4'),
    ('2', 'vlc',     'VLC'),
    ('3', 'toco',    'Toco'),
    ('4', 'truck',   'Truck'),
    ('5', 'bitruck', 'Bitruck'),
    ('6', 'carreta', 'Carreta'),
]

_LINHAS_VEICULO = "\n".join(f"{num} - {rotulo}" for num, _valor, rotulo in OPCOES_VEICULO)

COLETA_NOME = (
    "Boa! Pra começar seu cadastro, como é seu nome completo?\n"
    "\n"
    "EMA | EMALOG"
)

COLETA_NOME_INVALIDO = (
    "Não peguei seu nome. Me manda seu nome completo, por favor.\n"
    "\n"
    "EMA | EMALOG"
)

COLETA_VEICULO = (
    "E você roda com qual veículo? Responde só o número:\n"
    f"{_LINHAS_VEICULO}\n"
    "\n"
    "EMA | EMALOG"
)

COLETA_VEICULO_INVALIDO = (
    "Não entendi. Responde só o número do veículo:\n"
    f"{_LINHAS_VEICULO}\n"
    "\n"
    "EMA | EMALOG"
)

COLETA_CIDADE_UF = (
    "De qual cidade/UF você costuma sair? (ex.: Campinas SP)\n"
    "\n"
    "EMA | EMALOG"
)

COLETA_CIDADE_UF_INVALIDO = (
    "Preciso da cidade e do estado (a sigla). Ex.: Campinas SP — ou só a sigla, SP.\n"
    "\n"
    "EMA | EMALOG"
)

# Fim da coleta: o bot passa a conversa para um atendente finalizar o cadastro.
# É o handoff do estado [3] com texto (o handoff genérico não fala nada).
COLETA_TRANSFERE = (
    "Anotado, obrigado! ✅\n"
    "Já estou passando você para um atendente da equipe, que finaliza seu "
    "cadastro e te mostra as cargas.\n"
    "É só responder nesta mesma conversa.\n"
    "\n"
    "EMA | EMALOG"
)


# ── opt-out ─────────────────────────────────────────────────────────────────
# NÃO é literal do contrato: a seção 6 pede "confirma em uma linha", sem redigir
# a frase. Uma linha curta, para a 5.x refinar se quiser.
OPTOUT_CONFIRMACAO = (
    "Pronto, não te chamo mais por aqui. Se mudar de ideia, é só me responder. 👋\n"
    "\n"
    "EMA | EMALOG"
)
