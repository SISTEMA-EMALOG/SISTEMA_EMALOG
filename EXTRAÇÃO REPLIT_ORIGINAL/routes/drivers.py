from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, abort
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from models import Driver, AuditLog
from app import db
from utils.excel_handler import export_drivers_excel, import_drivers_excel
import os
import logging
from datetime import datetime

drivers_bp = Blueprint('drivers', __name__, url_prefix='/drivers')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'pdf', 'png', 'jpg', 'jpeg', 'gif'}

@drivers_bp.route('/')
@login_required
def index():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))
    search     = request.args.get('search', '')
    status     = request.args.get('status', 'all')
    truck_type = request.args.get('truck_type', 'all')
    page       = request.args.get('page', 1, type=int)
    per_page   = 20

    query = Driver.query

    if search:
        query = query.filter(Driver.name.contains(search) | Driver.cpf.contains(search))

    if status == 'active':
        query = query.filter_by(active=True)
    elif status == 'inactive':
        query = query.filter_by(active=False)

    if truck_type and truck_type != 'all':
        query = query.filter(Driver.truck_type == truck_type)

    pagination = query.order_by(Driver.name).paginate(page=page, per_page=per_page, error_out=False)
    drivers = pagination.items

    # Count per truck type for filter badges (respects search/status but not truck_type)
    base_q = Driver.query
    if search:
        base_q = base_q.filter(Driver.name.contains(search) | Driver.cpf.contains(search))
    if status == 'active':
        base_q = base_q.filter_by(active=True)
    elif status == 'inactive':
        base_q = base_q.filter_by(active=False)

    from sqlalchemy import func
    type_counts = dict(
        base_q.with_entities(Driver.truck_type, func.count(Driver.id))
               .group_by(Driver.truck_type).all()
    )
    total_count = sum(type_counts.values())

    return render_template('drivers/index.html',
                           drivers=drivers,
                           pagination=pagination,
                           search=search,
                           status=status,
                           truck_type=truck_type,
                           type_counts=type_counts,
                           total_count=total_count)

@drivers_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('drivers.index'))
    
    if request.method == 'POST':
        try:
            # Handle file uploads
            from utils.storage import save_file as _save
            files = {}
            for file_field in ['cnh_document', 'crlv_document', 'address_proof', 'vehicle_photo', 'cavalinho_crlv']:
                if file_field in request.files:
                    file = request.files[file_field]
                    if file and file.filename and allowed_file(file.filename):
                        filename = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
                        key = f"uploads/drivers/{filename}"
                        _save(file.stream, key)
                        files[file_field] = key

            is_carreta = request.form.get('truck_type') == 'carreta'

            def _get(field):
                """Return form value or None if blank."""
                v = request.form.get(field, '').strip()
                return v or None

            def _date(field):
                v = _get(field)
                if not v:
                    return None
                for fmt in ('%Y-%m-%d', '%d/%m/%Y'):
                    try:
                        return datetime.strptime(v, fmt).date()
                    except ValueError:
                        pass
                return None

            def _int(field):
                v = _get(field)
                try:
                    return int(v) if v else None
                except (TypeError, ValueError):
                    return None

            # Create driver — only name + phone are truly required
            driver = Driver(
                name=request.form['name'].strip(),
                phone=request.form['phone'].strip(),
                cpf=_get('cpf'),
                rg=_get('rg'),
                birth_date=_date('birth_date'),
                email=_get('email'),
                # Address fields (all optional — EMA fills later)
                cep=_get('cep'),
                street=_get('street'),
                number=_get('number'),
                complement=_get('complement'),
                neighborhood=_get('neighborhood'),
                city=_get('city'),
                state=_get('state'),
                # CNH and vehicle data
                cnh_expiry=_date('cnh_expiry'),
                antt_number=_get('antt_number'),
                truck_type=_get('truck_type'),
                has_tracker=request.form.get('has_tracker') == '1',
                tracker_type=_get('tracker_type') if request.form.get('has_tracker') == '1' else None,
                vehicle_plate=_get('vehicle_plate'),
                vehicle_model=_get('vehicle_model'),
                vehicle_year=_int('vehicle_year'),
                # Cavalinho (only for carreta type)
                cavalinho_plate=_get('cavalinho_plate') if is_carreta else None,
                cavalinho_model=_get('cavalinho_model') if is_carreta else None,
                cavalinho_year=_int('cavalinho_year') if is_carreta else None,
                # Banking data
                bank_name=_get('bank_name'),
                agency=_get('agency'),
                account=_get('account'),
                pix_key=_get('pix_key'),
                created_by=current_user.id,
                **files
            )
            
            db.session.add(driver)
            db.session.commit()
            
            # Audit log
            audit = AuditLog(
                user_id=current_user.id,
                action='CREATE',
                table_name='drivers',
                record_id=driver.id,
                new_values=f"Motorista criado: {driver.name}"
            )
            db.session.add(audit)
            db.session.commit()
            
            flash('Motorista cadastrado com sucesso!', 'success')
            return redirect(url_for('drivers.index'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao cadastrar motorista: {str(e)}', 'error')
    
    return render_template('drivers/form.html')

@drivers_bp.route('/quick-add', methods=['POST'])
@login_required
def quick_add():
    """Create one or many drivers with just name + phone. Returns JSON."""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'}), 403

    data       = request.get_json(silent=True) or {}
    entries    = data.get('drivers', [])   # list of {name, phone}
    start_ema  = data.get('start_ema', False)

    if not entries:
        return jsonify({'error': 'Nenhum motorista informado'}), 400

    created   = []
    errors    = []
    ema_started = 0

    for item in entries:
        name  = (item.get('name') or '').strip()
        phone = (item.get('phone') or '').strip()
        if not name or not phone:
            errors.append(f"Linha inválida — nome e telefone obrigatórios: {item}")
            continue
        try:
            driver = Driver(
                name=name,
                phone=phone,
                created_by=current_user.id,
            )
            db.session.add(driver)
            db.session.flush()   # get driver.id
            created.append(driver)
        except Exception as exc:
            db.session.rollback()
            errors.append(f"{name}: {exc}")

    db.session.commit()

    # Optionally kick off EMA for each created driver
    if start_ema and created:
        from models import EmaSession
        from utils.ema_agent import (greeting_message, FIELD_DEFS,
                                      _is_field_applicable, _append_history)
        from utils.evolution_api import send_text
        for driver in created:
            applicable  = [f for f in FIELD_DEFS if _is_field_applicable(f, driver)]
            first_field = applicable[0]['key'] if applicable else None
            ema_session = EmaSession(
                driver_id     = driver.id,
                status        = 'active',
                revalidation  = True,
                current_field = first_field,
                history       = [],
                staged_data   = {},
                started_at    = datetime.utcnow(),
            )
            db.session.add(ema_session)
            db.session.flush()
            greeting = greeting_message(driver, revalidation=True)
            ok = send_text(driver.phone, greeting)
            if ok:
                _append_history(ema_session, 'ema', greeting)
                ema_started += 1
            else:
                ema_session.status    = 'error'
                ema_session.error_msg = 'Falha ao enviar saudação inicial'
        db.session.commit()

    return jsonify({
        'created':     len(created),
        'ema_started': ema_started,
        'errors':      errors,
        'driver_ids':  [d.id for d in created],
    })


@drivers_bp.route('/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit(id):
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('drivers.index'))
    
    driver = Driver.query.get_or_404(id)
    
    if request.method == 'POST':
        try:
            old_values = f"Nome: {driver.name}, CPF: {driver.cpf}"
            
            # Handle file uploads
            from utils.storage import save_file as _save
            for file_field in ['cnh_document', 'crlv_document', 'address_proof', 'vehicle_photo', 'cavalinho_crlv']:
                if file_field in request.files:
                    file = request.files[file_field]
                    if file and file.filename and allowed_file(file.filename):
                        filename = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
                        key = f"uploads/drivers/{filename}"
                        _save(file.stream, key)
                        setattr(driver, file_field, key)

            is_carreta = request.form.get('truck_type') == 'carreta'

            # Update driver data
            driver.name = request.form['name']
            driver.cpf = request.form['cpf']
            driver.rg = request.form['rg']
            driver.birth_date = datetime.strptime(request.form['birth_date'], '%Y-%m-%d').date()
            driver.phone = request.form['phone']
            driver.email = request.form.get('email')
            # Address fields
            driver.cep = request.form['cep']
            driver.street = request.form['street']
            driver.number = request.form['number']
            driver.complement = request.form.get('complement')
            driver.neighborhood = request.form['neighborhood']
            driver.city = request.form['city']
            driver.state = request.form['state']
            # CNH and vehicle data
            driver.cnh_expiry = datetime.strptime(request.form['cnh_expiry'], '%Y-%m-%d').date()
            driver.antt_number = request.form.get('antt_number')
            driver.truck_type = request.form['truck_type']
            driver.has_tracker = request.form.get('has_tracker') == '1'
            driver.tracker_type = request.form.get('tracker_type') if request.form.get('has_tracker') == '1' else None
            driver.vehicle_plate = request.form['vehicle_plate']
            driver.vehicle_model = request.form['vehicle_model']
            driver.vehicle_year = int(request.form['vehicle_year'])
            # Cavalinho (only for carreta type)
            driver.cavalinho_plate = request.form.get('cavalinho_plate') if is_carreta else None
            driver.cavalinho_model = request.form.get('cavalinho_model') if is_carreta else None
            driver.cavalinho_year = int(request.form['cavalinho_year']) if is_carreta and request.form.get('cavalinho_year') else None
            if not is_carreta:
                driver.cavalinho_crlv = None
            # Banking data
            driver.bank_name = request.form.get('bank_name')
            driver.agency = request.form.get('agency')
            driver.account = request.form.get('account')
            driver.pix_key = request.form.get('pix_key')
            
            db.session.commit()
            
            # Audit log
            audit = AuditLog(
                user_id=current_user.id,
                action='UPDATE',
                table_name='drivers',
                record_id=driver.id,
                old_values=old_values,
                new_values=f"Nome: {driver.name}, CPF: {driver.cpf}"
            )
            db.session.add(audit)
            db.session.commit()
            
            flash('Motorista atualizado com sucesso!', 'success')
            return redirect(url_for('drivers.index'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao atualizar motorista: {str(e)}', 'error')
    
    return render_template('drivers/form.html', driver=driver)

@drivers_bp.route('/<int:id>')
@login_required
def view(id):
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('freight.index'))
    from datetime import date
    driver = Driver.query.get_or_404(id)
    return render_template('drivers/view.html', driver=driver, today=date.today())

@drivers_bp.route('/<int:id>/toggle-status', methods=['POST'])
@login_required
def toggle_status(id):
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'message': 'Acesso negado'})
    
    driver = Driver.query.get_or_404(id)
    old_status = driver.active
    driver.active = not driver.active
    
    db.session.commit()
    
    # Audit log
    audit = AuditLog(
        user_id=current_user.id,
        action='UPDATE',
        table_name='drivers',
        record_id=driver.id,
        old_values=f"Status: {'Ativo' if old_status else 'Inativo'}",
        new_values=f"Status: {'Ativo' if driver.is_active else 'Inativo'}"
    )
    db.session.add(audit)
    db.session.commit()
    
    return jsonify({
        'success': True, 
        'new_status': driver.active,
        'message': f'Motorista {"ativado" if driver.active else "desativado"} com sucesso!'
    })

@drivers_bp.route('/<int:id>/delete', methods=['POST'])
@login_required
def delete(id):
    if current_user.role not in ['admin', 'operador']:
        flash('Acesso negado. Apenas administradores e operadores podem excluir motoristas.', 'error')
        return redirect(url_for('drivers.view', id=id))

    driver = Driver.query.get_or_404(id)

    # Block deletion if driver has any freights (active or historical)
    from models import Freight
    freight_count = Freight.query.filter_by(assigned_driver_id=driver.id).count()
    if freight_count > 0:
        flash(f'Não é possível excluir {driver.name}: possui {freight_count} frete(s) vinculado(s). '
              'Inative o motorista em vez de excluir.', 'error')
        return redirect(url_for('drivers.view', id=id))

    driver_name = driver.name
    try:
        # Delete related records that don't need history
        from models import EmaSession, Payment, WhatsAppMessage, DriverRating
        EmaSession.query.filter_by(driver_id=driver.id).delete()
        DriverRating.query.filter_by(driver_id=driver.id).delete()

        # Try to delete DriverBid if model exists
        try:
            from models import DriverBid
            DriverBid.query.filter_by(driver_id=driver.id).delete()
        except Exception:
            pass

        # Nullify FK references that keep financial history
        Payment.query.filter_by(driver_id=driver.id).update({'driver_id': None})
        WhatsAppMessage.query.filter_by(driver_id=driver.id).update({'driver_id': None})

        # Delete audit logs for this driver
        AuditLog.query.filter_by(table_name='drivers', record_id=driver.id).delete()

        db.session.delete(driver)

        # Log the deletion
        audit = AuditLog(
            user_id=current_user.id,
            action='DELETE',
            table_name='drivers',
            record_id=id,
            old_values=f"Nome: {driver_name}",
            new_values='Excluído'
        )
        db.session.add(audit)
        db.session.commit()

        flash(f'Motorista {driver_name} excluído com sucesso.', 'success')
    except Exception as e:
        db.session.rollback()
        logging.error(f'Erro ao excluir motorista {id}: {e}')
        flash(f'Erro ao excluir motorista: {str(e)}', 'error')
        return redirect(url_for('drivers.view', id=id))

    return redirect(url_for('drivers.index'))


@drivers_bp.route('/export')
@login_required
def export():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('drivers.index'))
    
    return export_drivers_excel()

@drivers_bp.route('/import', methods=['POST'])
@login_required
def import_drivers():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'message': 'Acesso negado'})
    
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': 'Nenhum arquivo selecionado'})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': 'Nenhum arquivo selecionado'})
    
    if file and file.filename.endswith(('.xlsx', '.xls')):
        try:
            result = import_drivers_excel(file, current_user.id)
            return jsonify(result)
        except Exception as e:
            return jsonify({'success': False, 'message': f'Erro ao importar: {str(e)}'})
    
    return jsonify({'success': False, 'message': 'Formato de arquivo inválido'})

@drivers_bp.route('/<int:driver_id>/download/<doc_type>')
@login_required
def download_document(driver_id, doc_type):
    """Download driver document — staff only"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        abort(403)
    driver = Driver.query.get_or_404(driver_id)
    
    # Map document types to model fields
    doc_fields = {
        'cnh': driver.cnh_document,
        'crlv': driver.crlv_document,
        'address': driver.address_proof,
        'vehicle_photo': driver.vehicle_photo,
        'cavalinho_crlv': driver.cavalinho_crlv,
    }
    
    if doc_type not in doc_fields:
        abort(404)
    
    file_path = doc_fields[doc_type]
    
    if not file_path:
        flash('Documento não encontrado.', 'error')
        return redirect(url_for('drivers.view', id=driver_id))

    try:
        from utils.storage import get_file as _get
        buf = _get(file_path)
        if buf is None:
            flash('Documento não encontrado no servidor.', 'error')
            return redirect(url_for('drivers.view', id=driver_id))
        filename_dl = os.path.basename(file_path)
        return send_file(buf, as_attachment=True, download_name=filename_dl)
    except Exception as e:
        flash(f'Erro ao baixar documento: {str(e)}', 'error')
        return redirect(url_for('drivers.view', id=driver_id))


@drivers_bp.route('/extract-document', methods=['POST'])
@login_required
def extract_document():
    """Gemini Vision — extrai dados de documentos do motorista automaticamente."""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'error': 'Acesso negado'}), 403

    doc_type = request.form.get('doc_type', '')
    if 'image' not in request.files:
        return jsonify({'error': 'Nenhum arquivo enviado'}), 400

    file = request.files['image']
    if not file or file.filename == '':
        return jsonify({'error': 'Arquivo inválido'}), 400

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'jpg'
    mime_map = {
        'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
        'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp',
        'pdf': 'application/pdf',
    }
    mime_type = mime_map.get(ext, 'image/jpeg')
    image_bytes = file.read()

    prompts = {
        'cnh': (
            "Analise esta CNH (Carteira Nacional de Habilitação) brasileira e extraia os dados.\n"
            "Retorne APENAS um JSON válido com estes campos (use null se não encontrar):\n"
            '{"name":"nome completo","cpf":"000.000.000-00","rg":"número RG",'
            '"birth_date":"YYYY-MM-DD","cnh_expiry":"YYYY-MM-DD"}\n'
            "Nenhum texto fora do JSON."
        ),
        'crlv': (
            "Analise este CRLV (documento do veículo) brasileiro e extraia os dados.\n"
            "Retorne APENAS um JSON válido com estes campos (use null se não encontrar):\n"
            '{"vehicle_plate":"placa ex ABC-1234","vehicle_model":"marca e modelo ex Volvo FH 460",'
            '"vehicle_year":2020,"antt_number":"RNTRC se houver"}\n'
            "Nenhum texto fora do JSON."
        ),
        'address_proof': (
            "Analise este comprovante de residência brasileiro (conta de água, luz, gás, telefone ou extrato bancário) "
            "e extraia o endereço do titular.\n"
            "Retorne APENAS um JSON válido com estes campos (use null se não encontrar):\n"
            '{"cep":"00000-000","street":"logradouro","number":"número",'
            '"complement":"complemento ou null","neighborhood":"bairro","city":"cidade","state":"UF"}\n'
            "Nenhum texto fora do JSON."
        ),
    }

    if doc_type not in prompts:
        return jsonify({'error': 'Tipo de documento inválido. Use: cnh, crlv ou address_proof'}), 400

    try:
        from google import genai
        from google.genai import types as gtypes
        import json as _json

        client = genai.Client(
            api_key=os.environ.get('AI_INTEGRATIONS_GEMINI_API_KEY'),
            http_options={
                'api_version': '',
                'base_url': os.environ.get('AI_INTEGRATIONS_GEMINI_BASE_URL')
            }
        )

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=gtypes.Content(
                role='user',
                parts=[
                    gtypes.Part(inline_data=gtypes.Blob(mime_type=mime_type, data=image_bytes)),
                    gtypes.Part(text=prompts[doc_type])
                ]
            )
        )

        raw = (response.text or '').strip()
        # Remove markdown code fences if present
        if raw.startswith('```'):
            raw = '\n'.join(raw.split('\n')[1:])
            raw = raw.rsplit('```', 1)[0].strip()

        data = _json.loads(raw)
        # Clean up null strings
        data = {k: (v if v not in ('null', 'None', '') else None) for k, v in data.items()}
        return jsonify({'success': True, 'data': data})

    except _json.JSONDecodeError:
        return jsonify({'error': 'Não foi possível extrair os dados. Tente com uma imagem mais nítida.'}), 422
    except Exception as e:
        logging.error(f'Extração de documento erro: {e}')
        return jsonify({'error': f'Erro ao processar documento: {str(e)}'}), 500
