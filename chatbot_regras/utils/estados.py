"""
Fase 5, etapa 5.2 — um handler por estado da raiz "Oferta de frete por região".

Estados construídos até aqui: [1] entrada, [2] identificacao, [3] coleta_minima,
[4] viagem_ativa, [11] encerramento e [12] handoff. Qualquer transição que o
contrato levaria a um estado ainda NÃO construído (5 geolocalizacao, 6..10 frete,
e os caminhos [A][B][C][R]) cai em handoff, com um motivo claro.

[3] coleta_minima é NATIVA e por seleção de opções — sem nenhum envolvimento do
agente EMA (que não consome API neste sistema). O bot só junta o mínimo para
INICIAR o cadastro (nome + veículo + cidade/UF); o atendente humano finaliza
depois do handoff.

Cada handler recebe o `ctx` (montado em maquina.py) e devolve um `Passo`, que a
máquina interpreta e PERSISTE. O handler NÃO envia WhatsApp, NÃO grava
WhatsAppMessage e NÃO faz commit — só decide o próximo passo e devolve os textos.

O contrato completo, com o porquê de cada decisão, está em
atendimento_conversas/FASE5_CHATBOT.md.
"""
import re
from datetime import date

from infraestrutura_critica.app import db
from infraestrutura_critica.models import (
    Driver, Freight, DriverBid, OptOut, BotContato,
    BOT_ESTADO_IDENTIFICACAO, BOT_ESTADO_COLETA_MINIMA, BOT_ESTADO_VIAGEM_ATIVA,
    BOT_ESTADO_ENCERRAMENTO, BOT_ESTADO_HANDOFF,
    BOT_SESSAO_HANDOFF, BOT_SESSAO_ENCERRADA, BOT_SESSAO_OPTOUT,
    BOT_CONTATO_OPTOUT,
)
from chatbot_regras.utils import mensagens


# ── O resultado de um handler ───────────────────────────────────────────────
# tipo:
#   'ir'       muda de estado. Se automatico=True, a máquina processa o novo
#              estado na hora (transição silenciosa, como identificacao e
#              viagem_ativa); se False, para e espera a próxima mensagem.
#   'terminar' estado final: grava status (handoff/encerrada/optout) e motivo.
#   'aguardar' fica no estado e conta uma tentativa não entendida (plumbing das
#              "2 tentativas por estado"; nenhum handler da 5.2 usa ainda — os
#              estados que interpretam texto livre são das etapas 5.3+).
class Passo:
    __slots__ = ('tipo', 'estado', 'status', 'motivo', 'textos', 'automatico')

    def __init__(self, tipo, *, estado=None, status=None, motivo=None,
                 textos=None, automatico=False):
        self.tipo = tipo
        self.estado = estado
        self.status = status
        self.motivo = motivo
        self.textos = list(textos) if textos else []
        self.automatico = automatico


def ir(estado, *, automatico=False, textos=None):
    return Passo('ir', estado=estado, textos=textos, automatico=automatico)


def handoff(*, motivo, textos=None):
    """[12] handoff — entrega à Central. O bot para de responder.

    Sem texto por padrão: o contrato não redige mensagem para o handoff genérico
    (a conversa vira 'manual' e quem fala a seguir é o atendente). O único texto
    de despedida do contrato é o do [11] encerramento, para o fim da reserva.
    """
    return Passo('terminar', estado=BOT_ESTADO_HANDOFF,
                 status=BOT_SESSAO_HANDOFF, motivo=motivo, textos=textos)


def encerrar(*, motivo, textos=None, estado=None):
    """Estado final sem handoff (ex.: motorista em viagem, opt-out)."""
    return Passo('terminar', estado=estado, status=BOT_SESSAO_ENCERRADA,
                 motivo=motivo, textos=textos)


def aguardar(*, textos=None):
    return Passo('aguardar', textos=textos)


# ── Helpers de contexto ─────────────────────────────────────────────────────

def _anota_contexto(session, **kv):
    """Grava chaves no contexto JSON reatribuindo o dict (dispara a detecção de
    mudança do SQLAlchemy sem precisar de flag_modified)."""
    atual = dict(session.contexto or {})
    atual.update(kv)
    session.contexto = atual


# ════════════════════════════════════════════════════════════════════════════
# [1] entrada
# ════════════════════════════════════════════════════════════════════════════
def estado_entrada(ctx, entrada_usuario):
    """A mensagem de abertura já saiu no disparo da campanha; aqui chega a
    resposta do motorista. Qualquer resposta (que não seja uma saída global,
    tratada antes) segue para a identificação — silenciosa."""
    return ir(BOT_ESTADO_IDENTIFICACAO, automatico=True)


# ════════════════════════════════════════════════════════════════════════════
# [2] identificacao — sem mensagem; resolve o telefone contra o cadastro
# ════════════════════════════════════════════════════════════════════════════
def estado_identificacao(ctx, entrada_usuario):
    driver = ctx.driver  # normalize_contact_key + find_driver_by_phone (lazy)

    if driver is None:
        # Sem cadastro: o PRÓPRIO bot inicia o cadastro (coleta mínima nativa),
        # sem agente EMA. Cria o registro mínimo (nome provisório + telefone) e
        # segue para [3] coleta_minima. O atendente humano finaliza no handoff.
        driver = _criar_cadastro_minimo(ctx)
        ctx.session.driver_id = driver.id
        ctx._driver = driver            # os próximos handlers do mesmo laço já o veem
        ctx._driver_resolvido = True
        _anota_contexto(ctx.session, cadastro='sem_cadastro')
        return ir(BOT_ESTADO_COLETA_MINIMA, automatico=True)

    # Vincula o motorista à sessão (o painel de handoff usa isso).
    ctx.session.driver_id = driver.id

    if not _cadastro_completo(driver):
        # Existe, mas falta campo obrigatório: o bot coleta o mínimo que falta e
        # entrega ao atendente, que finaliza. (Decisão 2026-09-18.)
        _anota_contexto(ctx.session, cadastro='incompleto')
        return ir(BOT_ESTADO_COLETA_MINIMA, automatico=True)

    # Cadastro completo → [4] viagem_ativa (silencioso).
    _anota_contexto(ctx.session, cadastro='completo')
    return ir(BOT_ESTADO_VIAGEM_ATIVA, automatico=True)


def _criar_cadastro_minimo(ctx):
    """Cria o Driver mínimo de um número novo: nome provisório 'Motorista <4
    dígitos>' (decisão 6 da seção 12) e o telefone normalizado, para o
    find_driver_by_phone reachar depois. validated=False até um operador conferir.
    Só flush (sem commit): a máquina commita ao fim de processar_mensagem_bot."""
    driver = Driver(
        name=f'Motorista {ctx.chave[-4:]}',
        phone=ctx.chave,
        validated=False,
    )
    db.session.add(driver)
    db.session.flush()   # atribui driver.id sem fechar a transação
    return driver


def _cadastro_completo(driver) -> bool:
    """Critério do contrato (seção 2, [2] identificacao): todos os campos
    `required` de FIELD_DEFS aplicáveis preenchidos (respeitando `carreta_only`),
    mais a CNH dentro da validade.

    Reaproveita FIELD_DEFS e _is_field_applicable do EMA para não duplicar a
    definição de "completo". A 5.3 refina se o custo desta consulta incomodar.
    """
    from prospeccao_captacao_motorista.utils.ema_agent import (
        FIELD_DEFS, _is_field_applicable,
    )

    for campo in FIELD_DEFS:
        if not campo.get('required'):
            continue
        if not _is_field_applicable(campo, driver):
            continue
        valor = getattr(driver, campo['key'], None)
        if valor in (None, ''):
            return False

    # CNH dentro da validade (cnh_expiry é um required de FIELD_DEFS, mas ali só
    # conta como "preenchido"; aqui exigimos que ainda não tenha vencido).
    venc = getattr(driver, 'cnh_expiry', None)
    if venc is None:
        return False
    venc = venc.date() if hasattr(venc, 'date') else venc
    return venc >= date.today()


# ════════════════════════════════════════════════════════════════════════════
# [3] coleta_minima — coleta NATIVA do mínimo (nome + veículo + cidade/UF)
# ════════════════════════════════════════════════════════════════════════════
# Sem agente EMA, por seleção de opções. Pergunta SÓ o que falta, um campo por
# mensagem, grava no Driver e entrega ao atendente, que finaliza o cadastro.
# As 27 UFs, para validar a cidade/UF. Constante nativa (não vem do EMA).
_UFS = {
    'AC', 'AL', 'AP', 'AM', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MT', 'MS', 'MG',
    'PA', 'PB', 'PR', 'PE', 'PI', 'RJ', 'RN', 'RS', 'RO', 'RR', 'SC', 'SP', 'SE',
    'TO',
}

_PROMPT = {
    'nome':      mensagens.COLETA_NOME,
    'veiculo':   mensagens.COLETA_VEICULO,
    'cidade_uf': mensagens.COLETA_CIDADE_UF,
}
_REASK = {
    'nome':      mensagens.COLETA_NOME_INVALIDO,
    'veiculo':   mensagens.COLETA_VEICULO_INVALIDO,
    'cidade_uf': mensagens.COLETA_CIDADE_UF_INVALIDO,
}


def estado_coleta_minima(ctx, entrada_usuario):
    session = ctx.session
    driver = ctx.driver
    if driver is None:
        # Rede de segurança: só se chega aqui com o motorista vinculado.
        return handoff(motivo='cadastro_nao_concluido')

    coleta = dict((session.contexto or {}).get('coleta') or {})
    pendentes = coleta.get('pendentes')

    if pendentes is None:
        # Primeira entrada: descobre o que falta do mínimo e pergunta o primeiro.
        pendentes = _minimos_pendentes(driver)
        if not pendentes:
            # Já tem o mínimo (motorista incompleto por OUTROS campos): entrega
            # direto ao atendente, sem perguntar nada.
            return handoff(motivo='coleta_minima_ok',
                           textos=[mensagens.COLETA_TRANSFERE])
        _set_coleta(session, pendentes, 0)
        return _perguntar(pendentes[0])

    # Coleta em andamento: esta mensagem é a resposta ao campo atual.
    i = coleta.get('i', 0)
    campo = pendentes[i]
    valor, ok = _interpretar(campo, ctx.texto, ctx.texto_norm)
    if not ok:
        # Não entendeu: conta a tentativa (a 3ª vira handoff, na máquina).
        return aguardar(textos=[_REASK[campo]])

    _gravar(driver, campo, valor)
    session.tentativas = 0   # cada pergunta ganha suas próprias 2 tentativas
    i += 1
    if i < len(pendentes):
        _set_coleta(session, pendentes, i)
        return _perguntar(pendentes[i])

    # Terminou o mínimo → handoff para o atendente finalizar o cadastro.
    return handoff(motivo='coleta_minima_ok', textos=[mensagens.COLETA_TRANSFERE])


def _minimos_pendentes(driver):
    """Quais dos 3 mínimos faltam, na ordem em que serão perguntados."""
    faltam = []
    if _nome_pendente(driver):
        faltam.append('nome')
    if not (driver.truck_type or '').strip():
        faltam.append('veiculo')
    if not (driver.state or '').strip():
        faltam.append('cidade_uf')
    return faltam


def _nome_pendente(driver):
    """Nome falta quando está vazio ou ainda é o provisório 'Motorista 8877'."""
    nome = (driver.name or '').strip()
    return (not nome) or bool(re.match(r'^Motorista \d{4}$', nome))


def _set_coleta(session, pendentes, i):
    _anota_contexto(session, coleta={'pendentes': list(pendentes), 'i': i})


def _perguntar(campo):
    """Envia a pergunta do campo e ESPERA a resposta (fica no mesmo estado)."""
    return ir(BOT_ESTADO_COLETA_MINIMA, automatico=False, textos=[_PROMPT[campo]])


def _interpretar(campo, texto, texto_norm):
    """Devolve (valor, ok). ok=False manda re-perguntar (conta tentativa)."""
    if campo == 'nome':
        nome = (texto or '').strip()
        return (nome, True) if len(nome) >= 2 else (None, False)

    if campo == 'veiculo':
        escolha = (texto or '').strip()
        for num, valor, _rot in mensagens.OPCOES_VEICULO:
            if escolha == num:              # respondeu o número da opção
                return valor, True
        for _num, valor, _rot in mensagens.OPCOES_VEICULO:
            if texto_norm == valor:         # ou digitou o próprio tipo ("carreta")
                return valor, True
        return None, False

    if campo == 'cidade_uf':
        cidade, uf = _parse_cidade_uf(texto)
        return ((cidade, uf), True) if uf else (None, False)

    return None, False


def _gravar(driver, campo, valor):
    if campo == 'nome':
        driver.name = valor
    elif campo == 'veiculo':
        driver.truck_type = valor
    elif campo == 'cidade_uf':
        cidade, uf = valor
        driver.state = uf
        if cidade:
            driver.city = cidade


def _parse_cidade_uf(texto):
    """'Campinas SP' / 'Campinas/SP' / 'SP' → (cidade|None, 'SP'|None).

    Acha a UF como o último token de 2 letras que seja uma sigla válida; o que
    vem antes vira a cidade. Só a UF é obrigatória — a cidade refina, e o
    atendente ajusta o resto. A geolocalização (estado 5) terá parse mais rico."""
    if not texto:
        return None, None
    tokens = re.findall(r"[0-9A-Za-zÀ-ÿ]{2,}", texto.strip())
    idx_uf = None
    uf = None
    for i in range(len(tokens) - 1, -1, -1):
        t = tokens[i].upper()
        if len(t) == 2 and t in _UFS:
            uf, idx_uf = t, i
            break
    if uf is None:
        return None, None
    cidade_tokens = tokens[:idx_uf]
    cidade = ' '.join(cidade_tokens).strip().title() if cidade_tokens else None
    return cidade, uf


# ════════════════════════════════════════════════════════════════════════════
# [4] viagem_ativa — sem mensagem se não houver viagem
# ════════════════════════════════════════════════════════════════════════════
def estado_viagem_ativa(ctx, entrada_usuario):
    driver = ctx.driver
    if driver is None:
        # Só se chega aqui com cadastro (via identificacao). Rede de segurança
        # para a origem 'reoferta'/'pedido' que ainda não passa motorista.
        _anota_contexto(ctx.session, proximo_planejado='geolocalizacao')
        return handoff(motivo='cadastro_nao_concluido')

    if _em_viagem(driver):
        # Ocupado: avisa e encerra sem ofertar. O contrato desenha [4]→[11],
        # mas o texto é o próprio do [4] ("boa viagem"), não o do encerramento
        # de reserva. Fecha em bom termo — o motorista volta depois.
        _anota_contexto(ctx.session, motivo='em_viagem')
        return encerrar(motivo='em_viagem',
                        textos=[mensagens.VIAGEM_ATIVA_OCUPADO])

    # Livre → o contrato segue para [5] geolocalizacao, que não existe na 5.2.
    # Handoff é o destino honesto: satisfaz o aceite "número novo entra, vira
    # handoff" também para o cadastro completo e livre.
    _anota_contexto(ctx.session, proximo_planejado='geolocalizacao')
    return handoff(motivo='etapa_nao_implementada')


def _em_viagem(driver) -> bool:
    """Critérios de "ocupado" do contrato (estado [4])."""
    if (driver.availability_status or '') == 'em_frete':
        return True

    tem_frete = Freight.query.filter(
        Freight.assigned_driver_id == driver.id,
        Freight.status.in_(('aceito', 'em_transito')),
    ).first()
    if tem_frete is not None:
        return True

    tem_bid = DriverBid.query.filter(
        DriverBid.driver_id == driver.id,
        DriverBid.status.in_(('sent', 'responded', 'no_price', 'interested')),
    ).first()
    return tem_bid is not None


# ════════════════════════════════════════════════════════════════════════════
# [11] encerramento — fim da reserva; o bot se cala e entrega ao atendente
# ════════════════════════════════════════════════════════════════════════════
def estado_encerramento(ctx, entrada_usuario):
    """Alcançado depois do fluxo de reserva (estados 8-10, ainda não
    construídos). Implementado aqui para a máquina já saber fechar por este
    caminho. Vira status='handoff': daqui em diante quem fala é gente."""
    return Passo('terminar', estado=BOT_ESTADO_ENCERRAMENTO,
                 status=BOT_SESSAO_HANDOFF, motivo='reserva_registrada',
                 textos=[mensagens.ENCERRAMENTO])


# ════════════════════════════════════════════════════════════════════════════
# [12] handoff — rede de segurança se uma sessão já estiver neste estado
# ════════════════════════════════════════════════════════════════════════════
def estado_handoff(ctx, entrada_usuario):
    """Na prática não é chamado: ao entrar em handoff a sessão vira
    status='handoff' e deixa de ser 'ativa', então processar_mensagem_bot não a
    encontra mais. Fica por idempotência."""
    return handoff(motivo='handoff')


# ── Saída global: opt-out (seção 6) ─────────────────────────────────────────
def registrar_e_encerrar_optout(ctx, *, motivo):
    """Grava a recusa (OptOut + BotContato), confirma em uma linha e marca a
    sessão como optout. Checado ANTES do disparo em etapas posteriores; aqui
    trata o "SAIR" dito no meio da conversa."""
    if OptOut.query.filter_by(telefone=ctx.chave).first() is None:
        db.session.add(OptOut(telefone=ctx.chave, origem='bot',
                              motivo='pediu SAIR'))

    # Se a sessão veio de uma campanha, marca o contato para não redisparar.
    for contato in (ctx.session.contato or []):
        contato.status = BOT_CONTATO_OPTOUT

    return Passo('terminar', estado=None, status=BOT_SESSAO_OPTOUT,
                 motivo=motivo, textos=[mensagens.OPTOUT_CONFIRMACAO])


# ── Tabela de despacho: só os estados construídos até aqui ──────────────────
# Estado ausente daqui (geolocalizacao, busca/lista/escolha/confirmacao/reserva,
# e os caminhos alternativos) → a máquina cai em handoff 'etapa_nao_implementada'.
HANDLERS = {
    'entrada':       estado_entrada,
    'identificacao': estado_identificacao,
    'coleta_minima': estado_coleta_minima,
    'viagem_ativa':  estado_viagem_ativa,
    'encerramento':  estado_encerramento,
    'handoff':       estado_handoff,
}
