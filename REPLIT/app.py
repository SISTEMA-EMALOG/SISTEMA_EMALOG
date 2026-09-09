
import os
import logging
from flask import Flask, redirect, url_for, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user
from flask_socketio import SocketIO, join_room, emit, disconnect
from flask_mail import Mail
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import datetime

# Configure logging — INFO in production, DEBUG only when explicitly set
_log_level = logging.DEBUG if os.environ.get('FLASK_DEBUG') == '1' else logging.INFO
logging.basicConfig(level=_log_level)

class Base(DeclarativeBase):
    pass

db = SQLAlchemy(model_class=Base)
login_manager = LoginManager()
mail = Mail()

# Initialize SocketIO sem configuração inicial
socketio = SocketIO()

def create_app():
    app = Flask(__name__)

    # Security configurations
    secret_key = os.environ.get("SESSION_SECRET")
    if not secret_key:
        import secrets
        secret_key = secrets.token_hex(32)
        print("WARNING: Using generated secret key. Set SESSION_SECRET environment variable for production!")

    app.secret_key = secret_key
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    app.config['TEMPLATES_AUTO_RELOAD'] = True

    # ── Session cookie hardening ──────────────────────────────────────────────
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE'] = True   # served over HTTPS via proxy
    app.config['PERMANENT_SESSION_LIFETIME'] = 43200  # 12 h
    app.config['IDLE_TIMEOUT_MINUTES'] = int(os.environ.get('IDLE_TIMEOUT_MINUTES', 30))

    # ── Security headers ─────────────────────────────────────────────────────
    @app.after_request
    def add_security_headers(response):
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains; preload'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
        # Cache headers por tipo de conteúdo
        if 'text/html' in response.content_type:
            # Páginas HTML dinâmicas: sem cache
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'
        elif request.path.startswith('/static/'):
            # Assets estáticos: cache longo (CSS, JS, imagens)
            if any(request.path.endswith(ext) for ext in ('.css', '.js', '.woff', '.woff2', '.ttf', '.eot')):
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            elif any(request.path.endswith(ext) for ext in ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.webp')):
                response.headers['Cache-Control'] = 'public, max-age=86400'
        # Permit CDNs used by the app (Tailwind, FA, Chart.js, Socket.IO)
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' cdn.tailwindcss.com cdnjs.cloudflare.com cdn.jsdelivr.net unpkg.com; "
            "style-src 'self' 'unsafe-inline' cdn.tailwindcss.com cdnjs.cloudflare.com fonts.googleapis.com unpkg.com; "
            "font-src 'self' cdnjs.cloudflare.com fonts.gstatic.com; "
            "img-src 'self' data: blob: nominatim.openstreetmap.org *.tile.openstreetmap.org; "
            "connect-src 'self' nominatim.openstreetmap.org router.project-osrm.org cdnjs.cloudflare.com cdn.jsdelivr.net wss: ws:; "
            "frame-ancestors 'self';"
        )
        return response

    # ── CSRF protection ───────────────────────────────────────────────────────
    from utils.security import validate_csrf_token, generate_csrf_token

    @app.before_request
    def csrf_protect():
        validate_csrf_token()

    # Expose csrf_token() as a Jinja2 global so templates can use it
    app.jinja_env.globals['csrf_token'] = generate_csrf_token

    # Simple markdown → HTML filter for Jinja2 (used in assistant history)
    import re as _re
    from markupsafe import Markup, escape as _escape
    def _safe_markdown(text):
        html = str(_escape(text))
        html = _re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', html)
        html = _re.sub(r'__(.*?)__',     r'<strong>\1</strong>', html)
        html = _re.sub(r'\*(.*?)\*',     r'<em>\1</em>',         html)
        html = _re.sub(r'`([^`]+)`',     r'<code>\1</code>',     html)
        html = _re.sub(r'^### (.+)$', r'<h3>\1</h3>', html, flags=_re.MULTILINE)
        html = _re.sub(r'^## (.+)$',  r'<h2>\1</h2>', html, flags=_re.MULTILINE)
        html = _re.sub(r'^# (.+)$',   r'<h1>\1</h1>', html, flags=_re.MULTILINE)
        html = _re.sub(r'^[•\-\*] (.+)$', r'<li>\1</li>', html, flags=_re.MULTILINE)
        html = _re.sub(r'(<li>.*?</li>\n?)+', lambda m: f'<ul>{m.group(0)}</ul>', html, flags=_re.DOTALL)
        html = _re.sub(r'^\d+\. (.+)$', r'<li>\1</li>', html, flags=_re.MULTILINE)
        html = html.replace('\n\n', '</p><p>').replace('\n', '<br>')
        return Markup(f'<p>{html}</p>')
    app.jinja_env.filters['safe_markdown'] = _safe_markdown

    # ── 403 error handler ─────────────────────────────────────────────────────
    @app.errorhandler(403)
    def forbidden(error):
        from flask import render_template as _rt
        return _rt('errors/403.html'), 403

    # Configure database — priority order:
    #  1. Native Replit PostgreSQL via PG* env vars (most reliable)
    #  2. DATABASE_URL env var (legacy Neon or external)
    #  3. SQLite fallback (development only)
    def _build_replit_pg_url():
        """Build PostgreSQL URL from Replit's individual PG* variables."""
        host = os.environ.get("PGHOST")
        port = os.environ.get("PGPORT", "5432")
        user = os.environ.get("PGUSER")
        password = os.environ.get("PGPASSWORD")
        dbname = os.environ.get("PGDATABASE")
        if all([host, user, password, dbname]):
            # Local Replit managed DB (helium) doesn't use SSL
            ssl = "" if host in ("helium", "localhost", "127.0.0.1") else "?sslmode=require"
            return f"postgresql://{user}:{password}@{host}:{port}/{dbname}{ssl}"
        return None

    def _normalise_pg_url(url):
        """Ensure PostgreSQL URL has sslmode=require (required by Neon/Replit)."""
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        if "postgresql://" in url and "sslmode=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}sslmode=require"
        return url

    def _test_pg(url):
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=3)
        conn.close()

    sqlite_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "emalog.db")
    os.makedirs(os.path.dirname(sqlite_path), exist_ok=True)
    database_url = None

    # 1. Try Replit native PG* vars first (with SSL)
    replit_pg_url = _build_replit_pg_url()
    if replit_pg_url:
        try:
            _test_pg(replit_pg_url)
            database_url = replit_pg_url
            logging.info("✅ Banco: PostgreSQL Replit nativo (PG* vars)")
        except Exception as e:
            logging.warning(f"⚠️ PG* vars falhou: {e}")

    # 2. Try DATABASE_URL if PG* vars didn't work (normalise SSL)
    if not database_url:
        env_url = _normalise_pg_url(os.environ.get("DATABASE_URL", ""))
        if env_url.startswith("postgresql://"):
            try:
                _test_pg(env_url)
                database_url = env_url
                logging.info("✅ Banco: PostgreSQL via DATABASE_URL")
            except Exception as e:
                logging.warning(f"⚠️ DATABASE_URL inacessível ({e}). Usando SQLite.")

    # 3. SQLite fallback
    if not database_url:
        database_url = f"sqlite:///{sqlite_path}"
        logging.warning(f"⚠️ Banco: SQLite local (fallback) — {sqlite_path}")

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    logging.info(f"🗄️ Banco: {'SQLite local' if 'sqlite' in database_url else 'PostgreSQL'}")

    pg_options = {
        "pool_recycle": 300,
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 20,
        "pool_timeout": 10,
    }
    sqlite_options = {"connect_args": {"check_same_thread": False}}
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = sqlite_options if "sqlite" in database_url else pg_options
    app.config["UPLOAD_FOLDER"] = "uploads"
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max file size
    app.config["ALLOWED_EXTENSIONS"] = {'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx', 'xlsx', 'xls'}

    # Email configuration
    app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'email-ssl.com.br')
    app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 465))
    app.config['MAIL_USE_SSL'] = True
    app.config['MAIL_USE_TLS'] = False
    app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'comercial@emalog.com.br')
    app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
    app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'comercial@emalog.com.br')

    # Create upload directory
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs("instance", exist_ok=True)

    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Por favor, faça login para acessar esta página.'
    login_manager.login_message_category = 'info'
    mail.init_app(app)

    # Configure SocketIO (threading mode para compatibilidade com gunicorn sync workers)
    # CORS: usa SOCKETIO_ALLOWED_ORIGINS se definida; caso contrário, permite
    # apenas a própria origem do app (via verificação de header pelo browser).
    _raw_origins = os.environ.get('SOCKETIO_ALLOWED_ORIGINS', '').strip()
    if _raw_origins:
        _cors = [o.strip() for o in _raw_origins.split(',') if o.strip()]
    else:
        # None = Flask-SocketIO delega ao Flask-CORS / browser same-origin check
        # Seguro porque todas as rotas SocketIO exigem sessão autenticada
        _cors = None

    socketio.init_app(
        app,
        cors_allowed_origins=_cors,
        async_mode='threading',
        logger=False,
        engineio_logger=False,
        manage_session=False,
        ping_timeout=60,
        ping_interval=25
    )
    print("✅ SocketIO configurado")

    # Create all database tables (with retry for cold-start DB endpoints)
    with app.app_context():
        import models  # noqa: F401
        import time
        for attempt in range(5):
            try:
                db.create_all()
                logging.info("✅ Banco de dados inicializado")

                # Schema must be ready before the first request reaches a blueprint.
                from utils.migrations import run_migrations
                run_migrations(db)

                # ── Operações pós-criação em background ──
                import threading as _threading

                def _bg_post_init():
                    """Roda em background: admin + seed."""
                    with app.app_context():
                        # 1. Admin padrão
                        try:
                            create_default_admin()
                            logging.info("✅ Admin padrão configurado")
                        except Exception as adm_err:
                            logging.error(f"⚠️ Erro ao configurar admin: {adm_err}")

                        # 3. Seed de dados
                        try:
                            from utils.seed import run_seed_if_empty
                            run_seed_if_empty(app, db)
                        except Exception as seed_err:
                            logging.warning(f"⚠️ Seed ignorado: {seed_err}")

                _t = _threading.Thread(target=_bg_post_init, daemon=True, name='db-post-init')
                _t.start()

                break
            except Exception as e:
                if attempt < 4:
                    logging.warning(f"⚠️ DB não disponível (tentativa {attempt+1}/5): {e}")
                    time.sleep(3)
                else:
                    logging.error(f"❌ Falha ao conectar ao banco após 5 tentativas: {e}")
                    raise

    # User loader for Flask-Login
    @login_manager.user_loader
    def load_user(user_id):
        from models import User
        return User.query.get(int(user_id))

    # Importar e registrar blueprints
    blueprints_registered = []
    
    try:
        # Auth blueprint
        try:
            from auth import auth_bp
            app.register_blueprint(auth_bp)
            blueprints_registered.append('auth')
        except Exception as e:
            print(f"⚠️ Erro ao registrar auth blueprint: {e}")

        # Dashboard blueprint
        try:
            from routes.dashboard import dashboard_bp
            app.register_blueprint(dashboard_bp)
            blueprints_registered.append('dashboard')
        except Exception as e:
            print(f"⚠️ Erro ao registrar dashboard blueprint: {e}")

        # Quotes blueprint
        try:
            from routes.quotes import quotes_bp
            app.register_blueprint(quotes_bp)
            blueprints_registered.append('quotes')
            print("✅ Blueprint quotes registrado com sucesso")
        except Exception as e:
            print(f"⚠️ Erro ao registrar quotes blueprint: {e}")
            import traceback
            traceback.print_exc()

        # Freight blueprint
        try:
            from routes.freight import freight_bp
            app.register_blueprint(freight_bp)
            blueprints_registered.append('freight')
        except Exception as e:
            print(f"⚠️ Erro ao registrar freight blueprint: {e}")

        # Clients blueprint
        try:
            from routes.clients import clients_bp
            app.register_blueprint(clients_bp)
            blueprints_registered.append('clients')
        except Exception as e:
            print(f"⚠️ Erro ao registrar clients blueprint: {e}")

        # Drivers blueprint
        try:
            from routes.drivers import drivers_bp
            app.register_blueprint(drivers_bp)
            blueprints_registered.append('drivers')
        except Exception as e:
            print(f"⚠️ Erro ao registrar drivers blueprint: {e}")

        # Client users blueprint
        try:
            from routes.client_users import client_users_bp
            app.register_blueprint(client_users_bp)
            blueprints_registered.append('client_users')
        except Exception as e:
            print(f"⚠️ Erro ao registrar client_users blueprint: {e}")

        # Admin blueprint
        try:
            from routes.admin import admin_bp
            app.register_blueprint(admin_bp)
            blueprints_registered.append('admin')
        except Exception as e:
            print(f"⚠️ Erro ao registrar admin blueprint: {e}")

        # Financial blueprint
        try:
            from routes.financial import financial_bp
            app.register_blueprint(financial_bp)
            blueprints_registered.append('financial')
        except Exception as e:
            print(f"⚠️ Erro ao registrar financial blueprint: {e}")

        # Reports blueprint
        try:
            from routes.reports import reports_bp
            app.register_blueprint(reports_bp)
            blueprints_registered.append('reports')
        except Exception as e:
            print(f"⚠️ Erro ao registrar reports blueprint: {e}")

        # Chat blueprint
        try:
            from routes.chat import chat_bp
            app.register_blueprint(chat_bp)
            blueprints_registered.append('chat')
        except Exception as e:
            print(f"⚠️ Erro ao registrar chat blueprint: {e}")

        # Notifications blueprint (apenas SocketIO)
        try:
            from routes.notifications import notifications_bp
            app.register_blueprint(notifications_bp)
            blueprints_registered.append('notifications')
        except Exception as e:
            print(f"⚠️ Erro ao registrar notifications blueprint: {e}")

        # CRM blueprint
        try:
            from routes.crm import crm_bp
            app.register_blueprint(crm_bp)
            blueprints_registered.append('crm')
        except Exception as e:
            print(f"⚠️ Erro ao registrar crm blueprint: {e}")
            import traceback; traceback.print_exc()

        # Assistant blueprint
        try:
            from routes.assistant import assistant_bp
            app.register_blueprint(assistant_bp)
            blueprints_registered.append('assistant')
        except Exception as e:
            print(f"⚠️ Erro ao registrar assistant blueprint: {e}")
            import traceback; traceback.print_exc()

        # EMA Agent blueprint
        try:
            from routes.ema_agent import ema_bp
            app.register_blueprint(ema_bp)
            blueprints_registered.append('ema')
        except Exception as e:
            print(f"⚠️ Erro ao registrar ema blueprint: {e}")
            import traceback; traceback.print_exc()

        # Driver Bids blueprint
        try:
            from routes.driver_bids import bids_bp
            app.register_blueprint(bids_bp)
            blueprints_registered.append('bids')
        except Exception as e:
            print(f"⚠️ Erro ao registrar bids blueprint: {e}")
            import traceback; traceback.print_exc()

        # Contracting Kanban blueprint
        try:
            from routes.contracting import contracting_bp
            app.register_blueprint(contracting_bp)
            blueprints_registered.append('contracting')
        except Exception as e:
            print(f"⚠️ Erro ao registrar contracting blueprint: {e}")
            import traceback; traceback.print_exc()

        # Demo Screenshots blueprint (temporary — remove after presentation)
        try:
            from routes.demo_screenshots import demo_bp
            app.register_blueprint(demo_bp)
            blueprints_registered.append('demo')
        except Exception as e:
            print(f"⚠️ Erro ao registrar demo blueprint: {e}")

        print(f"✅ Blueprints registrados: {', '.join(blueprints_registered)}")

    except Exception as e:
        print(f"⚠️ Erro geral ao registrar blueprints: {e}")
        import traceback
        traceback.print_exc()

    # Mockup sandbox proxy — forward /__mockup/ to vite dev server on port 23636
    @app.route('/__mockup/', defaults={'path': ''})
    @app.route('/__mockup/<path:path>')
    def mockup_proxy(path):
        import urllib.request
        import urllib.error
        from flask import Response, request as flask_request
        target = f'http://localhost:23636/__mockup/{path}'
        if flask_request.query_string:
            target += '?' + flask_request.query_string.decode('utf-8')
        try:
            req = urllib.request.Request(target, headers={
                k: v for k, v in flask_request.headers if k.lower() not in ('host', 'content-length')
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read()
                headers = dict(resp.headers)
                # Remove hop-by-hop headers
                for h in ('transfer-encoding', 'connection', 'keep-alive'):
                    headers.pop(h, None)
                    headers.pop(h.title(), None)
                return Response(content, status=resp.status,
                                headers=headers,
                                content_type=resp.headers.get('Content-Type', 'text/html'))
        except urllib.error.HTTPError as e:
            return Response(e.read(), status=e.code)
        except Exception:
            return Response('Mockup server not available', status=503)

    # Root route
    @app.route('/')
    def root():
        """Root route redirects to login"""
        if current_user.is_authenticated:
            return redirect(url_for('dashboard.index'))
        return redirect(url_for('auth.login'))

    @app.route('/apresentacao-cliente')
    def apresentacao_cliente():
        """Serve the animated client portal presentation."""
        from flask import send_from_directory
        return send_from_directory('static', 'apresentacao_cliente.html')

    @app.route('/guia-cliente')
    def guia_cliente():
        """Serve the real-screenshot client portal guide."""
        from flask import send_from_directory
        return send_from_directory('static', 'guia_cliente_emalog.html')

    # Error handlers
    @app.errorhandler(404)
    def not_found(error):
        from flask import render_template
        return render_template('errors/404.html'), 404

    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        from flask import render_template
        return render_template('errors/500.html'), 500

    @app.errorhandler(413)
    def file_too_large(error):
        from flask import flash, redirect, request
        flash('Arquivo muito grande. Máximo permitido: 16MB', 'error')
        return redirect(request.referrer or '/')

    # Admin e seed agora rodam em background (thread db-post-init)

    # ── Agendador de backup automático diário ─────────────────────────────
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        def _auto_backup():
            """Cria backup automático diário dentro do contexto da app."""
            with app.app_context():
                from utils.backup import create_backup
                result = create_backup()
                if result.get('ok'):
                    logging.info(f"⏰ Backup automático criado: {result['filename']} "
                                 f"({result['tables']} tabelas, {result['rows']} registros)")
                else:
                    logging.error(f"⏰ Backup automático falhou: {result.get('error')}")

        def _ema_inactivity_check():
            """Runs every minute. Sends reminder after 2 min inactivity; abandons after 3 min."""
            with app.app_context():
                try:
                    from models import EmaSession, db
                    from datetime import datetime, timedelta
                    from utils.evolution_api import send_text
                    from utils.ema_agent import _append_history

                    now              = datetime.utcnow()
                    remind_after     = timedelta(minutes=5)   # 5 min sem resposta → lembrete
                    abandon_after_reminder = timedelta(minutes=2)  # 2 min após lembrete → encerra

                    active_statuses = ('active', 'awaiting_file', 'awaiting_confirmation')
                    sessions = EmaSession.query.filter(
                        EmaSession.status.in_(active_statuses)
                    ).all()

                    for s in sessions:
                        if s.driver and s.driver.whatsapp_mode == 'manual':
                            continue
                        # Referência: última msg do motorista → última atividade da sessão → criação
                        # IMPORTANTE: usar updated_at (set a cada msg, inclusive da EMA) e NÃO started_at,
                        # para evitar disparar lembrete imediatamente após reinício do app (gunicorn --reload)
                        ref_time = s.last_driver_msg_at or s.updated_at or s.created_at
                        if ref_time is None:
                            continue
                        idle = now - ref_time

                        # Encerrar: lembrete foi enviado E já passaram 2 min desde o lembrete
                        if s.reminder_sent_at and (now - s.reminder_sent_at) >= abandon_after_reminder:
                            s.status          = 'abandoned'
                            s.abandoned_reason= 'Sem resposta após lembrete (2 min)'
                            s.updated_at      = now
                            msg = (
                                "⏰ Oi! Parece que você está ocupado agora.\n"
                                "Sem problema — quando quiser continuar o cadastro é só falar aqui! "
                                "Qualquer dúvida, o operador da EMALOG pode te ajudar. 😊"
                            )
                            send_text(s.driver.phone, msg)
                            _append_history(s, 'ema', '[Sistema] Conversa encerrada por inatividade.')
                            logging.info(f"[EMA] Sessão #{s.id} ({s.driver.name}) abandonada por inatividade.")

                        # Lembrete: 5 min sem resposta e ainda não enviou lembrete
                        elif idle >= remind_after and s.reminder_sent_at is None:
                            msg = (
                                "👋 Oi! Ainda estou aqui esperando pra completar seu cadastro 😊\n\n"
                                "Quando puder, é só continuar de onde paramos! "
                                "Se precisar de mais tempo, tudo bem — mas sem resposta por mais 2 minutinhos "
                                "vou ter que pausar a conversa."
                            )
                            ok = send_text(s.driver.phone, msg)
                            if ok:
                                s.reminder_sent_at = now
                                _append_history(s, 'ema', msg)
                                logging.info(f"[EMA] Lembrete enviado para sessão #{s.id} ({s.driver.name})")

                    db.session.commit()
                except Exception as exc:
                    logging.error(f"[EMA] Erro no job de inatividade: {exc}", exc_info=True)

        _scheduler = BackgroundScheduler(daemon=True)
        # Backup diário às 02h00 UTC
        _scheduler.add_job(_auto_backup, CronTrigger(hour=2, minute=0),
                           id='daily_backup', replace_existing=True)
        # EMA inactivity check — every minute
        from apscheduler.triggers.interval import IntervalTrigger
        _scheduler.add_job(_ema_inactivity_check, IntervalTrigger(minutes=1),
                           id='ema_inactivity', replace_existing=True)
        _scheduler.start()
        logging.info("⏰ Agendador de backup automático iniciado (02h00 UTC diário)")
        logging.info("⏰ Agendador de inatividade EMA iniciado (verifica a cada 1 min)")
    except Exception as e:
        logging.warning(f"⚠️ Não foi possível iniciar agendador de backup: {e}")

    # Configuração das salas do SocketIO para notificações
    @socketio.on('join_notifications')
    def handle_join_notifications():
        """Entrar na sala de notificações específicas do usuário"""
        if current_user.is_authenticated:
            user_room = f'user_{current_user.id}'
            rooms_joined = [user_room]

            # Entrar na sala específica do usuário
            join_room(user_room)
            logging.info(f"🔗 Usuário {current_user.username} entrou na sala: {user_room}")

            # Se for cliente, entrar também na sala do cliente
            if current_user.role == 'cliente' and current_user.client_id:
                client_room = f'client_{current_user.client_id}'
                join_room(client_room)
                rooms_joined.append(client_room)
                logging.info(f"🏢 Cliente {current_user.username} entrou na sala do cliente: {client_room}")

            # Se for operador/admin, entrar na sala de operadores
            if current_user.role in ['admin', 'operador', 'vendedor']:
                join_room('operators')
                rooms_joined.append('operators')
                logging.info(f"👨‍💼 Operador {current_user.username} entrou na sala de operadores")

            logging.info(f"✅ CONECTADO: {current_user.username} (ID: {current_user.id}, Role: {current_user.role}, Client_ID: {current_user.client_id}) - Salas: {rooms_joined}")

            # Confirmar conexão com detalhes COMPLETOS
            emit('notification_status', {
                'status': 'connected',
                'user_id': current_user.id,
                'username': current_user.username,
                'user_role': current_user.role,
                'client_id': current_user.client_id,
                'room': user_room,
                'rooms': rooms_joined,
                'timestamp': datetime.now().isoformat(),
                'message': f'Conectado como {current_user.role} nas salas {rooms_joined}'
            })
            
            # Enviar teste imediato para verificar conectividade
            emit('test_connectivity', {
                'message': 'Teste de conectividade SocketIO',
                'timestamp': datetime.now().isoformat(),
                'user_id': current_user.id,
                'rooms': rooms_joined
            })

        else:
            logging.warning("❌ Tentativa de conexão não autorizada às notificações")
            emit('notification_status', {'status': 'unauthorized'})
            disconnect()

    return app

def create_default_admin():
    """Create default admin user if not exists"""
    from models import User
    from werkzeug.security import generate_password_hash

    admin = User.query.filter_by(email='patrick.souza@emalog.com.br').first()
    if not admin:
        admin = User()
        admin.username = 'patrick.souza'
        admin.email = 'patrick.souza@emalog.com.br'
        admin.password_hash = generate_password_hash('$Geraldo87')
        admin.role = 'admin'
        admin.active = True
        db.session.add(admin)
        db.session.commit()
        logging.info("Default admin user created successfully")
