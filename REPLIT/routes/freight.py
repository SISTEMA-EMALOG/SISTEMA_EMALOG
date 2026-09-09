from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import login_required, current_user
from models import Freight, Quote, Driver, Client, AuditLog, Payment, FreightStatusLog, DriverRating
from app import db
from sqlalchemy import and_
from sqlalchemy.orm import joinedload
from utils.whatsapp import send_freight_offer
from datetime import datetime
import uuid
import json
import logging
import os

freight_bp = Blueprint('freight', __name__, url_prefix='/freight')

@freight_bp.route('/')
@login_required
def index():
    search = request.args.get('search', '')
    status = request.args.get('status', 'all')
    client_id = request.args.get('client_id', '')
    page = request.args.get('page', 1, type=int)
    per_page = 20

    query = Freight.query.options(
        joinedload(Freight.client),
        joinedload(Freight.assigned_driver)
    )

    if current_user.role == 'cliente' and current_user.client_id:
        query = query.filter_by(client_id=current_user.client_id)
    elif client_id:
        query = query.filter_by(client_id=int(client_id))

    if search:
        query = query.filter(Freight.freight_number.contains(search))

    if status != 'all':
        query = query.filter_by(status=status)

    pagination = query.order_by(Freight.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    clients = Client.query.filter_by(active=True).order_by(Client.company_name).all()

    return render_template('freight/index.html',
                           freights=pagination.items,
                           pagination=pagination,
                           clients=clients,
                           search=search, status=status, selected_client=client_id)

@freight_bp.route('/new', methods=['GET', 'POST'])
@freight_bp.route('/new/<int:quote_id>', methods=['GET', 'POST'])
@login_required
def new(quote_id=None):
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('freight.index'))
    
    quote = None
    if quote_id:
        quote = Quote.query.get_or_404(quote_id)
        if quote.status != 'aprovado':
            flash('Apenas cotações aprovadas podem gerar fretes.', 'error')
            return redirect(url_for('quotes.view', id=quote_id))
        # Evita frete duplicado para a mesma cotação
        existing = Freight.query.filter_by(quote_id=quote_id).first()
        if existing:
            flash(f'Esta cotação já gerou o frete {existing.freight_number}.', 'warning')
            return redirect(url_for('freight.view', id=existing.id))
    
    if request.method == 'POST':
        try:
            # Generate freight number
            freight_count  = Freight.query.count() + 1
            freight_number = f"FRT-{datetime.now().strftime('%Y%m%d')}-{freight_count:04d}"
            
            freight = Freight(
                freight_number=freight_number,
                quote_id=quote.id if quote else None,
                client_id=int(request.form['client_id']),
                origin=request.form['origin'],
                destination=request.form['destination'],
                product=request.form['product'],
                weight=float(request.form['weight']),
                agreed_price=float(request.form['agreed_price']),
                pickup_date=datetime.strptime(request.form['pickup_date'], '%Y-%m-%d').date() if request.form.get('pickup_date') else None,
                delivery_date=datetime.strptime(request.form['delivery_date'], '%Y-%m-%d').date() if request.form.get('delivery_date') else None,
                notes=request.form.get('notes'),
                created_by=current_user.id
            )
            
            db.session.add(freight)
            db.session.commit()
            
            # Audit log
            audit = AuditLog(
                user_id=current_user.id,
                action='CREATE',
                table_name='freights',
                record_id=freight.id,
                new_values=f"Frete criado: {freight.freight_number}"
            )
            db.session.add(audit)
            db.session.commit()
            
            flash('Frete criado com sucesso!', 'success')
            return redirect(url_for('freight.view', id=freight.id))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao criar frete: {str(e)}', 'error')
    
    clients = Client.query.filter_by(is_active=True).order_by(Client.company_name).all()
    drivers = Driver.query.filter_by(is_active=True).order_by(Driver.name).all()
    
    return render_template('freight/form.html', quote=quote, clients=clients, drivers=drivers)

@freight_bp.route('/<int:id>')
@login_required
def view(id):
    freight = Freight.query.get_or_404(id)
    
    # Check access permissions
    if current_user.role == 'cliente' and freight.client_id != current_user.client_id:
        flash('Acesso negado.', 'error')
        return redirect(url_for('freight.index'))
    
    # Parse selected drivers and responses
    try:
        freight.parsed_selected_drivers = json.loads(freight.selected_drivers) if freight.selected_drivers else []
        freight.parsed_whatsapp_responses = json.loads(freight.whatsapp_responses) if freight.whatsapp_responses else []
    except:
        freight.parsed_selected_drivers = []
        freight.parsed_whatsapp_responses = []
    
    # Get selected drivers data
    selected_drivers = []
    if freight.parsed_selected_drivers:
        selected_drivers = Driver.query.filter(Driver.id.in_(freight.parsed_selected_drivers)).all()
    
    status_logs = FreightStatusLog.query.filter_by(freight_id=freight.id)\
        .order_by(FreightStatusLog.created_at.asc()).all()

    existing_rating = None
    if freight.assigned_driver_id:
        existing_rating = DriverRating.query.filter_by(
            freight_id=freight.id,
            driver_id=freight.assigned_driver_id
        ).first()

    from models import FreightDocument
    cte_documents = FreightDocument.query.filter_by(
        freight_id=freight.id,
        document_type='cte'
    ).order_by(FreightDocument.uploaded_at.asc()).all()

    nf_documents = FreightDocument.query.filter_by(
        freight_id=freight.id,
        document_type='nf_assinada'
    ).order_by(FreightDocument.uploaded_at.asc()).all()

    return render_template('freight/view.html', freight=freight,
                           selected_drivers=selected_drivers,
                           status_logs=status_logs,
                           existing_rating=existing_rating,
                           cte_documents=cte_documents,
                           nf_documents=nf_documents)

@freight_bp.route('/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit(id):
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('freight.index'))
    
    freight = Freight.query.get_or_404(id)
    
    if freight.status in ['entregue', 'cancelado']:
        flash('Fretes entregues ou cancelados não podem ser editados.', 'error')
        return redirect(url_for('freight.view', id=id))
    
    if request.method == 'POST':
        try:
            old_values = f"Origem: {freight.origin}, Destino: {freight.destination}, Preço: {freight.agreed_price}"
            
            # Update freight
            freight.client_id = int(request.form['client_id'])
            freight.origin = request.form['origin']
            freight.destination = request.form['destination']
            freight.product = request.form['product']
            freight.weight = float(request.form['weight'])
            freight.agreed_price = float(request.form['agreed_price'])
            freight.pickup_date = datetime.strptime(request.form['pickup_date'], '%Y-%m-%d').date() if request.form.get('pickup_date') else None
            freight.delivery_date = datetime.strptime(request.form['delivery_date'], '%Y-%m-%d').date() if request.form.get('delivery_date') else None
            freight.notes = request.form.get('notes')
            
            db.session.commit()
            
            # Audit log
            audit = AuditLog(
                user_id=current_user.id,
                action='UPDATE',
                table_name='freights',
                record_id=freight.id,
                old_values=old_values,
                new_values=f"Origem: {freight.origin}, Destino: {freight.destination}, Preço: {freight.agreed_price}"
            )
            db.session.add(audit)
            db.session.commit()
            
            flash('Frete atualizado com sucesso!', 'success')
            return redirect(url_for('freight.view', id=freight.id))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao atualizar frete: {str(e)}', 'error')
    
    clients = Client.query.filter_by(is_active=True).order_by(Client.company_name).all()
    drivers = Driver.query.filter(
        Driver.active.is_(True),
        Driver.is_active.is_(True),
        Driver.availability_status == 'disponivel'
    ).order_by(Driver.name).all()
    return render_template(
        'freight/form.html',
        freight=freight,
        clients=clients,
        drivers=drivers
    )

@freight_bp.route('/<int:id>/select-drivers', methods=['GET', 'POST'])
@login_required
def select_drivers(id):
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('freight.index'))
    
    freight = Freight.query.get_or_404(id)
    
    if freight.status != 'ofertado':
        flash('Apenas fretes ofertados podem ter motoristas selecionados.', 'error')
        return redirect(url_for('freight.view', id=id))
    
    if request.method == 'POST':
        try:
            selected_driver_ids = request.form.getlist('selected_drivers')
            
            if not selected_driver_ids:
                flash('Selecione pelo menos um motorista.', 'error')
                drivers = Driver.query.filter_by(is_active=True).order_by(Driver.name).all()
                return render_template('freight/select_drivers.html', freight=freight, drivers=drivers)
            
            # Save selected drivers
            freight.selected_drivers = json.dumps([int(id) for id in selected_driver_ids])
            db.session.commit()
            
            # Buscar todos os motoristas selecionados de uma só vez (evita N+1)
            driver_ids_int = [int(did) for did in selected_driver_ids]
            selected_drivers_map = {
                d.id: d for d in Driver.query.filter(Driver.id.in_(driver_ids_int)).all()
            }

            # Send WhatsApp offers
            sent_count = 0
            for did in driver_ids_int:
                driver = selected_drivers_map.get(did)
                if driver and driver.phone:
                    try:
                        send_freight_offer(freight, driver)
                        sent_count += 1
                    except Exception as e:
                        flash(f'Erro ao enviar WhatsApp para {driver.name}: {str(e)}', 'warning')
            
            freight.whatsapp_sent = True
            db.session.commit()
            
            # Audit log
            audit = AuditLog(
                user_id=current_user.id,
                action='UPDATE',
                table_name='freights',
                record_id=freight.id,
                old_values="Motoristas: não selecionados",
                new_values=f"Motoristas selecionados: {len(driver_ids_int)}, WhatsApp enviado: {sent_count}"
            )
            db.session.add(audit)
            db.session.commit()
            
            drivers_list = [d.name for d in selected_drivers_map.values()]
            
            flash(f'Ofertas WhatsApp enviadas para {sent_count} motoristas: {", ".join(drivers_list)}', 'success')
            return redirect(url_for('freight.sent_offers', id=freight.id))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao selecionar motoristas: {str(e)}', 'error')
    
    drivers = Driver.query.filter_by(is_active=True).order_by(Driver.name).all()
    
    # Parse already selected drivers
    try:
        selected_ids = json.loads(freight.selected_drivers) if freight.selected_drivers else []
    except:
        selected_ids = []
    
    # Generate message preview with sample driver
    sample_driver = drivers[0] if drivers else None
    if sample_driver:
        from utils.whatsapp import generate_freight_message
        message_preview = generate_freight_message(freight, sample_driver)
    else:
        message_preview = "Nenhum motorista encontrado para preview da mensagem."
    
    return render_template('freight/select_drivers.html', 
                         freight=freight, 
                         drivers=drivers, 
                         selected_ids=selected_ids,
                         message_preview=message_preview)



@freight_bp.route('/<int:id>/update-status', methods=['POST'])
@login_required
def update_status(id):
    try:
        if current_user.role not in ['admin', 'operador', 'vendedor']:
            return jsonify({'success': False, 'message': 'Acesso negado'}), 403
        
        freight = Freight.query.get_or_404(id)
        
        # Get status from request
        if request.is_json:
            new_status = request.json.get('status')
        else:
            new_status = request.form.get('status')
        
        if not new_status:
            return jsonify({'success': False, 'message': 'Status não informado'}), 400
        
        valid_statuses = ['ofertado', 'aceito', 'em_coleta', 'em_transito', 'entregue', 'cancelado']
        if new_status not in valid_statuses:
            return jsonify({'success': False, 'message': 'Status inválido'}), 400
        
        old_status = freight.status
        freight.status = new_status

        # Registrar log de status
        status_log = FreightStatusLog(
            freight_id=freight.id,
            old_status=old_status,
            new_status=new_status,
            changed_by=current_user.id
        )
        db.session.add(status_log)

        # Atualizar disponibilidade do motorista
        if freight.assigned_driver_id:
            driver = Driver.query.get(freight.assigned_driver_id)
            if driver:
                if new_status in ('entregue', 'cancelado'):
                    driver.availability_status = 'disponivel'
                elif new_status == 'aceito':
                    driver.availability_status = 'em_frete'

        # Se o status mudou para 'aceito', atualizar pagamentos pendentes
        if new_status == 'aceito' and freight.assigned_driver_id:
            # Atualizar pagamentos pendentes sem motorista
            pending_payments = Payment.query.filter(
                and_(
                    Payment.freight_id == freight.id,
                    Payment.driver_id.is_(None),
                    Payment.status == 'pendente'
                )
            ).all()
            
            for payment in pending_payments:
                payment.driver_id = freight.assigned_driver_id

        # Se frete foi cancelado, marcar cotação vinculada como negada
        if new_status == 'cancelado' and freight.quote_id:
            from models import Quote as _Quote
            linked_quote = _Quote.query.get(freight.quote_id)
            if linked_quote and linked_quote.status not in ('aprovada', 'aprovado_cliente', 'aprovado', 'negado', 'rejeitada'):
                linked_quote.status = 'negado'
                linked_quote.rejection_reason = 'Frete cancelado pela equipe operacional'
                linked_quote.rejected_at = datetime.utcnow()
                linked_quote.rejected_by = current_user.id
        
        # Save to database
        db.session.commit()
        
        # Create audit log
        audit = AuditLog(
            user_id=current_user.id,
            action='UPDATE',
            table_name='freights',
            record_id=freight.id,
            old_values=f"Status: {old_status}",
            new_values=f"Status: {new_status}"
        )
        db.session.add(audit)
        db.session.commit()
        
        status_names = {
            'ofertado': 'Ofertado',
            'aceito': 'Aceito',
            'em_coleta': 'Em Coleta',
            'em_transito': 'Em Trânsito',
            'entregue': 'Entregue',
            'cancelado': 'Cancelado'
        }
        
        # Enviar notificação para cliente sobre mudança de status
        from routes.notifications import notify_freight_status_update
        notify_freight_status_update(freight, old_status, new_status)
        
        # Se finalizado, enviar email para cliente
        if new_status == 'entregue':
            try:
                from utils.email_service import send_freight_completed_email
                documents = freight.documents if hasattr(freight, 'documents') else []
                nf_files = []
                for doc in documents:
                    if doc.document_type == 'nf_assinada':
                        nf_files.append({
                            'filename': doc.original_filename,
                            'path': doc.file_path
                        })
                send_freight_completed_email(freight, nf_files)
            except Exception as e:
                logging.error(f"Erro ao enviar email de frete finalizado: {e}")

        return jsonify({
            'success': True, 
            'message': f'Status atualizado para {status_names.get(new_status, new_status)}!'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Erro interno: {str(e)}'}), 500

@freight_bp.route('/<int:id>/finalize-with-nf', methods=['POST'])
@login_required
def finalize_with_nf(id):
    """Finaliza frete com upload obrigatório de NF assinada"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'message': 'Acesso negado'}), 403
    
    freight = Freight.query.get_or_404(id)
    already_delivered = (freight.status == 'entregue')
    
    try:
        # Verificar se arquivos foram enviados
        if 'nf_files' not in request.files:
            return jsonify({'success': False, 'message': 'Nenhum arquivo de NF foi enviado'}), 400
        
        files = request.files.getlist('nf_files')
        if not files or len(files) == 0:
            return jsonify({'success': False, 'message': 'É obrigatório enviar pelo menos uma NF assinada'}), 400
        
        # Importar FreightDocument
        from models import FreightDocument
        import uuid
        import os
        from werkzeug.utils import secure_filename
        
        uploaded_documents = []
        from utils.storage import save_file as _save

        # Processar cada arquivo — lê os bytes explicitamente antes de salvar
        for file in files:
            if file.filename != '':
                # Validar extensão
                if not allowed_file(file.filename):
                    return jsonify({'success': False, 'message': f'Arquivo {file.filename} não é um formato válido'}), 400
                
                # Gerar nome único
                filename = secure_filename(file.filename)
                unique_filename = f"{uuid.uuid4().hex}_{filename}"
                key = f"uploads/nf_assinadas/{unique_filename}"

                # Ler bytes explicitamente (garante que o ponteiro está no início)
                file_bytes = file.read()
                if not file_bytes:
                    return jsonify({'success': False, 'message': f'Arquivo {file.filename} está vazio'}), 400

                # Salvar no Object Storage (ou disco local em dev)
                _save(file_bytes, key)

                # Criar registro no banco
                document = FreightDocument(
                    freight_id=freight.id,
                    document_type='nf_assinada',
                    filename=unique_filename,
                    original_filename=filename,
                    file_path=key,
                    file_size=len(file_bytes),
                    uploaded_by=current_user.id
                )
                
                db.session.add(document)
                uploaded_documents.append(document)

        if not uploaded_documents:
            return jsonify({'success': False, 'message': 'Nenhum arquivo válido foi enviado'}), 400

        if not already_delivered:
            # Primeira finalização: atualizar status e liberar motorista
            old_status = freight.status
            freight.status = 'entregue'
            freight.delivery_date = datetime.now().date()

            finalize_notes_val = request.form.get('finalize_notes', '').strip()
            status_log = FreightStatusLog(
                freight_id=freight.id,
                old_status=old_status,
                new_status='entregue',
                notes=f'Finalizado com {len(uploaded_documents)} NF(s) anexada(s). {finalize_notes_val}',
                changed_by=current_user.id
            )
            db.session.add(status_log)

            if freight.assigned_driver_id:
                drv = Driver.query.get(freight.assigned_driver_id)
                if drv:
                    drv.availability_status = 'disponivel'

            finalize_notes = request.form.get('finalize_notes', '').strip()
            if finalize_notes:
                current_notes = freight.notes or ''
                freight.notes = f"{current_notes}\n\nFinalização: {finalize_notes}".strip()
        else:
            old_status = 'entregue'

        db.session.commit()
        
        # Log de auditoria
        audit = AuditLog(
            user_id=current_user.id,
            action='UPDATE',
            table_name='freights',
            record_id=freight.id,
            old_values=f"Status: {old_status}",
            new_values=f"{'Status: entregue' if not already_delivered else 'Docs adicionados'} - {len(uploaded_documents)} NF(s)"
        )
        db.session.add(audit)
        db.session.commit()

        if not already_delivered:
            # Enviar notificação e email apenas na primeira finalização
            from routes.notifications import notify_freight_status_update
            notify_freight_status_update(freight, old_status, 'entregue')
            try:
                from utils.email_service import send_freight_completed_email
                nf_files = [{'filename': d.original_filename, 'path': d.file_path} for d in uploaded_documents]
                send_freight_completed_email(freight, nf_files)
            except Exception as e:
                logging.error(f"Erro ao enviar email de finalização: {e}")

        msg = (f'Frete finalizado! {len(uploaded_documents)} documento(s) anexado(s).'
               if not already_delivered
               else f'{len(uploaded_documents)} comprovante(s) adicionado(s) com sucesso.')
        return jsonify({'success': True, 'message': msg})
        
    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao finalizar frete com NF: {e}")
        return jsonify({'success': False, 'message': f'Erro ao finalizar frete: {str(e)}'}), 500

def allowed_file(filename):
    """Verifica se a extensão do arquivo é permitida"""
    allowed_extensions = {'pdf', 'png', 'jpg', 'jpeg'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions


@freight_bp.route('/<int:id>/upload-cte', methods=['POST'])
@login_required
def upload_cte(id):
    """Upload do CT-e — disponível para operador/admin após frete aceito"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'message': 'Acesso negado'}), 403

    freight = Freight.query.get_or_404(id)

    if freight.status not in ['aceito', 'em_transito', 'entregue']:
        return jsonify({'success': False, 'message': 'CT-e só pode ser anexado após o frete ser aceito pelo motorista'}), 400

    if 'cte_file' not in request.files:
        return jsonify({'success': False, 'message': 'Nenhum arquivo enviado'}), 400

    file = request.files['cte_file']
    if not file or file.filename == '':
        return jsonify({'success': False, 'message': 'Arquivo inválido'}), 400

    if not allowed_file(file.filename):
        return jsonify({'success': False, 'message': 'Formato inválido. Use PDF, PNG, JPG ou JPEG'}), 400

    try:
        from models import FreightDocument
        from werkzeug.utils import secure_filename

        from utils.storage import save_file as _save

        filename = secure_filename(file.filename)
        unique_filename = f"{uuid.uuid4().hex}_{filename}"
        key = f"uploads/cte/{unique_filename}"

        # Ler bytes explicitamente para garantir que o ponteiro está no início
        file_bytes = file.read()
        if not file_bytes:
            return jsonify({'success': False, 'message': 'Arquivo vazio'}), 400
        _save(file_bytes, key)

        doc = FreightDocument(
            freight_id=freight.id,
            document_type='cte',
            filename=unique_filename,
            original_filename=filename,
            file_path=key,
            file_size=len(file_bytes),
            uploaded_by=current_user.id
        )
        db.session.add(doc)

        audit = AuditLog(
            user_id=current_user.id,
            action='CREATE',
            table_name='freight_documents',
            record_id=freight.id,
            new_values=f'CT-e anexado: {filename}'
        )
        db.session.add(audit)
        db.session.commit()

        return jsonify({
            'success': True,
            'message': 'CT-e anexado com sucesso!',
            'doc': {
                'id': doc.id,
                'original_filename': doc.original_filename,
                'file_size': doc.file_size,
                'uploaded_at': doc.uploaded_at.strftime('%d/%m/%Y %H:%M')
            }
        })
    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao fazer upload do CT-e: {e}")
        return jsonify({'success': False, 'message': f'Erro ao salvar arquivo: {str(e)}'}), 500


@freight_bp.route('/<int:id>/upload-nf', methods=['POST'])
@login_required
def upload_nf(id):
    """Adiciona comprovante de entrega (NF assinada) — disponível após frete entregue"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'message': 'Acesso negado'}), 403

    freight = Freight.query.get_or_404(id)

    if freight.status != 'entregue':
        return jsonify({'success': False, 'message': 'Comprovante só pode ser adicionado após o frete ser entregue'}), 400

    if 'nf_file' not in request.files:
        return jsonify({'success': False, 'message': 'Nenhum arquivo enviado'}), 400

    file = request.files['nf_file']
    if not file or file.filename == '':
        return jsonify({'success': False, 'message': 'Arquivo inválido'}), 400

    if not allowed_file(file.filename):
        return jsonify({'success': False, 'message': 'Formato inválido. Use PDF, PNG, JPG ou JPEG'}), 400

    try:
        from models import FreightDocument
        from werkzeug.utils import secure_filename
        from utils.storage import save_file as _save

        filename = secure_filename(file.filename)
        unique_filename = f"{uuid.uuid4().hex}_{filename}"
        key = f"uploads/nf_assinadas/{unique_filename}"

        file_bytes = file.read()
        if not file_bytes:
            return jsonify({'success': False, 'message': 'Arquivo vazio'}), 400
        _save(file_bytes, key)

        doc = FreightDocument(
            freight_id=freight.id,
            document_type='nf_assinada',
            filename=unique_filename,
            original_filename=filename,
            file_path=key,
            file_size=len(file_bytes),
            uploaded_by=current_user.id
        )
        db.session.add(doc)

        audit = AuditLog(
            user_id=current_user.id,
            action='CREATE',
            table_name='freight_documents',
            record_id=freight.id,
            new_values=f'Comprovante de entrega adicionado: {filename}'
        )
        db.session.add(audit)
        db.session.commit()

        return jsonify({
            'success': True,
            'message': 'Comprovante adicionado com sucesso!',
            'doc': {
                'id': doc.id,
                'original_filename': doc.original_filename,
                'file_size': doc.file_size,
                'uploaded_at': doc.uploaded_at.strftime('%d/%m/%Y %H:%M')
            }
        })
    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao fazer upload de NF: {e}")
        return jsonify({'success': False, 'message': f'Erro ao salvar arquivo: {str(e)}'}), 500


@freight_bp.route('/document/<int:doc_id>/download')
@login_required
def download_document(doc_id):
    """Download de documento do frete — staff sempre, cliente baixa CT-e e NF do próprio frete entregue"""
    from models import FreightDocument
    from flask import send_file

    doc = FreightDocument.query.get_or_404(doc_id)
    freight = Freight.query.get_or_404(doc.freight_id)

    # Clientes: só documentos do próprio frete e tipos permitidos
    if current_user.role == 'cliente':
        if freight.client_id != current_user.client_id:
            return jsonify({'error': 'Acesso negado'}), 403
        # CT-e disponível em qualquer status; NF assinada só após entregue
        if doc.document_type == 'nf_assinada' and freight.status != 'entregue':
            return jsonify({'error': 'Acesso negado'}), 403
        if doc.document_type not in ('cte', 'nf_assinada'):
            return jsonify({'error': 'Acesso negado'}), 403

    from utils.storage import get_file as _get
    buf = _get(doc.file_path)
    if buf is None:
        return jsonify({'error': 'Arquivo não encontrado no servidor'}), 404

    return send_file(
        buf,
        as_attachment=True,
        download_name=doc.original_filename
    )

@freight_bp.route('/<int:id>/save-message', methods=['POST'])
@login_required
def save_message(id):
    """AJAX endpoint to save custom WhatsApp message"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'message': 'Acesso negado'})
    
    freight = Freight.query.get_or_404(id)
    custom_message = request.json.get('message', '').strip()
    
    # Allow empty custom message to reset to default
    freight.custom_whatsapp_message = custom_message if custom_message else None
    
    try:
        db.session.commit()
        
        # Generate new preview with sample driver
        drivers = Driver.query.filter_by(is_active=True).first()
        if drivers:
            from utils.whatsapp import generate_freight_message
            new_preview = generate_freight_message(freight, drivers)
        else:
            new_preview = "Nenhum motorista encontrado para preview."
        
        return jsonify({
            'success': True, 
            'message': 'Mensagem salva com sucesso!',
            'preview': new_preview
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Erro ao salvar: {str(e)}'})

@freight_bp.route('/<int:id>/rate-driver', methods=['POST'])
@login_required
def rate_driver(id):
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'message': 'Acesso negado'}), 403

    freight = Freight.query.get_or_404(id)
    if not freight.assigned_driver_id:
        return jsonify({'success': False, 'message': 'Frete sem motorista atribuído'}), 400

    existing = DriverRating.query.filter_by(
        freight_id=freight.id, driver_id=freight.assigned_driver_id
    ).first()
    if existing:
        return jsonify({'success': False, 'message': 'Motorista já foi avaliado neste frete'}), 400

    try:
        rating = DriverRating(
            freight_id=freight.id,
            driver_id=freight.assigned_driver_id,
            rating=int(request.form.get('rating', 5)),
            punctuality=int(request.form.get('punctuality', 5)) or None,
            cargo_care=int(request.form.get('cargo_care', 5)) or None,
            communication=int(request.form.get('communication', 5)) or None,
            comment=request.form.get('comment', '').strip() or None,
            rated_by=current_user.id
        )
        db.session.add(rating)
        db.session.commit()
        flash('Avaliação registrada com sucesso!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao registrar avaliação: {str(e)}', 'error')

    return redirect(url_for('freight.view', id=id))


@freight_bp.route('/<int:id>/edit-message', methods=['GET', 'POST'])
@login_required 
def edit_message(id):
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('freight.index'))
    
    freight = Freight.query.get_or_404(id)
    
    if request.method == 'POST':
        custom_message = request.form.get('custom_message', '').strip()
        
        # Allow empty custom message to reset to default
        freight.custom_whatsapp_message = custom_message if custom_message else None
        
        try:
            db.session.commit()
            if custom_message:
                flash('Mensagem personalizada salva! Use "Selecionar Motoristas" para enviar.', 'success')
            else:
                flash('Mensagem resetada para o padrão.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao salvar mensagem: {str(e)}', 'error')
        return redirect(url_for('freight.view', id=freight.id))
    
    # Generate preview message
    from utils.whatsapp import generate_freight_message
    driver = Driver.query.first()  # Sample driver for preview
    if driver:
        preview_message = generate_freight_message(freight, driver)
    else:
        preview_message = "Nenhum motorista encontrado para preview."
    
    return render_template('freight/edit_message.html', freight=freight, preview_message=preview_message)

@freight_bp.route('/<int:id>/sent-offers')
@login_required
def sent_offers(id):
    """Show drivers who received WhatsApp offers"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('freight.index'))
    
    freight = Freight.query.get_or_404(id)
    
    # Get drivers who received offers
    sent_drivers = []
    if freight.selected_drivers:
        try:
            selected_ids = json.loads(freight.selected_drivers)
            sent_drivers = Driver.query.filter(Driver.id.in_(selected_ids)).all()
        except:
            sent_drivers = []
    
    return render_template('freight/sent_offers.html', freight=freight, sent_drivers=sent_drivers)

@freight_bp.route('/<int:freight_id>/available-drivers')
@login_required
def available_drivers(freight_id):
    """Retorna lista de motoristas ativos para seleção via AJAX"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'message': 'Acesso negado.'}), 403
    freight = Freight.query.get_or_404(freight_id)
    drivers = Driver.query.filter_by(is_active=True).order_by(Driver.name).all()
    return jsonify({'success': True, 'drivers': [
        {
            'id': d.id,
            'name': d.name,
            'phone': d.phone or '',
            'truck_type': d.truck_type or '',
            'vehicle_plate': d.vehicle_plate or '',
            'availability_status': d.availability_status or 'disponivel',
            'is_current': d.id == freight.assigned_driver_id,
        }
        for d in drivers
    ]})


@freight_bp.route('/<int:freight_id>/reassign-driver/<int:driver_id>', methods=['POST'])
@login_required
def reassign_driver(freight_id, driver_id):
    """Trocar motorista de um frete já atribuído"""
    try:
        if current_user.role not in ['admin', 'operador', 'vendedor']:
            return jsonify({'success': False, 'message': 'Acesso negado.'}), 403

        freight = Freight.query.get_or_404(freight_id)
        new_driver = Driver.query.get_or_404(driver_id)

        if freight.status in ['entregue', 'cancelado']:
            return jsonify({'success': False, 'message': 'Frete já finalizado, não é possível trocar o motorista.'}), 400

        if not freight.assigned_driver_id:
            return jsonify({'success': False, 'message': 'Frete ainda não tem motorista atribuído. Use a atribuição normal.'}), 400

        if freight.assigned_driver_id == driver_id:
            return jsonify({'success': False, 'message': 'Esse motorista já está atribuído a este frete.'}), 400

        data = request.get_json(silent=True) or {}
        driver_cost_input = data.get('driver_cost')

        if driver_cost_input:
            try:
                new_payment_value = float(str(driver_cost_input).replace(',', '.'))
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': 'Valor do motorista inválido.'}), 400
        else:
            new_payment_value = freight.driver_cost or 0

        old_driver = Driver.query.get(freight.assigned_driver_id)
        old_driver_name = old_driver.name if old_driver else f'ID {freight.assigned_driver_id}'

        # Cancelar pagamentos pendentes do motorista anterior
        cancelled = Payment.query.filter(
            and_(
                Payment.freight_id == freight_id,
                Payment.driver_id == freight.assigned_driver_id,
                Payment.status == 'pendente'
            )
        ).all()
        for p in cancelled:
            p.status = 'cancelado'

        # Liberar motorista anterior
        if old_driver:
            old_driver.availability_status = 'disponivel'

        # Atribuir novo motorista
        freight.assigned_driver_id = driver_id
        freight.driver_cost = new_payment_value
        new_driver.availability_status = 'em_frete'

        # Log de status
        swap_log = FreightStatusLog(
            freight_id=freight_id,
            old_status=freight.status,
            new_status=freight.status,
            notes=f'Motorista trocado: {old_driver_name} → {new_driver.name}. Novo custo: R$ {new_payment_value:.2f}',
            changed_by=current_user.id
        )
        db.session.add(swap_log)

        # Criar novos pagamentos 70/30 para o novo motorista
        today = datetime.now().date()
        pickup = freight.pickup_date or today
        delivery = freight.delivery_date or pickup
        value_70 = round(new_payment_value * 0.70, 2)
        value_30 = round(new_payment_value - value_70, 2)

        db.session.add(Payment(
            driver_id=driver_id,
            freight_id=freight_id,
            payment_type='carregamento_70',
            amount=value_70,
            description=f'70% carregamento — Frete {freight.freight_number} (novo motorista)',
            status='pendente',
            milestone='carregamento',
            due_date=pickup,
            payment_date=pickup,
            created_by=current_user.id
        ))
        db.session.add(Payment(
            driver_id=driver_id,
            freight_id=freight_id,
            payment_type='finalizacao_30',
            amount=value_30,
            description=f'30% finalização — Frete {freight.freight_number} (novo motorista)',
            status='pendente',
            milestone='finalizacao',
            due_date=delivery,
            payment_date=delivery,
            created_by=current_user.id
        ))

        db.session.add(AuditLog(
            user_id=current_user.id,
            action='UPDATE',
            table_name='freights',
            record_id=freight.id,
            old_values=f'Motorista: {old_driver_name}',
            new_values=f'Motorista trocado para: {new_driver.name} (ID: {new_driver.id}). Custo: R$ {new_payment_value:.2f}'
        ))
        db.session.commit()

        return jsonify({
            'success': True,
            'message': (
                f'Motorista alterado para {new_driver.name}! '
                f'Pagamentos anteriores cancelados. Novos pagamentos: '
                f'R$ {value_70:.2f} (70%) + R$ {value_30:.2f} (30%).'
            )
        })

    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao trocar motorista: {str(e)}")
        return jsonify({'success': False, 'message': f'Erro ao trocar motorista: {str(e)}'}), 500


@freight_bp.route('/<int:freight_id>/assign-to-driver/<int:driver_id>', methods=['POST'])
@login_required
def assign_to_driver(freight_id, driver_id):
    """Assign a driver to a freight and create automatic 70%/30% payment schedule"""
    try:
        if current_user.role not in ['admin', 'operador', 'vendedor']:
            return jsonify({'success': False, 'message': 'Acesso negado.'}), 403

        freight = Freight.query.get_or_404(freight_id)
        driver = Driver.query.get_or_404(driver_id)

        if freight.assigned_driver_id:
            return jsonify({'success': False, 'message': 'Frete já está atribuído a outro motorista.'}), 400

        # Accept driver_cost from request body (operator enters the agreed value)
        data = request.get_json(silent=True) or {}
        driver_cost_input = data.get('driver_cost')

        if driver_cost_input:
            try:
                payment_value = float(str(driver_cost_input).replace(',', '.'))
            except (ValueError, TypeError):
                return jsonify({'success': False, 'message': 'Valor do motorista inválido.'}), 400
        elif freight.driver_cost:
            payment_value = freight.driver_cost
        elif freight.quote_id:
            from models import Quote
            q = Quote.query.get(freight.quote_id)
            payment_value = q.driver_cost if q and q.driver_cost else freight.agreed_price * 0.8
        else:
            payment_value = freight.agreed_price * 0.8

        # Update freight
        freight.assigned_driver_id = driver_id
        freight.driver_cost = payment_value
        freight.status = 'aceito'

        # Marcar motorista como em frete
        driver.availability_status = 'em_frete'

        # Log de status
        assign_log = FreightStatusLog(
            freight_id=freight_id,
            old_status='ofertado',
            new_status='aceito',
            notes=f'Motorista {driver.name} atribuído. Custo: R$ {payment_value:.2f}',
            changed_by=current_user.id
        )
        db.session.add(assign_log)

        today = datetime.now().date()
        pickup = freight.pickup_date or today
        delivery = freight.delivery_date or pickup

        value_70 = round(payment_value * 0.70, 2)
        value_30 = round(payment_value - value_70, 2)

        # Payment 1: 70% on loading (due at pickup)
        payment_70 = Payment(
            driver_id=driver_id,
            freight_id=freight_id,
            payment_type='carregamento_70',
            amount=value_70,
            description=f'70% carregamento — Frete {freight.freight_number}',
            status='pendente',
            milestone='carregamento',
            due_date=pickup,
            payment_date=pickup,
            created_by=current_user.id
        )

        # Payment 2: 30% on delivery (due at delivery)
        payment_30 = Payment(
            driver_id=driver_id,
            freight_id=freight_id,
            payment_type='finalizacao_30',
            amount=value_30,
            description=f'30% finalização — Frete {freight.freight_number}',
            status='pendente',
            milestone='finalizacao',
            due_date=delivery,
            payment_date=delivery,
            created_by=current_user.id
        )

        db.session.add(payment_70)
        db.session.add(payment_30)

        audit = AuditLog(
            user_id=current_user.id,
            action='UPDATE',
            table_name='freights',
            record_id=freight.id,
            old_values='Motorista: não atribuído',
            new_values=(
                f'Motorista: {driver.name} (ID: {driver.id}). '
                f'Pagamentos criados: 70% R$ {value_70:.2f} (carregamento) + '
                f'30% R$ {value_30:.2f} (finalização)'
            )
        )
        db.session.add(audit)
        db.session.commit()

        return jsonify({
            'success': True,
            'message': (
                f'Motorista {driver.name} atribuído! '
                f'Pagamentos criados: R$ {value_70:.2f} (70% carregamento) + '
                f'R$ {value_30:.2f} (30% finalização).'
            )
        })

    except Exception as e:
        db.session.rollback()
        logging.error(f"Erro ao atribuir motorista: {str(e)}")
        return jsonify({'success': False, 'message': f'Erro ao atribuir frete: {str(e)}'}), 500
