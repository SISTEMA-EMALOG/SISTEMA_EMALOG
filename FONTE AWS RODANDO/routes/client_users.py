
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from models import User, Client
from app import db
from werkzeug.security import generate_password_hash
from datetime import datetime

client_users_bp = Blueprint('client_users', __name__, url_prefix='/client/users')

@client_users_bp.route('/admin/<int:client_id>')
@login_required
def admin_list(client_id):
    """Admin/operador view of users for a specific client"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))
    client = Client.query.get_or_404(client_id)
    users = User.query.filter_by(client_id=client_id).all()
    return render_template('client_users/admin_list.html', users=users, client=client, current_year=datetime.now().year)

@client_users_bp.route('/admin/<int:client_id>/new', methods=['GET', 'POST'])
@login_required
def admin_new(client_id):
    """Admin/operador creates a new user for a specific client"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))
    client = Client.query.get_or_404(client_id)
    current_count = User.query.filter_by(client_id=client_id).count()
    if current_count >= 5:
        flash('Limite máximo de 5 usuários por cliente atingido.', 'error')
        return redirect(url_for('client_users.admin_list', client_id=client_id))
    if request.method == 'POST':
        try:
            username = request.form.get('username', '').strip()
            email = request.form.get('email', '').strip()
            if not username or not email:
                flash('Nome de usuário e email são obrigatórios.', 'error')
                back = request.form.get('_back') or url_for('client_users.admin_list', client_id=client_id)
                return redirect(back)
            existing = User.query.filter((User.username == username) | (User.email == email)).first()
            if existing:
                flash('Nome de usuário ou email já existe.', 'error')
                back = request.form.get('_back') or url_for('client_users.admin_list', client_id=client_id)
                return redirect(back)
            user = User()
            user.username = username
            user.email = email
            user.role = 'cliente'
            user.client_id = client_id
            user.active = True
            user.must_change_password = True
            import secrets as _sec
            default_password = _sec.token_urlsafe(10)
            user.password_hash = generate_password_hash(default_password)
            db.session.add(user)
            db.session.commit()
            flash(f'Usuário criado! Senha inicial: {default_password} (deve ser alterada no primeiro login)', 'success')
            # Redirect back to wherever the form came from (client edit page or admin_list)
            back = request.form.get('_back') or url_for('client_users.admin_list', client_id=client_id)
            return redirect(back)
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao criar usuário: {str(e)}', 'error')
    back = request.args.get('_back') or url_for('client_users.admin_list', client_id=client_id)
    return redirect(back)

@client_users_bp.route('/admin/<int:client_id>/user/<int:user_id>/toggle', methods=['POST'])
@login_required
def admin_toggle(client_id, user_id):
    """Admin toggles status of a client user"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'message': 'Acesso negado'})
    user = User.query.filter_by(id=user_id, client_id=client_id).first_or_404()
    user.active = not user.active
    db.session.commit()
    status = "ativado" if user.active else "desativado"
    return jsonify({'success': True, 'message': f'Usuário {status} com sucesso!'})

@client_users_bp.route('/admin/<int:client_id>/user/<int:user_id>/reset-password', methods=['POST'])
@login_required
def admin_reset_password(client_id, user_id):
    """Admin resets password of a client user"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'message': 'Acesso negado'})
    user = User.query.filter_by(id=user_id, client_id=client_id).first_or_404()
    import secrets as _sec
    new_password = _sec.token_urlsafe(10)
    user.password_hash = generate_password_hash(new_password)
    user.must_change_password = True
    db.session.commit()
    return jsonify({'success': True, 'message': f'Senha resetada!', 'password': new_password})

@client_users_bp.route('/')
@login_required
def index():
    """Lista de usuários do cliente"""
    if current_user.role != 'cliente' or not current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))
    
    # Buscar usuários associados ao cliente
    users = User.query.filter_by(client_id=current_user.client_id).all()
    
    return render_template('client_users/index.html', users=users)

@client_users_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    """Criar novo usuário para o cliente"""
    if current_user.role != 'cliente' or not current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))
    
    # Verificar limite de usuários (máximo 5 por cliente)
    current_users_count = User.query.filter_by(client_id=current_user.client_id).count()
    if current_users_count >= 5:
        flash('Limite máximo de 5 usuários por cliente atingido.', 'error')
        return redirect(url_for('client_users.index'))

    # Domínio do e-mail do usuário logado
    my_domain = current_user.email.split('@')[1].lower() if '@' in current_user.email else None
    
    if request.method == 'POST':
        try:
            # Validar campos obrigatórios
            required_fields = ['username', 'email']
            for field in required_fields:
                if not request.form.get(field):
                    flash(f'Campo obrigatório: {field}', 'error')
                    return render_template('client_users/form.html', my_domain=my_domain)
            
            new_email = request.form['email'].strip().lower()

            # Validar domínio do e-mail — deve ser o mesmo domínio da empresa
            if my_domain:
                new_domain = new_email.split('@')[1].lower() if '@' in new_email else ''
                if new_domain != my_domain:
                    flash(f'O e-mail deve pertencer ao domínio @{my_domain} da sua empresa.', 'error')
                    return render_template('client_users/form.html', my_domain=my_domain)
            
            # Verificar se usuário já existe
            existing_user = User.query.filter(
                (User.username == request.form['username']) |
                (User.email == new_email)
            ).first()
            
            if existing_user:
                flash('Nome de usuário ou email já existe.', 'error')
                return render_template('client_users/form.html', my_domain=my_domain)
            
            # Criar usuário
            user = User()
            user.username = request.form['username']
            user.email = new_email
            user.role = 'cliente'
            user.client_id = current_user.client_id
            user.active = True
            user.must_change_password = True
            
            import secrets as _sec
            default_password = _sec.token_urlsafe(10)
            user.password_hash = generate_password_hash(default_password)
            
            db.session.add(user)
            db.session.commit()
            
            flash(f'Usuário criado com sucesso! Senha inicial: {default_password} (deve ser alterada no primeiro login)', 'success')
            return redirect(url_for('client_users.index'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao criar usuário: {str(e)}', 'error')
    
    return render_template('client_users/form.html', my_domain=my_domain)

@client_users_bp.route('/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit(id):
    """Editar usuário do cliente"""
    if current_user.role != 'cliente' or not current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))
    
    # Verificar se o usuário pertence ao mesmo cliente
    user = User.query.filter_by(id=id, client_id=current_user.client_id).first_or_404()
    
    if request.method == 'POST':
        try:
            user.username = request.form['username']
            user.email = request.form['email']
            user.active = bool(request.form.get('active'))
            
            # Atualizar senha se fornecida
            if request.form.get('password'):
                user.password_hash = generate_password_hash(request.form['password'])
                user.must_change_password = True
            
            db.session.commit()
            flash('Usuário atualizado com sucesso!', 'success')
            return redirect(url_for('client_users.index'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao atualizar usuário: {str(e)}', 'error')
    
    return render_template('client_users/form.html', user=user)

@client_users_bp.route('/<int:id>/toggle-status', methods=['POST'])
@login_required
def toggle_status(id):
    """Ativar/desativar usuário"""
    if current_user.role != 'cliente' or not current_user.client_id:
        return jsonify({'success': False, 'message': 'Acesso negado'})
    
    # Verificar se o usuário pertence ao mesmo cliente
    user = User.query.filter_by(id=id, client_id=current_user.client_id).first_or_404()
    
    if user.id == current_user.id:
        return jsonify({'success': False, 'message': 'Você não pode desativar sua própria conta'})
    
    user.active = not user.active
    db.session.commit()
    
    status_text = "ativado" if user.active else "desativado"
    return jsonify({
        'success': True,
        'message': f'Usuário {status_text} com sucesso!'
    })

@client_users_bp.route('/<int:id>/reset-password', methods=['POST'])
@login_required
def reset_password(id):
    """Resetar senha do usuário"""
    if current_user.role != 'cliente' or not current_user.client_id:
        return jsonify({'success': False, 'message': 'Acesso negado'})
    
    # Verificar se o usuário pertence ao mesmo cliente
    user = User.query.filter_by(id=id, client_id=current_user.client_id).first_or_404()
    
    try:
        # Gerar nova senha padrão
        import secrets as _sec
        new_password = _sec.token_urlsafe(10)
        user.password_hash = generate_password_hash(new_password)
        user.must_change_password = True
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Senha resetada! Nova senha: {new_password}',
            'password': new_password
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({
            'success': False,
            'message': f'Erro ao resetar senha: {str(e)}'
        })
