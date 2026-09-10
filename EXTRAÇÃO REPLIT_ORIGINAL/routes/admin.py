from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from models import User, Client, Driver, Quote, Freight, Payment, AuditLog
from app import db
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from functools import wraps
from sqlalchemy import func, case as sa_case
from sqlalchemy.orm import joinedload
import json

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            flash('Acesso negado. Apenas administradores podem acessar esta área.', 'error')
            return redirect(url_for('dashboard.index'))
        return f(*args, **kwargs)
    return decorated_function

@admin_bp.route('/')
@login_required
@admin_required
def index():
    """Dashboard principal do administrador"""
    now = datetime.utcnow()
    seven_days_ago  = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)

    # ── KPIs: 4 queries CASE WHEN ao invés de 17 queries separadas ───────────
    u_counts = db.session.query(
        func.count(User.id).label('total'),
        func.sum(sa_case((User.active == True, 1), else_=0)).label('active'),
        func.sum(sa_case(((User.must_change_password == True) & (User.active == True), 1), else_=0)).label('must_change'),
    ).one()
    cl_counts = db.session.query(
        func.count(Client.id).label('total'),
        func.sum(sa_case((Client.active == True, 1), else_=0)).label('active'),
        func.sum(sa_case((Client.created_at >= thirty_days_ago, 1), else_=0)).label('new_30d'),
    ).one()
    dr_counts = db.session.query(
        func.count(Driver.id).label('total'),
        func.sum(sa_case((Driver.availability_status == 'disponivel', 1), else_=0)).label('available'),
        func.sum(sa_case((Driver.availability_status == 'indisponivel', 1), else_=0)).label('unavailable'),
        func.sum(sa_case((Driver.created_at >= thirty_days_ago, 1), else_=0)).label('new_30d'),
    ).one()
    q_counts = db.session.query(
        func.count(Quote.id).label('total'),
        func.sum(sa_case((Quote.status == 'pendente', 1), else_=0)).label('pending'),
        func.sum(sa_case((Quote.status.in_(['cotada', 'negociacao']), 1), else_=0)).label('awaiting'),
        func.sum(sa_case((Quote.status == 'aprovada', 1), else_=0)).label('approved'),
    ).one()
    fr_counts = db.session.query(
        func.count(Freight.id).label('total'),
        func.sum(sa_case((Freight.status.in_(['ofertado', 'aceito', 'em_coleta', 'em_transito']), 1), else_=0)).label('active'),
        func.sum(sa_case((Freight.status == 'entregue', 1), else_=0)).label('delivered'),
        func.sum(sa_case((Freight.created_at >= thirty_days_ago, 1), else_=0)).label('new_30d'),
    ).one()
    pending_payments = Payment.query.filter_by(status='pendente').count()

    stats = {
        'total_users':            int(u_counts.total or 0),
        'active_users':           int(u_counts.active or 0),
        'total_clients':          int(cl_counts.total or 0),
        'active_clients':         int(cl_counts.active or 0),
        'total_drivers':          int(dr_counts.total or 0),
        'available_drivers':      int(dr_counts.available or 0),
        'total_quotes':           int(q_counts.total or 0),
        'pending_quotes':         int(q_counts.pending or 0),
        'awaiting_client_quotes': int(q_counts.awaiting or 0),
        'approved_quotes':        int(q_counts.approved or 0),
        'total_freights':         int(fr_counts.total or 0),
        'active_freights':        int(fr_counts.active or 0),
        'delivered_freights':     int(fr_counts.delivered or 0),
        'pending_payments':       pending_payments,
        'new_clients_30d':        int(cl_counts.new_30d or 0),
        'new_drivers_30d':        int(dr_counts.new_30d or 0),
        'new_freights_30d':       int(fr_counts.new_30d or 0),
    }

    # ── Alertas ───────────────────────────────────────────────────────────────
    alerts = []
    if stats['pending_quotes'] > 0:
        alerts.append({
            'level': 'warning',
            'icon': 'fa-file-invoice',
            'message': f"{stats['pending_quotes']} cotação(ões) aguardando precificação",
            'link': url_for('quotes.index') + '?status=pendente'
        })
    if stats['awaiting_client_quotes'] > 0:
        alerts.append({
            'level': 'info',
            'icon': 'fa-clock',
            'message': f"{stats['awaiting_client_quotes']} proposta(s) enviada(s) aguardando aprovação do cliente",
            'link': url_for('quotes.index') + '?status=cotada'
        })
    if stats['pending_payments'] > 0:
        alerts.append({
            'level': 'warning',
            'icon': 'fa-dollar-sign',
            'message': f"{stats['pending_payments']} pagamento(s) pendente(s) para motoristas",
            'link': url_for('financial.index')
        })
    if int(dr_counts.unavailable or 0) > 0:
        alerts.append({
            'level': 'info',
            'icon': 'fa-truck',
            'message': f"{int(dr_counts.unavailable)} motorista(s) indisponível(is)",
            'link': url_for('drivers.index')
        })
    if int(u_counts.must_change or 0) > 0:
        alerts.append({
            'level': 'info',
            'icon': 'fa-key',
            'message': f"{int(u_counts.must_change)} usuário(s) ainda não alteraram a senha padrão",
            'link': url_for('admin.users')
        })

    # ── Atividades recentes ────────────────────────────────────────────────────
    recent_activities = AuditLog.query.filter(
        AuditLog.created_at >= seven_days_ago
    ).order_by(AuditLog.created_at.desc()).limit(15).all()

    # ── Últimos logins ────────────────────────────────────────────────────────
    recent_logins = User.query.filter(
        User.last_login.isnot(None)
    ).order_by(User.last_login.desc()).limit(6).all()

    # ── Fretes ativos ─────────────────────────────────────────────────────────
    active_freight_list = Freight.query.filter(
        Freight.status.in_(['aceito', 'em_coleta', 'em_transito'])
    ).order_by(Freight.created_at.desc()).limit(5).all()

    return render_template('admin/dashboard.html',
                           stats=stats,
                           alerts=alerts,
                           recent_activities=recent_activities,
                           recent_logins=recent_logins,
                           active_freight_list=active_freight_list,
                           now=now)

@admin_bp.route('/users')
@login_required
@admin_required
def users():
    """Gerenciamento de usuários com paginação"""
    search  = request.args.get('search', '')
    role    = request.args.get('role', 'all')
    status  = request.args.get('status', 'all')
    page    = request.args.get('page', 1, type=int)
    per_page = 20

    query = User.query

    if search:
        query = query.filter(
            (User.username.contains(search)) |
            (User.email.contains(search))
        )
    if role != 'all':
        query = query.filter_by(role=role)
    if status == 'active':
        query = query.filter_by(active=True)
    elif status == 'inactive':
        query = query.filter_by(active=False)

    pagination = query.order_by(User.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    clients = Client.query.filter_by(active=True).order_by(Client.company_name).all()

    # Contadores rápidos para o cabeçalho
    role_counts = {
        'admin':    User.query.filter_by(role='admin').count(),
        'operador': User.query.filter_by(role='operador').count(),
        'vendedor': User.query.filter_by(role='vendedor').count(),
        'cliente':  User.query.filter_by(role='cliente').count(),
        'active':   User.query.filter_by(active=True).count(),
        'inactive': User.query.filter_by(active=False).count(),
    }

    return render_template('admin/users.html',
                           users=pagination.items,
                           pagination=pagination,
                           clients=clients,
                           search=search,
                           role=role,
                           status=status,
                           role_counts=role_counts,
                           now=datetime.utcnow())

@admin_bp.route('/users/new', methods=['GET', 'POST'])
@login_required
@admin_required
def new_user():
    """Criar novo usuário"""
    if request.method == 'POST':
        try:
            # Validar campos obrigatórios (senha não é obrigatória para novos usuários)
            required_fields = ['username', 'email', 'role']
            for field in required_fields:
                if not request.form.get(field):
                    flash(f'Campo obrigatório: {field}', 'error')
                    return redirect(url_for('admin.new_user'))
            
            # Verificar se usuário já existe
            existing_user = User.query.filter(
                (User.username == request.form['username']) |
                (User.email == request.form['email'])
            ).first()
            
            if existing_user:
                flash('Nome de usuário ou email já existe.', 'error')
                return redirect(url_for('admin.new_user'))
            
            # Criar usuário
            user = User()
            user.username = request.form['username']
            user.email = request.form['email']
            
            # Gerar senha inicial aleatória e segura
            import secrets as _sec
            default_password = _sec.token_urlsafe(10)
            user.password_hash = generate_password_hash(default_password)
            user.role = request.form['role']
            user.active = bool(request.form.get('active'))
            user.must_change_password = True  # Obrigar troca no primeiro login
            
            # Se é cliente, associar ao cliente
            if user.role == 'cliente' and request.form.get('client_id'):
                user.client_id = int(request.form['client_id'])
            
            db.session.add(user)
            db.session.commit()
            
            # Log da criação
            audit = AuditLog()
            audit.user_id = current_user.id
            audit.action = 'CREATE'
            audit.table_name = 'users'
            audit.record_id = user.id
            audit.new_values = f"Usuário criado: {user.username} ({user.role}) - senha inicial gerada (não registrada)"
            db.session.add(audit)
            db.session.commit()
            
            flash(f'Usuário criado com senha inicial: {default_password} (deve ser alterada no primeiro login)', 'info')
            return redirect(url_for('admin.users'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao criar usuário: {str(e)}', 'error')
    
    clients = Client.query.filter_by(active=True).order_by(Client.company_name).all()
    return render_template('admin/user_form.html', clients=clients)

@admin_bp.route('/users/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user(id):
    """Editar usuário existente"""
    user = User.query.get_or_404(id)
    
    if request.method == 'POST':
        try:
            old_values = f"Username: {user.username}, Email: {user.email}, Role: {user.role}"
            
            user.username = request.form['username']
            user.email = request.form['email']
            user.role = request.form['role']
            user.active = bool(request.form.get('active'))
            
            # Atualizar senha se fornecida
            if request.form.get('password'):
                user.password_hash = generate_password_hash(request.form['password'])
            
            # Associar cliente se necessário
            if user.role == 'cliente' and request.form.get('client_id'):
                user.client_id = int(request.form['client_id'])
            else:
                user.client_id = None
            
            db.session.commit()
            
            # Log da alteração
            new_values = f"Username: {user.username}, Email: {user.email}, Role: {user.role}"
            audit = AuditLog()
            audit.user_id = current_user.id
            audit.action = 'UPDATE'
            audit.table_name = 'users'
            audit.record_id = user.id
            audit.old_values = old_values
            audit.new_values = new_values
            db.session.add(audit)
            db.session.commit()
            
            flash('Usuário atualizado com sucesso!', 'success')
            return redirect(url_for('admin.users'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao atualizar usuário: {str(e)}', 'error')
    
    clients = Client.query.filter_by(active=True).order_by(Client.company_name).all()
    return render_template('admin/user_form.html', user=user, clients=clients)

@admin_bp.route('/users/<int:id>/toggle-status', methods=['POST'])
@login_required
@admin_required
def toggle_user_status(id):
    """Ativar/desativar usuário"""
    user = User.query.get_or_404(id)
    
    if user.id == current_user.id:
        flash('Você não pode desativar sua própria conta.', 'error')
        return redirect(url_for('admin.users'))
    
    old_status = "Ativo" if user.active else "Inativo"
    user.active = not user.active
    new_status = "Ativo" if user.active else "Inativo"

    try:
        # Log da alteração
        audit = AuditLog()
        audit.user_id = current_user.id
        audit.action = 'UPDATE'
        audit.table_name = 'users'
        audit.record_id = user.id
        audit.old_values = f"Status: {old_status}"
        audit.new_values = f"Status: {new_status}"
        db.session.add(audit)
        db.session.commit()
    except Exception as e:
        # Se o audit log falhar (ex: sequência desatualizada), salva apenas a alteração principal
        import logging as _log
        _log.warning(f"Audit log falhou (ignorado): {e}")
        db.session.rollback()
        db.session.add(user)
        db.session.commit()

    status_text = "ativado" if user.active else "desativado"
    flash(f'Usuário {status_text} com sucesso!', 'success')
    return redirect(url_for('admin.users'))

@admin_bp.route('/users/<int:id>/reset-password', methods=['POST'])
@login_required
@admin_required
def reset_password(id):
    """Resetar senha do usuário para padrão"""
    user = User.query.get_or_404(id)
    
    try:
        # Gerar nova senha aleatória e segura
        import secrets
        new_password = secrets.token_urlsafe(10)
        user.password_hash = generate_password_hash(new_password)
        user.must_change_password = True  # Obrigar troca no próximo login
        
        db.session.commit()
        
        # Log da alteração
        audit = AuditLog()
        audit.user_id = current_user.id
        audit.action = 'UPDATE'
        audit.table_name = 'users'
        audit.record_id = user.id
        audit.old_values = "Senha resetada pelo administrador"
        audit.new_values = "Senha resetada pelo administrador (nova senha não registrada por segurança)"
        db.session.add(audit)
        db.session.commit()
        
        flash(f'Senha resetada com sucesso! Nova senha: {new_password} (usuário deve alterar no próximo login)', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao resetar senha: {str(e)}', 'error')
    
    return redirect(url_for('admin.users'))

@admin_bp.route('/audit-logs')
@login_required
@admin_required
def audit_logs():
    """Visualizar logs de auditoria"""
    page = request.args.get('page', 1, type=int)
    user_id = request.args.get('user_id', '')
    action = request.args.get('action', '')
    table_name = request.args.get('table_name', '')
    
    query = AuditLog.query
    
    if user_id:
        query = query.filter_by(user_id=user_id)
    if action:
        query = query.filter_by(action=action)
    if table_name:
        query = query.filter_by(table_name=table_name)
    
    logs = query.order_by(AuditLog.created_at.desc()).paginate(
        page=page, per_page=50, error_out=False
    )
    
    users = User.query.order_by(User.username).all()
    
    return render_template('admin/audit_logs.html', 
                         logs=logs, 
                         users=users,
                         selected_user=user_id,
                         selected_action=action,
                         selected_table=table_name)

@admin_bp.route('/system-info')
@login_required
@admin_required
def system_info():
    """Informações do sistema"""
    import os
    import platform
    
    system_info = {
        'platform': platform.platform(),
        'python_version': platform.python_version(),
        'database_url': os.environ.get('DATABASE_URL', 'Not set'),
        'environment': 'Production' if not os.environ.get('DEBUG') else 'Development',
    }
    
    # Estatísticas detalhadas — 3 queries CASE WHEN ao invés de 9 separadas
    _now = datetime.utcnow()
    _7d  = _now - timedelta(days=7)
    _30d = _now - timedelta(days=30)

    ur = db.session.query(
        func.sum(sa_case((User.role == 'admin',    1), else_=0)).label('admin'),
        func.sum(sa_case((User.role == 'operador', 1), else_=0)).label('operador'),
        func.sum(sa_case((User.role == 'vendedor', 1), else_=0)).label('vendedor'),
        func.sum(sa_case((User.role == 'cliente',  1), else_=0)).label('cliente'),
    ).one()
    qs = db.session.query(
        func.sum(sa_case((Quote.status == 'pendente',  1), else_=0)).label('pendente'),
        func.sum(sa_case((Quote.status == 'aprovado',  1), else_=0)).label('aprovado'),
        func.sum(sa_case((Quote.status == 'rejeitado', 1), else_=0)).label('rejeitado'),
    ).one()
    al = db.session.query(
        func.sum(sa_case((AuditLog.created_at >= _7d,  1), else_=0)).label('last_7'),
        func.sum(sa_case((AuditLog.created_at >= _30d, 1), else_=0)).label('last_30'),
    ).one()

    detailed_stats = {
        'users_by_role': {
            'admin':    int(ur.admin    or 0),
            'operador': int(ur.operador or 0),
            'vendedor': int(ur.vendedor or 0),
            'cliente':  int(ur.cliente  or 0),
        },
        'quotes_by_status': {
            'pendente':  int(qs.pendente  or 0),
            'aprovado':  int(qs.aprovado  or 0),
            'rejeitado': int(qs.rejeitado or 0),
        },
        'recent_activity': {
            'last_7_days':  int(al.last_7  or 0),
            'last_30_days': int(al.last_30 or 0),
        }
    }
    
    return render_template('admin/system_info.html', 
                         system_info=system_info,
                         detailed_stats=detailed_stats)

@admin_bp.route('/api/activity-chart')
@login_required
@admin_required
def activity_chart_data():
    """Dados para gráfico de atividades"""
    days = request.args.get('days', 30, type=int)
    start_date = datetime.utcnow() - timedelta(days=days)
    
    # Atividades por dia
    activities = db.session.query(
        db.func.date(AuditLog.created_at),
        db.func.count(AuditLog.id)
    ).filter(
        AuditLog.created_at >= start_date
    ).group_by(
        db.func.date(AuditLog.created_at)
    ).all()
    
    # Formatar dados para o gráfico
    labels = []
    data = []
    
    for activity in activities:
        labels.append(activity[0].strftime('%d/%m'))
        data.append(activity[1])
    
    return jsonify({
        'labels': labels,
        'data': data
    })


# ══════════════════════════════════════════════
#  BACKUP — Gerenciamento de backups do banco
# ══════════════════════════════════════════════

@admin_bp.route('/backup')
@login_required
@admin_required
def backup_index():
    """Página de gerenciamento de backups"""
    from utils.backup import list_backups, get_backup_stats
    stats  = get_backup_stats()
    backups = list_backups()
    return render_template('admin/backup.html', stats=stats, backups=backups)


@admin_bp.route('/backup/create', methods=['POST'])
@login_required
@admin_required
def backup_create():
    """Cria um novo backup manualmente"""
    from utils.backup import create_backup
    result = create_backup()
    if result.get('ok'):
        flash(f'✅ Backup criado com sucesso: {result["filename"]} '
              f'({result["tables"]} tabelas, {result["rows"]} registros)', 'success')
    else:
        flash(f'❌ Erro ao criar backup: {result.get("error", "Desconhecido")}', 'error')
    return redirect(url_for('admin.backup_index'))


@admin_bp.route('/backup/download/<filename>')
@login_required
@admin_required
def backup_download(filename):
    """Download de um arquivo de backup"""
    import os
    from flask import send_file, abort
    from utils.backup import BACKUP_DIR

    # Segurança: apenas arquivos dentro de BACKUP_DIR com nome esperado
    if not filename.startswith('emalog_backup_') or '..' in filename:
        abort(400)

    from utils.backup import download_backup
    from io import BytesIO
    data = download_backup(filename)
    if data is None:
        abort(404)

    mimetype = 'application/json'
    return send_file(BytesIO(data), as_attachment=True, mimetype=mimetype,
                     download_name=filename)


@admin_bp.route('/backup/delete/<filename>', methods=['POST'])
@login_required
@admin_required
def backup_delete(filename):
    """Remove um backup específico"""
    import os
    from utils.backup import BACKUP_DIR

    if not filename.startswith('emalog_backup_') or '..' in filename:
        flash('Nome de arquivo inválido.', 'error')
        return redirect(url_for('admin.backup_index'))

    deleted = 0
    for ext in ['.json', '.db']:
        target = os.path.join(BACKUP_DIR, filename.replace('.json', ext).replace('.db', ext))
        base   = filename.rsplit('.', 1)[0]
        target = os.path.join(BACKUP_DIR, base + ext)
        if os.path.exists(target):
            os.remove(target)
            deleted += 1

    if deleted:
        flash(f'🗑️ Backup removido: {filename}', 'success')
    else:
        flash('Arquivo não encontrado.', 'error')
    return redirect(url_for('admin.backup_index'))


@admin_bp.route('/backup/api/status')
@login_required
@admin_required
def backup_api_status():
    """API: retorna status do último backup (para polling)"""
    from utils.backup import get_backup_stats
    return jsonify(get_backup_stats())