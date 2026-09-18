"""
Fase 5 — tela de operação do chatbot de regras.

Fatia mínima para TESTAR o chatbot de ponta a ponta antes da campanha (5.8):
- iniciar o bot para um número (manda a mensagem [1] e abre a sessão);
- acompanhar as sessões (estado, status, motorista);
- encerrar uma sessão para poder re-testar o mesmo número.

Só lógica de rota aqui; a regra de disparo vive em utils/campanha.py e a máquina
em utils/maquina.py.
"""
import logging

from flask import (Blueprint, render_template, request, flash, redirect, url_for)
from flask_login import login_required, current_user

from infraestrutura_critica.app import db
from infraestrutura_critica.models import (
    BotSession, BOT_SESSAO_ATIVA, BOT_SESSAO_ENCERRADA,
)
from chatbot_regras.utils import campanha

chatbot_bp = Blueprint('chatbot', __name__, url_prefix='/chatbot')
logger = logging.getLogger(__name__)

_MENSAGENS = {
    'numero_invalido': ('error', 'Número inválido — mande com DDD (ex.: 11 99999-8888).'),
    'optout':          ('error', 'Esse número pediu para não receber mensagens (opt-out).'),
    'ja_ativa':        ('error', 'Esse número já tem uma conversa de bot em andamento. Encerre-a para recomeçar.'),
    'enviado':         ('success', 'Chatbot iniciado! A mensagem de entrada foi enviada ao número.'),
    'erro_envio':      ('error', 'Sessão criada, mas o envio pela Evolution falhou — confira se a instância está no ar e o QR escaneado.'),
}


def _staff_only():
    return current_user.role in ('admin', 'operador')


@chatbot_bp.route('/', methods=['GET'])
@login_required
def index():
    if not _staff_only():
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))
    # id.desc() = mais recentes primeiro, portável nos dois bancos.
    sessoes = BotSession.query.order_by(BotSession.id.desc()).limit(40).all()
    return render_template('chatbot/index.html', sessoes=sessoes)


@chatbot_bp.route('/iniciar', methods=['POST'])
@login_required
def iniciar():
    if not _staff_only():
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))
    telefone = (request.form.get('telefone') or '').strip()
    if not telefone:
        flash('Informe um número.', 'error')
        return redirect(url_for('chatbot.index'))

    ok, motivo, _sid = campanha.iniciar_para_numero(
        telefone, criado_por=current_user.id)
    categoria, texto = _MENSAGENS.get(motivo, ('error', motivo))
    flash(texto, categoria)
    return redirect(url_for('chatbot.index'))


@chatbot_bp.route('/encerrar/<int:sid>', methods=['POST'])
@login_required
def encerrar(sid):
    if not _staff_only():
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))
    sessao = BotSession.query.get(sid)
    if sessao is None:
        flash('Sessão não encontrada.', 'error')
    elif sessao.status != BOT_SESSAO_ATIVA:
        flash('Essa sessão já não está ativa.', 'error')
    else:
        sessao.status = BOT_SESSAO_ENCERRADA
        sessao.motivo_fim = 'encerrada_no_painel'
        db.session.commit()
        flash('Sessão encerrada. Você já pode iniciar de novo para esse número.', 'success')
    return redirect(url_for('chatbot.index'))
