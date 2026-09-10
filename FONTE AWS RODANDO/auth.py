from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from models import User
from app import db
from utils.security import (
    record_failed_login, is_login_blocked, remaining_lockout,
    clear_failed_logins, safe_redirect_url
)
import logging

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

@auth_bp.route('/')
def root():
    """Root route redirects to login"""
    return redirect(url_for('auth.login'))

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.remote_addr or '0.0.0.0'

        # Rate limiting check
        if is_login_blocked(ip):
            secs = remaining_lockout(ip)
            mins = (secs + 59) // 60
            flash(f'Muitas tentativas incorretas. Tente novamente em {mins} minuto(s).', 'error')
            return render_template('auth/login.html')

        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))

        if not email or not password:
            flash('Email e senha são obrigatórios.', 'error')
            return render_template('auth/login.html')

        if len(email) > 120 or len(password) > 256:
            flash('Dados inválidos.', 'error')
            return render_template('auth/login.html')

        user = User.query.filter_by(email=email).first()

        if not user or not check_password_hash(user.password_hash, password):
            record_failed_login(ip)
            # Generic message — don't reveal whether email exists
            flash('Email ou senha incorretos.', 'error')
            return render_template('auth/login.html')

        if not user.is_active:
            flash('Usuário inativo. Entre em contato com o administrador.', 'error')
            return render_template('auth/login.html')

        clear_failed_logins(ip)
        login_user(user, remember=remember)

        from datetime import datetime
        user.last_login = datetime.utcnow()
        try:
            db.session.commit()
        except Exception as e:
            logging.warning('Failed to update last login time: %s', e)
            db.session.rollback()

        if user.first_login:
            flash('Bem-vindo ao EMALOG! Este é seu primeiro acesso.', 'info')
            user.first_login = False
            db.session.commit()

        if user.must_change_password:
            flash('Por segurança, você deve trocar sua senha antes de continuar.', 'warning')
            return redirect(url_for('auth.change_password'))

        # Safe redirect — reject external URLs
        next_page = safe_redirect_url(request.args.get('next'), 'dashboard.index')
        return redirect(next_page)

    return render_template('auth/login.html')

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logout realizado com sucesso.', 'success')
    return redirect(url_for('auth.login'))


@auth_bp.route('/keep-alive', methods=['POST'])
@login_required
def keep_alive():
    """Refreshes server-side session when user confirms they are still active."""
    from flask import session
    session.modified = True
    return jsonify({'ok': True})

@auth_bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        if not current_password or not check_password_hash(current_user.password_hash, current_password):
            flash('Senha atual incorreta.', 'error')
            return render_template('auth/change_password.html')
        
        if not new_password or not confirm_password:
            flash('Todos os campos são obrigatórios.', 'error')
            return render_template('auth/change_password.html')
        
        if new_password != confirm_password:
            flash('Nova senha e confirmação não coincidem.', 'error')
            return render_template('auth/change_password.html')
        
        import re as _re
        if len(new_password) < 8:
            flash('Nova senha deve ter pelo menos 8 caracteres.', 'error')
            return render_template('auth/change_password.html')
        if not _re.search(r'[A-Za-z]', new_password) or not _re.search(r'[0-9]', new_password):
            flash('A senha deve conter letras e números.', 'error')
            return render_template('auth/change_password.html')
        
        current_user.password_hash = generate_password_hash(new_password)
        current_user.must_change_password = False  # Senha foi trocada
        db.session.commit()
        
        flash('Senha alterada com sucesso.', 'success')
        return redirect(url_for('dashboard.index'))
    
    return render_template('auth/change_password.html')
