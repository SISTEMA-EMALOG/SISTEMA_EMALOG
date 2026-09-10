import os
import logging
from datetime import datetime, date
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user
from app import db
from models import Freight, Driver, Client, Quote, Payment, AssistantMessage, Lead, Opportunity, CRMActivity

assistant_bp = Blueprint('assistant', __name__, url_prefix='/assistant')

# --- Groq + Qwen 3.8 27B (Assistente Interno) ---
_groq_client = None

def get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        _groq_client = Groq(api_key=os.environ.get('GROQ_API_KEY', ''))
    return _groq_client


# --- Data snapshot: query DB and build a concise context string ---
def get_system_context(user_role: str = 'operador'):
    today = date.today()

    # ── Fretes ──────────────────────────────────────────────────────────────
    total_freights      = db.session.query(Freight).count()
    open_freights       = db.session.query(Freight).filter(
        Freight.status.in_(['ofertado', 'aceito', 'em_coleta', 'em_transito'])
    ).count()
    delivered_freights  = db.session.query(Freight).filter(Freight.status == 'entregue').count()
    cancelled_freights  = db.session.query(Freight).filter(Freight.status == 'cancelado').count()

    recent_freights = db.session.query(Freight).order_by(Freight.created_at.desc()).limit(5).all()
    recent_lines = []
    for f in recent_freights:
        driver_name = f.assigned_driver.name if f.assigned_driver else "sem motorista"
        client_name = f.client.company_name if f.client else "?"
        recent_lines.append(
            f"  • Frete {f.freight_number}: {f.origin_city or f.origin} → "
            f"{f.destination_city or f.destination} | "
            f"Status: {f.status} | Cliente: {client_name} | Motorista: {driver_name} | "
            f"Venda: R${f.agreed_price:.2f} | Custo motorista: R${(f.driver_cost or 0):.2f}"
        )

    # ── Motoristas ──────────────────────────────────────────────────────────
    total_drivers     = db.session.query(Driver).filter(Driver.active == True).count()
    available_drivers = db.session.query(Driver).filter(
        Driver.active == True, Driver.availability_status == 'disponivel'
    ).count()
    busy_drivers      = db.session.query(Driver).filter(
        Driver.active == True, Driver.availability_status == 'em_frete'
    ).count()

    # Tipos de caminhão disponíveis
    from sqlalchemy import func as _f
    truck_counts = db.session.query(
        Driver.truck_type, _f.count(Driver.id)
    ).filter(Driver.active == True).group_by(Driver.truck_type).all()
    truck_summary = ' | '.join([f"{t or 'N/A'}: {c}" for t, c in truck_counts]) or 'Nenhum'

    drivers_list = db.session.query(Driver).filter(
        Driver.active == True
    ).order_by(Driver.name).limit(15).all()
    driver_lines = []
    for d in drivers_list:
        cnh_alert = " ⚠️ CNH VENCENDO" if d.cnh_expiry and (d.cnh_expiry - today).days <= 30 else ""
        driver_lines.append(
            f"  • {d.name} | Tipo: {d.truck_type or 'N/A'} | Placa: {d.vehicle_plate or 'N/A'} | "
            f"Status: {d.availability_status} | {d.city}/{d.state} | "
            f"PIX: {d.pix_key or 'N/A'} | Banco: {d.bank_name or 'N/A'}{cnh_alert}"
        )

    # ── Clientes ────────────────────────────────────────────────────────────
    total_clients  = db.session.query(Client).filter(Client.active == True).count()
    total_inactive = db.session.query(Client).filter(Client.active == False).count()

    recent_clients = db.session.query(Client).filter(
        Client.active == True
    ).order_by(Client.created_at.desc()).limit(5).all()
    client_lines = []
    for c in recent_clients:
        client_lines.append(
            f"  • {c.company_name} | CNPJ: {c.cnpj or 'N/A'} | "
            f"Cidade: {c.city or 'N/A'}/{c.state or 'N/A'} | "
            f"Tel: {c.phone or 'N/A'} | Email: {c.email or 'N/A'}"
        )

    # ── Cotações ────────────────────────────────────────────────────────────
    pending_quotes  = db.session.query(Quote).filter(Quote.status == 'pendente').count()
    approved_quotes = db.session.query(Quote).filter(Quote.status == 'aprovada').count()
    rejected_quotes = db.session.query(Quote).filter(Quote.status == 'rejeitada').count()

    recent_quotes = db.session.query(Quote).order_by(Quote.created_at.desc()).limit(5).all()
    quote_lines = []
    for q in recent_quotes:
        client_name = q.client.company_name if q.client else "?"
        quote_lines.append(
            f"  • Cotação {q.quote_number}: {q.origin_city} → {q.destination_city} | "
            f"Status: {q.status} | Cliente: {client_name} | Veículo: {q.vehicle_type or 'N/A'} | "
            f"Venda: R${q.sale_value:.2f} | Custo motorista: R${q.driver_cost:.2f} | "
            f"Margem: R${(q.sale_value - q.driver_cost):.2f}"
        )

    # ── Financeiro ──────────────────────────────────────────────────────────
    pending_payments = db.session.query(Payment).filter(Payment.status == 'pendente').count()
    paid_payments    = db.session.query(Payment).filter(Payment.status == 'pago').count()
    pending_value    = db.session.query(_f.sum(Payment.amount)).filter(
        Payment.status == 'pendente'
    ).scalar() or 0.0
    paid_value       = db.session.query(_f.sum(Payment.amount)).filter(
        Payment.status == 'pago'
    ).scalar() or 0.0

    pending_pay_list = db.session.query(Payment).filter(
        Payment.status == 'pendente'
    ).order_by(Payment.payment_date).limit(5).all()
    payment_lines = []
    for p in pending_pay_list:
        driver_name = p.driver.name if p.driver else "?"
        freight_num = p.freight.freight_number if p.freight else "?"
        overdue = " ⚠️ VENCIDO" if p.payment_date and p.payment_date < today else ""
        payment_lines.append(
            f"  • R${p.amount:.2f} | {driver_name} | Frete: {freight_num} | "
            f"Tipo: {p.payment_type} | Vencimento: {p.payment_date}{overdue}"
        )

    # ── CRM ─────────────────────────────────────────────────────────────────
    leads_total    = db.session.query(Lead).count()
    leads_by_status = db.session.query(
        Lead.status, _f.count(Lead.id)
    ).group_by(Lead.status).all()
    leads_status_str = ' | '.join([f"{s or 'sem status'}: {c}" for s, c in leads_by_status])

    opps_total  = db.session.query(Opportunity).count()
    opps_open   = db.session.query(Opportunity).filter(
        Opportunity.stage.notin_(['fechado_ganho', 'fechado_perdido'])
    ).count()
    opps_won    = db.session.query(Opportunity).filter(
        Opportunity.stage == 'fechado_ganho'
    ).count()

    recent_activities = db.session.query(CRMActivity).order_by(
        CRMActivity.created_at.desc()
    ).limit(5).all()
    activity_lines = []
    for a in recent_activities:
        activity_lines.append(
            f"  • {a.activity_type or 'Atividade'}: {(a.title or a.notes or '')[:80]} "
            f"({a.created_at.strftime('%d/%m/%Y') if a.created_at else '?'})"
        )

    # ── Agente EMA ──────────────────────────────────────────────────────────
    try:
        from models import EmaSession
        ema_total    = db.session.query(EmaSession).count()
        ema_active   = db.session.query(EmaSession).filter(EmaSession.status == 'active').count()
        ema_done     = db.session.query(EmaSession).filter(EmaSession.status == 'completed').count()
        ema_paused   = db.session.query(EmaSession).filter(EmaSession.status == 'paused').count()
        ema_avg_pct  = db.session.query(_f.avg(EmaSession.completeness_pct)).scalar() or 0
        ema_str = (f"Total de sessões: {ema_total} | Ativas: {ema_active} | "
                   f"Concluídas: {ema_done} | Pausadas: {ema_paused} | "
                   f"Completude média: {ema_avg_pct:.0f}%")
    except Exception:
        ema_str = "Agente EMA ainda não possui sessões registradas."

    # ── Licitações / Consulta de preço ──────────────────────────────────────
    try:
        from models import DriverBid
        bids_total    = db.session.query(DriverBid).count()
        bids_pending  = db.session.query(DriverBid).filter(DriverBid.status == 'pending').count()
        bids_responded = db.session.query(DriverBid).filter(DriverBid.status == 'responded').count()
        bids_str = (f"Total: {bids_total} | Aguardando resposta: {bids_pending} | "
                    f"Respondidas: {bids_responded}")
    except Exception:
        bids_str = "Módulo de licitações sem dados ainda."

    # ── CNHs vencendo em 60 dias ─────────────────────────────────────────────
    from datetime import timedelta
    cutoff = today + timedelta(days=60)
    expiring_cnh = db.session.query(Driver).filter(
        Driver.active == True,
        Driver.cnh_expiry <= cutoff,
        Driver.cnh_expiry >= today
    ).order_by(Driver.cnh_expiry).all()
    cnh_lines = [
        f"  • {d.name} | CNH vence: {d.cnh_expiry.strftime('%d/%m/%Y')} "
        f"({(d.cnh_expiry - today).days} dias)"
        for d in expiring_cnh
    ]

    _role_intro = {
        'vendedor': "USUÁRIO: Vendedor. Foco: prospecção, scripts de abordagem, qualificação de leads, CRM, estratégia comercial.",
        'operador': "USUÁRIO: Operador logístico. Foco: gestão de fretes, motoristas, cotações e operação do dia a dia.",
        'admin': "USUÁRIO: Administrador. Acesso completo a todos os dados e funcionalidades.",
    }

    _spot_knowledge_compact = """
FRETE SPOT BR — REFERÊNCIA RÁPIDA:
• Spot = contratação pontual sem contrato fixo; margem normal 15–35%
• Veículos: Fiorino≤500kg | VUC/3/4≤3t | Toco≤6t | Truck≤14t | Carreta≤27t
• Picos: Jan–Jun (safra soja/milho), Out–Dez (Black Friday/Natal)
• Precificação: base tabela ANTT + diesel + pedágios + diária; urgência +10–20%
• Backhaul (frete retorno): desconto 20–30%, argumento de preço diferenciado
• Prospecção: descubra a DOR antes de falar preço; cold call, WhatsApp texto, LinkedIn
• Objeção "já tenho transportadora": posicione EMALOG como plano B
• Qualificação BANT: Budget, Authority, Need, Timeline
• Diferenciais EMALOG: atendimento direto, motoristas documentados, CT-e, portal cliente, rastreamento
• Para scripts detalhados, estratégias e sazonalidade completa: peça ao usuário que seja mais específico
"""

    return f"""Você é EMA, assistente de operações e vendas da EMALOG (transportadora spot brasileira).
{_role_intro.get(user_role, _role_intro['operador'])}
{_spot_knowledge_compact}
REGRAS: Responda em pt-BR, seja direto e preciso com dados internos, mencione quando usar internet.
Data: {datetime.now().strftime('%d/%m/%Y %H:%M')}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SNAPSHOT DO SISTEMA — {datetime.now().strftime('%d/%m/%Y %H:%M')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📦 FRETES
  Total: {total_freights} | Em aberto: {open_freights} | Entregues: {delivered_freights} | Cancelados: {cancelled_freights}

Últimos 10 fretes:
{chr(10).join(recent_lines) if recent_lines else '  Nenhum frete cadastrado ainda.'}

🚛 MOTORISTAS
  Ativos: {total_drivers} | Disponíveis: {available_drivers} | Em frete: {busy_drivers}
  Frota por tipo: {truck_summary}

Lista de motoristas (até 30):
{chr(10).join(driver_lines) if driver_lines else '  Nenhum motorista cadastrado ainda.'}

{'⚠️ CNHs VENCENDO NOS PRÓXIMOS 60 DIAS:' + chr(10) + chr(10).join(cnh_lines) if cnh_lines else '✅ Nenhuma CNH vencendo nos próximos 60 dias.'}

🏢 CLIENTES
  Ativos: {total_clients} | Inativos: {total_inactive}

Clientes recentes (10):
{chr(10).join(client_lines) if client_lines else '  Nenhum cliente cadastrado ainda.'}

📋 COTAÇÕES
  Pendentes (aguardando aprovação): {pending_quotes} | Aprovadas: {approved_quotes} | Rejeitadas: {rejected_quotes}

Últimas 8 cotações:
{chr(10).join(quote_lines) if quote_lines else '  Nenhuma cotação cadastrada ainda.'}

💰 FINANCEIRO
  Pagamentos pendentes: {pending_payments} | Total a pagar: R${pending_value:.2f}
  Pagamentos realizados: {paid_payments} | Total pago: R${paid_value:.2f}

Próximos 10 pagamentos pendentes:
{chr(10).join(payment_lines) if payment_lines else '  Nenhum pagamento pendente.'}

📊 CRM (Gestão de Leads)
  Total de leads: {leads_total}
  Por status: {leads_status_str}
  Oportunidades: {opps_total} total | {opps_open} em andamento | {opps_won} ganhas

Atividades recentes no CRM:
{chr(10).join(activity_lines) if activity_lines else '  Nenhuma atividade recente.'}

🤖 AGENTE EMA (Enriquecimento de dados de motoristas via WhatsApp)
  {ema_str}
  O Agente EMA coleta automaticamente via WhatsApp: CPF, RG, endereço, dados do veículo,
  dados bancários, e fotos dos documentos (CNH, CRLV, comprovante de residência).
  Campos coletados: 24 no total. Acesse em: Menu → Agente EMA

💬 CONSULTA DE PREÇO (Licitações com motoristas)
  {bids_str}
  Quando um frete é criado, o sistema pode enviar consultas de preço via WhatsApp
  para múltiplos motoristas e registrar as respostas automaticamente.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MÓDULOS DO SISTEMA EMALOG
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• COTAÇÕES: Criar cotação com origem/destino via CEP, tipo de veículo, dimensões da carga,
  valor da NF, data de coleta. Sistema calcula margem automaticamente. Ao aprovar, gera frete.

• FRETES: Ciclo completo — ofertado → aceito → em coleta → em trânsito → entregue.
  Permite selecionar motorista, enviar mensagem WhatsApp, anexar CT-e e NF assinada.

• MOTORISTAS: Cadastro completo com validação de CPF, CEP automático, tipos de caminhão
  brasileiros (3/4, VLC, toco, truck, bitruck, carreta), dados bancários e documentos.

• CLIENTES: Cadastro com validação de CNPJ via Receita Federal, múltiplos contatos,
  histórico de fretes, portal do cliente com acesso restrito.

• FINANCEIRO: Pagamentos automáticos — 70% no carregamento, 30% na entrega.
  Controle de status (pendente/pago/cancelado), relatórios por motorista.

• CRM: Gestão de leads com pipeline de vendas, importação em massa via Excel (5.113 leads),
  atividades, oportunidades, funil de conversão.

• AGENTE EMA: Bot de WhatsApp que coleta dados cadastrais dos motoristas automaticamente
  via conversa natural. Usa Llama AI para entender as respostas.

• RELATÓRIOS: Dashboard com gráficos de fretes por período, receita, motoristas mais ativos,
  exportação para Excel/PDF.

• CHAT: Comunicação interna em tempo real entre equipe e clientes via Socket.IO.

• BACKUP: Backup automático diário às 02h UTC com exportação JSON de todas as tabelas.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOBRE BUSCA NA INTERNET:
Você tem Google Search disponível. Use-o quando:
• O usuário pedir informações de mercado, tabelas de preço, notícias do setor
• Precisar de dados sobre uma empresa específica (CNPJ, setor, porte, localização)
• O usuário pedir tendências, benchmarks ou comparativos do mercado de logística brasileiro
• Precisar de informações sobre regulamentações, tabela ANTT, legislação de transporte
• O vendedor pedir ajuda para pesquisar um lead antes de uma ligação
Quando usar a busca, mencione brevemente que a informação veio da internet.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Responda com base nos dados internos e/ou pesquisa na internet conforme necessário.
Data/hora atual: {datetime.now().strftime('%d/%m/%Y %H:%M')}
"""


HISTORY_TTL_HOURS = 8


def cleanup_old_messages():
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=HISTORY_TTL_HOURS)
    deleted = (db.session.query(AssistantMessage)
               .filter(AssistantMessage.created_at < cutoff)
               .delete())
    if deleted:
        db.session.commit()
        logging.info(f"Assistente: {deleted} mensagens antigas removidas (>{HISTORY_TTL_HOURS}h)")


def load_user_history(user_id, limit=40):
    msgs = (db.session.query(AssistantMessage)
            .filter_by(user_id=user_id)
            .order_by(AssistantMessage.created_at.asc())
            .limit(limit)
            .all())
    return [{'role': m.role, 'text': m.text} for m in msgs]


def save_message(user_id, role, text):
    msg = AssistantMessage(user_id=user_id, role=role, text=text)
    db.session.add(msg)
    db.session.commit()


@assistant_bp.route('/')
@login_required
def index():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        from flask import abort
        abort(403)
    cleanup_old_messages()
    history = load_user_history(current_user.id)
    return render_template('assistant/index.html', history=history)


@assistant_bp.route('/chat', methods=['POST'])
@login_required
def chat():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'}), 403

    data = request.get_json()
    if not data or not data.get('message'):
        return jsonify({'error': 'Mensagem vazia'}), 400

    user_message = data['message'].strip()

    try:
        system_ctx = get_system_context(user_role=current_user.role)
        client = get_groq_client()

        db_history = load_user_history(current_user.id, limit=8)
        messages = [{'role': 'system', 'content': system_ctx}]
        for msg in db_history:
            role = 'user' if msg['role'] == 'user' else 'assistant'
            messages.append({'role': role, 'content': msg['text']})
        messages.append({'role': 'user', 'content': user_message})

        response = client.chat.completions.create(
            model='qwen/qwen3.8-27b',
            messages=messages,
            max_tokens=1500,
            temperature=0.4,
        )

        answer = response.choices[0].message.content or 'Não foi possível gerar uma resposta.'

        save_message(current_user.id, 'user', user_message)
        save_message(current_user.id, 'model', answer)

        return jsonify({'answer': answer, 'used_web': False})

    except Exception as e:
        error_msg = str(e)
        logging.error(f"Assistente IA erro: {error_msg}")
        return jsonify({'error': f'Erro ao consultar IA: {error_msg}'}), 500


@assistant_bp.route('/history')
@login_required
def history():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'}), 403
    cleanup_old_messages()
    msgs = load_user_history(current_user.id, limit=40)
    return jsonify({'history': msgs})


@assistant_bp.route('/clear', methods=['POST'])
@login_required
def clear_history():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'}), 403
    db.session.query(AssistantMessage).filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return jsonify({'ok': True})
