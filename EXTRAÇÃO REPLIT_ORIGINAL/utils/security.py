
import re
import os
import secrets
import time
import logging
from functools import wraps
from urllib.parse import urlparse
from werkzeug.utils import secure_filename
from flask import current_app, session, request, abort, url_for
from flask_login import current_user

# ─── CSRF ────────────────────────────────────────────────────────────────────

def generate_csrf_token() -> str:
    """Return (and store) a per-session CSRF token."""
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']

CSRF_EXEMPT_PREFIXES = (
    '/socket.io',
    '/ema/webhook',       # Evolution API — sem sessão de browser
    '/bids/webhook',      # Driver bid webhooks
)

def validate_csrf_token() -> None:
    """Abort 403 if CSRF token is missing or invalid.
    Call from app.before_request."""
    if request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
        return
    for prefix in CSRF_EXEMPT_PREFIXES:
        if request.path.startswith(prefix):
            return
    stored = session.get('_csrf_token')
    submitted = (
        request.form.get('_csrf_token')
        or request.headers.get('X-CSRFToken')
        or request.headers.get('X-XSRF-TOKEN')
    )
    if not stored or not submitted or not secrets.compare_digest(str(stored), str(submitted)):
        logging.warning(
            'CSRF validation failed — IP: %s Path: %s',
            request.remote_addr, request.path
        )
        abort(403)

# ─── OPEN-REDIRECT PROTECTION ────────────────────────────────────────────────

def safe_redirect_url(target: str | None, default_endpoint: str = 'dashboard.index') -> str:
    """Return `target` only when it is a relative path inside this app."""
    if not target:
        return url_for(default_endpoint)
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        logging.warning('Blocked open-redirect to: %s from IP %s', target, request.remote_addr)
        return url_for(default_endpoint)
    return target

# ─── LOGIN RATE LIMITER ──────────────────────────────────────────────────────

_login_attempts: dict[str, list[float]] = {}
MAX_FAILED_ATTEMPTS = 10
WINDOW_SECONDS = 300

def record_failed_login(ip: str) -> None:
    now = time.time()
    recent = [t for t in _login_attempts.get(ip, []) if now - t < WINDOW_SECONDS]
    recent.append(now)
    _login_attempts[ip] = recent

def is_login_blocked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _login_attempts.get(ip, []) if now - t < WINDOW_SECONDS]
    _login_attempts[ip] = recent
    return len(recent) >= MAX_FAILED_ATTEMPTS

def remaining_lockout(ip: str) -> int:
    """Seconds remaining in lockout (0 = not locked)."""
    now = time.time()
    recent = [t for t in _login_attempts.get(ip, []) if now - t < WINDOW_SECONDS]
    if len(recent) < MAX_FAILED_ATTEMPTS:
        return 0
    return max(0, int(WINDOW_SECONDS - (now - min(recent))))

def clear_failed_logins(ip: str) -> None:
    _login_attempts.pop(ip, None)

# ─── ROLE DECORATORS ─────────────────────────────────────────────────────────

def staff_only(f):
    """Restrict route to admin and operador roles."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ('admin', 'operador'):
            abort(403)
        return f(*args, **kwargs)
    return decorated

def admin_only(f):
    """Restrict route to admin role only."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            abort(403)
        return f(*args, **kwargs)
    return decorated

def validate_file_upload(file):
    """Validate uploaded file for security"""
    if not file or not file.filename:
        return False, "Nenhum arquivo selecionado"
    
    filename = secure_filename(file.filename)
    if not filename:
        return False, "Nome de arquivo inválido"
    
    # Check file extension
    allowed_extensions = current_app.config.get('ALLOWED_EXTENSIONS', set())
    if '.' not in filename or filename.rsplit('.', 1)[1].lower() not in allowed_extensions:
        return False, f"Tipo de arquivo não permitido. Permitidos: {', '.join(allowed_extensions)}"
    
    # Check file size
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    
    max_size = current_app.config.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024)
    if file_size > max_size:
        return False, f"Arquivo muito grande. Máximo: {max_size // (1024 * 1024)}MB"
    
    return True, filename

def validate_cpf(cpf):
    """Validate Brazilian CPF"""
    if not cpf:
        return False
    
    # Remove non-digits
    cpf = re.sub(r'[^0-9]', '', cpf)
    
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    
    # Calculate first digit
    sum1 = sum(int(cpf[i]) * (10 - i) for i in range(9))
    digit1 = 11 - (sum1 % 11)
    if digit1 >= 10:
        digit1 = 0
    
    # Calculate second digit
    sum2 = sum(int(cpf[i]) * (11 - i) for i in range(10))
    digit2 = 11 - (sum2 % 11)
    if digit2 >= 10:
        digit2 = 0
    
    return cpf[-2:] == f"{digit1}{digit2}"

def validate_cnpj(cnpj):
    """Validate Brazilian CNPJ"""
    if not cnpj:
        return False
    
    # Remove non-digits
    cnpj = re.sub(r'[^0-9]', '', cnpj)
    
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False
    
    # Calculate digits
    weights1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    weights2 = [6, 7, 8, 9, 2, 3, 4, 5, 6, 7, 8, 9]
    
    sum1 = sum(int(cnpj[i]) * weights1[i] for i in range(12))
    digit1 = 11 - (sum1 % 11)
    if digit1 >= 10:
        digit1 = 0
    
    sum2 = sum(int(cnpj[i]) * weights2[i] for i in range(13))
    digit2 = 11 - (sum2 % 11)
    if digit2 >= 10:
        digit2 = 0
    
    return cnpj[-2:] == f"{digit1}{digit2}"

def sanitize_string(value, max_length=None):
    """Sanitize string input"""
    if not value:
        return ""
    
    # Remove HTML tags and dangerous characters
    value = re.sub(r'<[^>]*>', '', str(value))
    value = re.sub(r'[<>"\']', '', value)
    
    if max_length:
        value = value[:max_length]
    
    return value.strip()
