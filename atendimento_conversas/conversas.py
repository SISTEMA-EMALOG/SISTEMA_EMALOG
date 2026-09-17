"""
Central de Atendimento — blueprint do módulo.

Fase 0: modelagem. Fase 1: entrada e saída de WhatsApp e a tela.
Fase 2: fila compartilhada entre atendentes, com a disputa decidida no banco
(ver utils/fila.py).

Regra de arquitetura: este módulo só tem lógica de atendimento. Núcleo,
modelos e utilitários compartilhados vêm de infraestrutura_critica/.
"""

import logging
import re
from datetime import datetime
from types import SimpleNamespace

from flask import Blueprint, Response, jsonify, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, inspect as sa_inspect

from infraestrutura_critica.app import db
from infraestrutura_critica.models import (
    Conversation,
    Driver,
    MessageAttachment,
    WhatsAppMessage,
    ANEXO_ARMAZENADO,
    CONVERSATION_ACTIVE_STATUSES,
    CONVERSATION_STATUS_OPEN,
    CONVERSATION_STATUS_RESOLVED,
)
from atendimento_conversas.utils import anexos, eventos_mensagem, fila, providers, twilio_client
from atendimento_conversas.utils.conversas_service import (
    avisar_conversa,
    conversa_para_entrada,
    marcar_lidas,
    mensagem_ja_processada,
    registrar_entrada,
    registrar_saida,
    serializar_conversa,
    serializar_mensagem,
)

log = logging.getLogger(__name__)

conversas_bp = Blueprint('conversas', __name__, url_prefix='/conversas')

# Vínculo automático de mensagem à conversa e aviso em tempo real para
# qualquer mensagem gravada, por qualquer caminho. Ver utils/eventos_mensagem.py.
eventos_mensagem.registrar()

LIMITE_MENSAGEM = 2000
ABAS = ('fila', 'minhas', 'ativas', 'resolvidas', 'todas')


def _staff_only():
    return current_user.role in fila.PAPEIS_ATENDENTE


def _forbidden():
    return jsonify({'success': False, 'error': 'Acesso negado'}), 403


def _ator():
    """
    Retrato de quem age, tirado no início da requisição.

    As operações da fila commitam, e o commit expira todo objeto da sessão,
    inclusive o usuário logado. Ler current_user depois disso dispara uma
    consulta, que abre transação nova. No envio, essa transação ficaria
    aberta durante a chamada HTTP ao provedor.
    """
    return SimpleNamespace(id=current_user.id, role=current_user.role)


def _corpo():
    return request.get_json(silent=True) or {}


def _int_ou_none(valor):
    if valor in (None, '', 'null'):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _responder(res, acao):
    """Resposta JSON de uma operação da fila, com aviso em tempo real."""
    corpo = {'ok': res.ok, 'codigo': res.codigo, 'mensagem': res.mensagem}
    if not res.ok:
        corpo['error'] = res.mensagem
    if res.conversa is not None:
        corpo['conversa'] = serializar_conversa(res.conversa)
    if res.ok and res.codigo == 'ok':
        # Depois do commit, que fila._executar já fez.
        avisar_conversa(res.conversa, extra={'acao': acao})
    return jsonify(corpo), res.http


# ── Tela ─────────────────────────────────────────────────────────────────────

@conversas_bp.route('/')
@login_required
def index():
    if not _staff_only():
        return _forbidden()
    canal = providers.canal_configurado()
    evolution = providers.estado_evolution()
    return render_template(
        'conversas/index.html',
        canal_ativo=canal,
        # A Evolution pode estar configurada mas com o WhatsApp desconectado.
        # Nesse caso o envio falha, então a tela avisa antes.
        evolution_conectada=(evolution.get('estado') == 'open'),
        evolution_estado=evolution.get('estado'),
    )


# ── Webhook de entrada da Twilio ─────────────────────────────────────────────

def _resposta_twiml():
    """A Twilio espera TwiML. Vazio significa 'recebi, não responda nada'."""
    corpo = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    return corpo, 200, {'Content-Type': 'application/xml'}


@conversas_bp.route('/webhook', methods=['POST'])
def webhook():
    """
    Recebe mensagem de WhatsApp da Twilio.

    Sem @login_required de propósito: é chamada server-to-server. A
    autenticação é a assinatura X-Twilio-Signature, e o caminho está em
    CSRF_EXEMPT_PREFIXES porque não existe sessão de browser aqui.

    Falha fechada: sem TWILIO_AUTH_TOKEN configurado, recusa tudo.
    """
    if not twilio_client.auth_token():
        log.error("❌ Conversas: webhook chamado sem TWILIO_AUTH_TOKEN configurado.")
        return jsonify({'error': 'Integração não configurada'}), 503

    assinatura = request.headers.get('X-Twilio-Signature', '')
    url = twilio_client.webhook_url(request)
    if not twilio_client.validate_signature(url, request.form.to_dict(), assinatura):
        log.warning(
            "⚠️ Conversas: assinatura Twilio inválida. IP %s, URL %s",
            request.remote_addr, url,
        )
        return jsonify({'error': 'Assinatura inválida'}), 403

    dados = twilio_client.parse_inbound(request.form)

    # A Twilio manda form-urlencoded, não JSON.
    if not dados['from_raw']:
        log.warning("⚠️ Conversas: webhook sem campo From.")
        return _resposta_twiml()

    external_id = dados['message_sid'] or None

    # Idempotência: a Twilio reentrega em caso de timeout ou erro 5xx.
    if external_id and mensagem_ja_processada(external_id) is not None:
        log.info(f"↩️ Conversas: mensagem {external_id} já registrada, ignorada.")
        return _resposta_twiml()

    try:
        # Conversa ativa garantida até o commit, logo abaixo.
        conversa, _criada = conversa_para_entrada(
            dados['from_raw'], contact_name=dados['profile_name'] or None,
        )
        if conversa is None:
            log.warning(f"⚠️ Conversas: telefone inutilizável em {dados['from_raw']!r}.")
            return _resposta_twiml()

        texto = dados['body']
        if not texto and dados['num_media']:
            texto = f"[{dados['num_media']} anexo(s)]"

        msg = registrar_entrada(conversa, texto, external_id=external_id,
                                source='twilio')
        # Anexos entram como pendentes NA MESMA transação da mensagem: se o
        # processo cair antes do download, ficam registrados em vez de sumir.
        pendentes = [
            anexos.registrar_pendente(msg, 'twilio', m['url'], content_type=m['content_type'])
            for m in dados['media']
        ]
        db.session.flush()
        # Retrato antes do commit: o download acontece sem transação aberta.
        a_processar = [(a.id, a.provider_ref) for a in pendentes]
        conversa_id = conversa.id
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        # Não logar o conteúdo da mensagem em produção.
        log.error(f"❌ Conversas: falha ao gravar mensagem recebida: {exc}")
        # 500 faz a Twilio reentregar, o que é o comportamento desejado.
        return jsonify({'error': 'Falha ao processar'}), 500

    # O aviso de mensagem nova sai sozinho no commit (utils/eventos_mensagem.py).
    for anexo_id, url in a_processar:
        anexos.processar(anexo_id, conversa_id, lambda u=url: anexos.baixar_da_twilio(u))
    if a_processar:
        avisar_conversa(db.session.get(Conversation, conversa_id), extra={'acao': 'anexo'})
    return _resposta_twiml()


@conversas_bp.route('/webhook/status', methods=['POST'])
def webhook_status():
    """Callback de status de entrega da Twilio."""
    if not twilio_client.auth_token():
        return jsonify({'error': 'Integração não configurada'}), 503

    assinatura = request.headers.get('X-Twilio-Signature', '')
    url = twilio_client.webhook_url(request)
    if not twilio_client.validate_signature(url, request.form.to_dict(), assinatura):
        log.warning("⚠️ Conversas: assinatura inválida no callback de status.")
        return jsonify({'error': 'Assinatura inválida'}), 403

    sid = request.form.get('MessageSid') or request.form.get('SmsSid') or ''
    novo = twilio_client.map_status(request.form.get('MessageStatus'))
    if not sid:
        return _resposta_twiml()

    try:
        msg = WhatsAppMessage.query.filter_by(external_message_id=sid).first()
        if msg is not None:
            msg.status = novo
            if novo == 'entregue' and not msg.delivered_at:
                msg.delivered_at = datetime.utcnow()
            db.session.commit()
            avisar_conversa(msg.conversation, extra={'message_id': msg.id, 'status_msg': novo})
    except Exception as exc:
        db.session.rollback()
        log.warning(f"⚠️ Conversas: falha ao atualizar status de {sid}: {exc}")

    return _resposta_twiml()


# ── API da tela: leitura ─────────────────────────────────────────────────────

def _filtro_ativa():
    return Conversation.status.in_(CONVERSATION_ACTIVE_STATUSES)


def _filtro_busca(busca):
    """
    Busca por nome ou telefone, em qualquer formato que o atendente digitar.

    O telefone da conversa é guardado só em dígitos, com código de país.
    Quem digita '(11) 98888-7777' procuraria o texto com parênteses e não
    acharia nada; por isso os dígitos são extraídos e comparados à parte.

    O nome é procurado no nome do contato e no cadastro do motorista. EXISTS
    via has(), e não join, para não duplicar linhas. Tudo portável.
    """
    like = f'%{busca}%'
    condicoes = [
        Conversation.contact_name.ilike(like),
        Conversation.driver.has(Driver.name.ilike(like)),
    ]
    digitos = re.sub(r'\D', '', busca)
    if len(digitos) >= 3:
        condicoes.append(Conversation.contact_phone.like(f'%{digitos}%'))
    return or_(*condicoes)


@conversas_bp.route('/api/conversas')
@login_required
def listar():
    """
    Conversas de uma aba, mais recentes primeiro.

    fila: ativas sem dono. minhas: ativas do usuário. ativas, resolvidas, todas.
    """
    if not _staff_only():
        return _forbidden()

    eu = current_user.id
    aba = (request.args.get('aba') or 'fila').strip()
    if aba not in ABAS:
        aba = 'fila'
    busca = (request.args.get('busca') or '').strip()

    q = Conversation.query
    if aba == 'fila':
        q = q.filter(_filtro_ativa(), Conversation.assigned_agent_id.is_(None))
    elif aba == 'minhas':
        q = q.filter(_filtro_ativa(), Conversation.assigned_agent_id == eu)
    elif aba == 'ativas':
        q = q.filter(_filtro_ativa())
    elif aba == 'resolvidas':
        q = q.filter(Conversation.status == CONVERSATION_STATUS_RESOLVED)

    if busca:
        q = q.filter(_filtro_busca(busca))

    contagens = {
        'fila': Conversation.query.filter(
            _filtro_ativa(), Conversation.assigned_agent_id.is_(None)).count(),
        'minhas': Conversation.query.filter(
            _filtro_ativa(), Conversation.assigned_agent_id == eu).count(),
    }

    conversas = q.order_by(Conversation.last_activity_at.desc(),
                           Conversation.id.desc()).limit(200).all()
    if not conversas:
        return jsonify({'conversas': [], 'contagens': contagens, 'aba': aba})

    ids = [c.id for c in conversas]

    nao_lidas = dict(
        db.session.query(WhatsAppMessage.conversation_id, func.count(WhatsAppMessage.id))
        .filter(WhatsAppMessage.conversation_id.in_(ids))
        .filter(WhatsAppMessage.direction == 'inbound')
        .filter(WhatsAppMessage.read_at.is_(None))
        .group_by(WhatsAppMessage.conversation_id)
        .all()
    )

    # Última mensagem de cada conversa: max(id) por conversa, portável.
    ultimos_ids = (
        db.session.query(func.max(WhatsAppMessage.id))
        .filter(WhatsAppMessage.conversation_id.in_(ids))
        .group_by(WhatsAppMessage.conversation_id)
    )
    ultimas = {
        m.conversation_id: m
        for m in WhatsAppMessage.query.filter(WhatsAppMessage.id.in_(ultimos_ids)).all()
    }

    return jsonify({
        'aba': aba,
        'contagens': contagens,
        'conversas': [serializar_conversa(c, nao_lidas.get(c.id, 0), ultimas.get(c.id))
                      for c in conversas],
    })


def _permissoes(conversa, eu, sou_admin, canal_ok):
    """
    O que a tela deve oferecer. Só orienta a interface: toda ação é
    revalidada no banco pela fila, e clique em estado velho recebe 409.
    """
    dono = conversa.assigned_agent_id
    ativa = conversa.status in CONVERSATION_ACTIVE_STATUSES
    return {
        'assumir': ativa and dono is None,
        'enviar': canal_ok and ativa and dono in (None, eu),
        'transferir': ativa and (dono == eu or (sou_admin and dono is not None)),
        'atribuir': ativa and sou_admin and dono is None,
        'liberar': ativa and dono is not None and (dono == eu or sou_admin),
        'devolver_bot': (ativa and conversa.driver_id is not None
                         and (dono in (None, eu) or sou_admin)),
        'resolver': ativa and (dono in (None, eu) or sou_admin),
        'reabrir': not ativa,
    }


@conversas_bp.route('/api/conversas/<int:conversa_id>')
@login_required
def detalhe(conversa_id):
    """Thread de uma conversa, com permissões e histórico."""
    if not _staff_only():
        return _forbidden()

    eu = current_user.id
    sou_admin = current_user.role == 'admin'

    conversa = Conversation.query.get_or_404(conversa_id)
    mensagens = (WhatsAppMessage.query
                 .filter_by(conversation_id=conversa.id)
                 .order_by(WhatsAppMessage.sent_at.asc(), WhatsAppMessage.id.asc())
                 .all())

    anexos_por_msg = {}
    if mensagens:
        for a in (MessageAttachment.query
                  .filter(MessageAttachment.message_id.in_([m.id for m in mensagens]))
                  .order_by(MessageAttachment.id).all()):
            anexos_por_msg.setdefault(a.message_id, []).append(anexos.serializar(a))

    # Leitura: só o responsável marca. Um colega espiando uma conversa da
    # fila não pode apagar o sinal de que tem gente esperando atendimento.
    # E marca só o que esta resposta exibe, por id.
    nao_lidas = [m.id for m in mensagens if m.direction == 'inbound' and m.read_at is None]
    if nao_lidas and conversa.assigned_agent_id == eu:
        try:
            marcar_lidas(conversa.id, nao_lidas)
            db.session.commit()
            nao_lidas = []
        except Exception as exc:
            db.session.rollback()
            log.warning(f"⚠️ Conversas: falha ao marcar como lidas: {exc}")

    canal_ok = providers.canal_configurado() is not None
    driver = conversa.driver
    return jsonify({
        'conversa': serializar_conversa(conversa, len(nao_lidas),
                                        mensagens[-1] if mensagens else None),
        'mensagens': [serializar_mensagem(m, anexos_por_msg.get(m.id)) for m in mensagens],
        'motorista': {
            'id': driver.id,
            'nome': driver.name,
            'telefone': driver.phone,
            'cidade': driver.city,
            'uf': driver.state,
            'veiculo': driver.truck_type,
            'placa': driver.vehicle_plate,
            'validado': bool(driver.validated),
        } if driver else None,
        'eventos': fila.eventos(conversa.id, limite=20),
        'eu_id': eu,
        'sou_admin': sou_admin,
        'canal_ativo': providers.canal_configurado(),
        'pode_enviar': canal_ok,
        'permissoes': _permissoes(conversa, eu, sou_admin, canal_ok),
    })


@conversas_bp.route('/api/atendentes')
@login_required
def atendentes():
    """Destinos possíveis de transferência."""
    if not _staff_only():
        return _forbidden()
    return jsonify({'atendentes': [
        {'id': u.id, 'nome': u.username, 'papel': u.role}
        for u in fila.atendentes_ativos()
    ]})


# ── API da tela: fila ────────────────────────────────────────────────────────

@conversas_bp.route('/api/conversas/<int:conversa_id>/assumir', methods=['POST'])
@login_required
def acao_assumir(conversa_id):
    if not _staff_only():
        return _forbidden()
    return _responder(fila.assumir(conversa_id, _ator()), 'assumiu')


@conversas_bp.route('/api/conversas/<int:conversa_id>/transferir', methods=['POST'])
@login_required
def acao_transferir(conversa_id):
    if not _staff_only():
        return _forbidden()
    corpo = _corpo()
    res = fila.transferir(conversa_id, _ator(), corpo.get('para'),
                          de_esperado=_int_ou_none(corpo.get('de')))
    return _responder(res, 'transferiu')


@conversas_bp.route('/api/conversas/<int:conversa_id>/liberar', methods=['POST'])
@login_required
def acao_liberar(conversa_id):
    if not _staff_only():
        return _forbidden()
    res = fila.liberar(conversa_id, _ator(), de_esperado=_int_ou_none(_corpo().get('de')))
    return _responder(res, 'liberou')


@conversas_bp.route('/api/conversas/<int:conversa_id>/devolver-bot', methods=['POST'])
@login_required
def acao_devolver_bot(conversa_id):
    if not _staff_only():
        return _forbidden()
    res = fila.devolver_ao_bot(conversa_id, _ator(),
                               de_esperado=_int_ou_none(_corpo().get('de')))
    return _responder(res, 'devolveu_bot')


@conversas_bp.route('/api/conversas/<int:conversa_id>/status', methods=['POST'])
@login_required
def mudar_status(conversa_id):
    """Resolve ou reabre uma conversa."""
    if not _staff_only():
        return _forbidden()

    corpo = _corpo()
    novo = str(corpo.get('status') or '').strip()
    ator = _ator()

    if novo == CONVERSATION_STATUS_RESOLVED:
        visto_ate = None
        if corpo.get('visto_ate'):
            try:
                visto_ate = datetime.fromisoformat(str(corpo['visto_ate']))
            except ValueError:
                return jsonify({'ok': False, 'error': 'visto_ate inválido.'}), 400
        return _responder(fila.resolver(conversa_id, ator, visto_ate=visto_ate), 'resolveu')

    if novo == CONVERSATION_STATUS_OPEN:
        return _responder(fila.reabrir(conversa_id, ator), 'reabriu')

    return jsonify({'ok': False, 'error': 'Status inválido.'}), 400


@conversas_bp.route('/api/conversas/<int:conversa_id>/enviar', methods=['POST'])
@login_required
def enviar(conversa_id):
    """
    Envia mensagem pelo canal ativo.

    Ordem obrigatória, e o motivo de cada passo:

    1. Assumir, com commit. Se a conversa estiver livre, quem envia vira
       dono; se outro chegou primeiro, 409 e NADA é enviado. Invertido, dois
       atendentes respondendo juntos mandariam as duas mensagens.
    2. Chamar o provedor SEM transação aberta. Nada de ler o banco entre o
       commit do passo 1 e o fim da chamada HTTP: por isso os dados vêm do
       retrato tirado dentro da operação, e o ator vem de _ator().
    3. Gravar a mensagem numa transação curta.
    """
    if not _staff_only():
        return _forbidden()

    ator = _ator()
    texto = str(_corpo().get('texto') or '').strip()
    if not texto or len(texto) > LIMITE_MENSAGEM:
        return jsonify({
            'ok': False,
            'error': f'A mensagem deve ter entre 1 e {LIMITE_MENSAGEM} caracteres.'
        }), 400

    if providers.canal_configurado() is None:
        return jsonify({
            'ok': False,
            'error': 'Nenhum canal de WhatsApp configurado. Configure a '
                     'Evolution ou a Twilio no ambiente.'
        }), 503

    # 1. Assumir.
    posse = fila.garantir_dono_para_envio(conversa_id, ator)
    if not posse.ok:
        return _responder(posse, 'enviar')
    if posse.codigo == 'ok':
        # Acabou de assumir: os outros atendentes precisam saber já.
        avisar_conversa(posse.conversa, extra={'acao': 'assumiu'})
        db.session.rollback()   # encerra a leitura feita pelo aviso

    telefone = posse.dados['contact_phone']
    driver_id = posse.dados['driver_id']

    # 2. Enviar, sem transação aberta.
    try:
        resultado = providers.enviar(telefone, texto)
        status_msg = resultado.get('status') or 'enviado'
        external_id = resultado.get('external_id')
        erro = None
    except providers.EnvioError as exc:
        status_msg, external_id, erro = 'erro', None, str(exc)
        log.error(f"❌ Conversas: envio falhou na conversa {conversa_id}: {exc}")

    # 3. Gravar.
    try:
        msg = registrar_saida(conversa_id, telefone, driver_id, texto,
                              autor_id=ator.id, external_id=external_id,
                              status=status_msg)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.error(f"❌ Conversas: falha ao gravar mensagem enviada: {exc}")
        return jsonify({'ok': False,
                        'error': 'Mensagem enviada, mas não foi possível registrá-la.'}), 500

    # O aviso da mensagem enviada sai sozinho no commit (utils/eventos_mensagem.py).

    if erro:
        return jsonify({'ok': False, 'error': erro, 'message_id': msg.id}), 502
    return jsonify({'ok': True, 'mensagem': serializar_mensagem(msg)})


@conversas_bp.route('/api/contadores')
@login_required
def contadores():
    """
    Números para o alerta global: o badge do menu e o título da aba.

    fila_humana conta conversas livres que precisam de gente, isto é, fora do
    EMA. Conversas que o bot está conduzindo não entram no alerta: seriam
    ruído constante para quem está em outra tela.
    """
    if not _staff_only():
        return _forbidden()
    eu = current_user.id

    fila_humana = Conversation.query.filter(
        _filtro_ativa(), Conversation.assigned_agent_id.is_(None),
        Conversation.handling_mode == fila.MODO_HUMANO).count()

    minhas_com_nao_lidas = (
        db.session.query(func.count(func.distinct(WhatsAppMessage.conversation_id)))
        .join(Conversation, Conversation.id == WhatsAppMessage.conversation_id)
        .filter(_filtro_ativa(), Conversation.assigned_agent_id == eu,
                WhatsAppMessage.direction == 'inbound', WhatsAppMessage.read_at.is_(None))
        .scalar() or 0
    )

    return jsonify({
        'fila_humana': fila_humana,
        'minhas_com_nao_lidas': minhas_com_nao_lidas,
        'alerta': fila_humana + minhas_com_nao_lidas,
    })


@conversas_bp.route('/api/anexos/<int:anexo_id>')
@login_required
def ver_anexo(anexo_id):
    """
    Entrega o arquivo de um anexo, lido do S3 pelo servidor.

    Passa pelo servidor em vez de redirecionar para uma URL assinada do S3
    por três motivos: o bucket continua privado, a permissão é conferida a
    cada acesso, e a política de segurança de conteúdo da app não precisa
    liberar o domínio do S3 para imagens.

    O tipo servido é o detectado pelos bytes na chegada, nunca o declarado
    pelo remetente, com nosniff e sandbox: um PDF não executa nada na origem
    da aplicação.
    """
    if not _staff_only():
        return _forbidden()

    anexo = db.session.get(MessageAttachment, anexo_id)
    if anexo is None or anexo.status != ANEXO_ARMAZENADO or not anexo.storage_key:
        return jsonify({'error': 'Anexo indisponível.'}), 404
    chave, tipo = anexo.storage_key, anexo.content_type
    # Encerra a leitura antes de ir à rede.
    db.session.rollback()

    try:
        dados = anexos.ler_do_s3(chave)
    except Exception as exc:
        log.error(f"❌ Anexos: falha ao ler anexo {anexo_id} do S3: {exc}")
        return jsonify({'error': 'Não foi possível ler o anexo.'}), 502

    extensao = anexos.EXTENSOES.get(tipo, '')
    return Response(dados, mimetype=tipo, headers={
        'Content-Disposition': f'inline; filename="anexo-{anexo_id}{extensao}"',
        'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': 'sandbox',
        'Cache-Control': 'private, max-age=300',
    })


@conversas_bp.route('/api/conversas/<int:conversa_id>/eventos')
@login_required
def listar_eventos(conversa_id):
    if not _staff_only():
        return _forbidden()
    return jsonify({'eventos': fila.eventos(conversa_id)})


# ── Diagnóstico ──────────────────────────────────────────────────────────────

@conversas_bp.route('/api/status')
@login_required
def diagnostico():
    """
    Confirma que o schema da Central existe DE FATO no banco em uso.

    O auto-migrator de utils/migrations.py engole falhas de DDL em log e segue
    o boot, e se a conexão com o PostgreSQL falhar a app cai para um SQLite
    local e continua de pé. Este endpoint responde às duas perguntas.
    """
    if not _staff_only():
        return _forbidden()

    try:
        inspector = sa_inspect(db.engine)
        tabelas = set(inspector.get_table_names())

        colunas_wa = set()
        if 'whatsapp_messages' in tabelas:
            colunas_wa = {c['name'] for c in inspector.get_columns('whatsapp_messages')}

        indices = set()
        for tabela in ('whatsapp_messages', 'conversations'):
            if tabela in tabelas:
                indices |= {i['name'] for i in inspector.get_indexes(tabela)}

        tem_tabela = 'conversations' in tabelas
        tem_coluna = 'conversation_id' in colunas_wa
        schema_ok = (tem_tabela and tem_coluna and 'read_at' in colunas_wa
                     and 'conversation_events' in tabelas
                     and 'message_attachments' in tabelas)

        return jsonify({
            'success': True,
            'dialeto': db.engine.dialect.name,
            'schema_ok': schema_ok,
            'tabela_conversations': tem_tabela,
            'tabela_conversation_events': 'conversation_events' in tabelas,
            'tabela_message_attachments': 'message_attachments' in tabelas,
            'coluna_conversation_id': tem_coluna,
            'coluna_read_at': 'read_at' in colunas_wa,
            's3_configurado': anexos.s3_configurado(),
            'indice_conversation_id': 'idx_whatsapp_messages_conversation_id' in indices,
            'indice_conversa_ativa': 'uq_conversations_open_contact' in indices,
            'total_conversas': Conversation.query.count() if tem_tabela else None,
            'mensagens_sem_conversa': (
                WhatsAppMessage.query.filter(WhatsAppMessage.conversation_id.is_(None)).count()
                if tem_coluna else None
            ),
            'canais': providers.diagnostico(),
            'webhook_entrada': url_for('conversas.webhook', _external=True),
            'webhook_status': url_for('conversas.webhook_status', _external=True),
        })
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500
