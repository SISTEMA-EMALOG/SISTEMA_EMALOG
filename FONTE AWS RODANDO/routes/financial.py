from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from models import Payment, Driver, Freight, AuditLog
from app import db
from sqlalchemy import func, and_
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta

financial_bp = Blueprint('financial', __name__, url_prefix='/financial')

@financial_bp.route('/')
@login_required
def index():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('dashboard.index'))
    
    # Date filters - show full current month by default (1st to last day)
    import calendar as _cal
    _now = datetime.now()
    _last_day = _cal.monthrange(_now.year, _now.month)[1]
    start_date = request.args.get('start_date', _now.replace(day=1).strftime('%Y-%m-%d'))
    end_date = request.args.get('end_date', _now.replace(day=_last_day).strftime('%Y-%m-%d'))
    driver_id = request.args.get('driver_id', '')

    # Convert dates
    start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
    end_dt = datetime.strptime(end_date, '%Y-%m-%d').date()

    # Build query — match on due_date so future pending payments are always visible
    query = Payment.query.options(
        joinedload(Payment.driver),
        joinedload(Payment.freight)
    ).filter(
        and_(
            Payment.due_date >= start_dt,
            Payment.due_date <= end_dt
        )
    )
    
    if driver_id:
        query = query.filter_by(driver_id=driver_id)
    
    payments_raw = query.order_by(Payment.payment_date.desc()).all()

    # Group carregamento_70 + finalizacao_30 by freight into single display units
    from collections import defaultdict, OrderedDict
    freight_groups = OrderedDict()
    standalone_payments = []

    for payment in payments_raw:
        if payment.payment_type in ('carregamento_70', 'finalizacao_30') and payment.freight_id:
            if payment.freight_id not in freight_groups:
                freight_groups[payment.freight_id] = {'p70': None, 'p30': None}
            if payment.payment_type == 'carregamento_70':
                freight_groups[payment.freight_id]['p70'] = payment
            else:
                freight_groups[payment.freight_id]['p30'] = payment
        else:
            standalone_payments.append({'type': 'standalone', 'payment': payment})

    grouped_payments = []
    for freight_id, parts in freight_groups.items():
        p70 = parts['p70']
        p30 = parts['p30']
        anchor = p70 or p30
        grouped_payments.append({
            'type': 'freight_split',
            'freight': anchor.freight,
            'driver': anchor.driver,
            'p70': p70,
            'p30': p30,
            'total': (p70.amount if p70 else 0) + (p30.amount if p30 else 0),
        })
    grouped_payments.extend(standalone_payments)

    # Keep payments variable for totals calculation
    payments = payments_raw

    # Calculate totals by type
    totals = {
        'adiantamento': 0,
        'pagamento': 0,
        'desconto': 0
    }
    
    for payment in payments:
        if payment.payment_type in totals:
            totals[payment.payment_type] += payment.amount
    
    # NOVOS CÁLCULOS PARA DASHBOARD FINANCEIRO
    # 1. Receitas do período (cotações aprovadas que viraram fretes)
    from models import Freight, Quote
    receitas_periodo = db.session.query(func.sum(Freight.agreed_price)).filter(
        and_(
            Freight.created_at >= datetime.combine(start_dt, datetime.min.time()),
            Freight.created_at <= datetime.combine(end_dt, datetime.max.time()),
            Freight.status.in_(['ofertado', 'aceito', 'em_transito', 'entregue'])  # Todos os fretes exceto cancelados
        )
    ).scalar() or 0
    
    # 2. Custos do período (pagamentos para motoristas — usa due_date)
    custos_periodo = db.session.query(func.sum(Payment.amount)).filter(
        and_(
            Payment.due_date >= start_dt,
            Payment.due_date <= end_dt,
            Payment.payment_type.in_(['frete_total', 'carregamento_70', 'finalizacao_30'])
        )
    ).scalar() or 0
    
    # 3. Margem de lucro
    margem_lucro = receitas_periodo - custos_periodo
    
    # Dashboard totais
    dashboard_totals = {
        'receitas': float(receitas_periodo),
        'custos': float(custos_periodo),
        'margem_lucro': float(margem_lucro)
    }
    
    # Driver balances — single GROUP BY query instead of 3 queries per driver
    drivers_list = Driver.query.filter_by(is_active=True).order_by(Driver.name).all()

    sums_by_driver_type = db.session.query(
        Payment.driver_id,
        Payment.payment_type,
        func.sum(Payment.amount).label('total')
    ).filter(
        Payment.driver_id.isnot(None),
        Payment.payment_type.in_(['adiantamento', 'pagamento', 'desconto'])
    ).group_by(Payment.driver_id, Payment.payment_type).all()

    sums_map = {}
    for driver_id, payment_type, total in sums_by_driver_type:
        sums_map.setdefault(driver_id, {})[payment_type] = total or 0

    driver_balances = {}
    for driver in drivers_list:
        driver_sums = sums_map.get(driver.id, {})
        advances = driver_sums.get('adiantamento', 0)
        payments_received = driver_sums.get('pagamento', 0)
        discounts = driver_sums.get('desconto', 0)
        balance = advances - payments_received - discounts
        driver_balances[driver.id] = {
            'driver': driver,
            'advances': advances,
            'payments': payments_received,
            'discounts': discounts,
            'balance': balance
        }
    
    return render_template('financial/index.html', 
                         payments=payments,
                         grouped_payments=grouped_payments,
                         totals=totals,
                         driver_balances=driver_balances,
                         drivers=drivers_list,
                         start_date=start_date,
                         end_date=end_date,
                         selected_driver=driver_id,
                         dashboard_totals=dashboard_totals)

@financial_bp.route('/new-payment', methods=['GET', 'POST'])
@login_required
def new_payment():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('financial.index'))
    
    if request.method == 'POST':
        try:
            payment = Payment(
                driver_id=int(request.form['driver_id']),
                freight_id=int(request.form['freight_id']) if request.form.get('freight_id') else None,
                payment_type=request.form['payment_type'],
                amount=float(request.form['amount']),
                description=request.form.get('description'),
                payment_date=datetime.strptime(request.form['payment_date'], '%Y-%m-%d').date(),
                created_by=current_user.id
            )
            
            db.session.add(payment)
            db.session.commit()
            
            # Audit log
            audit = AuditLog(
                user_id=current_user.id,
                action='CREATE',
                table_name='payments',
                record_id=payment.id,
                new_values=f"Pagamento {payment.payment_type}: R$ {payment.amount}"
            )
            db.session.add(audit)
            db.session.commit()
            
            flash('Pagamento registrado com sucesso!', 'success')
            return redirect(url_for('financial.index'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao registrar pagamento: {str(e)}', 'error')
    
    drivers = Driver.query.filter_by(is_active=True).order_by(Driver.name).all()
    freights = Freight.query.filter(Freight.status.in_(['aceito', 'em_coleta', 'em_transito', 'entregue'])).order_by(Freight.created_at.desc()).all()
    
    return render_template('financial/payment_form.html', drivers=drivers, freights=freights)

@financial_bp.route('/driver/<int:driver_id>/balance')
@login_required
def driver_balance(driver_id):
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'message': 'Acesso negado'})
    
    driver = Driver.query.get_or_404(driver_id)
    
    # Calculate balance
    advances = db.session.query(func.sum(Payment.amount)).filter(
        and_(
            Payment.driver_id == driver_id,
            Payment.payment_type == 'adiantamento'
        )
    ).scalar() or 0
    
    payments_received = db.session.query(func.sum(Payment.amount)).filter(
        and_(
            Payment.driver_id == driver_id,
            Payment.payment_type == 'pagamento'
        )
    ).scalar() or 0
    
    discounts = db.session.query(func.sum(Payment.amount)).filter(
        and_(
            Payment.driver_id == driver_id,
            Payment.payment_type == 'desconto'
        )
    ).scalar() or 0
    
    balance = advances - payments_received - discounts
    
    return jsonify({
        'success': True,
        'driver_name': driver.name,
        'advances': float(advances),
        'payments': float(payments_received),
        'discounts': float(discounts),
        'balance': float(balance)
    })

@financial_bp.route('/reports')
@login_required
def reports():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('financial.index'))
    
    # Monthly financial summary — compute month boundaries first, then run
    # a single grouped query per data source instead of 3 queries x 12 months.
    month_ranges = []
    for i in range(12):
        start_date = datetime.now().replace(day=1) - timedelta(days=30*i)
        end_date = (start_date + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        month_ranges.append((start_date, end_date))

    earliest_start = min(r[0] for r in month_ranges)
    latest_end = max(r[1] for r in month_ranges)

    def _month_key(d):
        return (d.year, d.month)

    payment_rows = db.session.query(
        Payment.payment_type,
        func.extract('year', Payment.payment_date).label('yr'),
        func.extract('month', Payment.payment_date).label('mo'),
        func.sum(Payment.amount).label('total')
    ).filter(
        Payment.payment_date >= earliest_start.date(),
        Payment.payment_date <= latest_end.date(),
        Payment.payment_type.in_(['adiantamento', 'pagamento'])
    ).group_by(Payment.payment_type, 'yr', 'mo').all()

    payment_sums = {}
    for payment_type, yr, mo, total in payment_rows:
        payment_sums[(payment_type, int(yr), int(mo))] = total or 0

    freight_rows = db.session.query(
        func.extract('year', Freight.created_at).label('yr'),
        func.extract('month', Freight.created_at).label('mo'),
        func.sum(Freight.agreed_price).label('total')
    ).filter(
        Freight.created_at >= earliest_start,
        Freight.created_at <= latest_end,
        Freight.status == 'entregue'
    ).group_by('yr', 'mo').all()

    freight_sums = {(int(yr), int(mo)): total or 0 for yr, mo, total in freight_rows}

    monthly_data = []
    for start_date, end_date in month_ranges:
        yr, mo = _month_key(start_date)
        advances = payment_sums.get(('adiantamento', yr, mo), 0)
        payments = payment_sums.get(('pagamento', yr, mo), 0)
        freight_revenue = freight_sums.get((yr, mo), 0)

        monthly_data.append({
            'month': start_date.strftime('%b/%Y'),
            'advances': float(advances),
            'payments': float(payments),
            'revenue': float(freight_revenue),
            'profit': float(freight_revenue) - float(advances)
        })
    
    monthly_data.reverse()
    
    return render_template('financial/reports.html', monthly_data=monthly_data)

@financial_bp.route('/api/dashboard-data')
@login_required
def api_dashboard_data():
    """API endpoint para dados do dashboard financeiro"""
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        return jsonify({'success': False, 'message': 'Acesso negado'})
    
    # Date filters
    start_date = request.args.get('start_date', datetime.now().replace(day=1).strftime('%Y-%m-%d'))
    end_date = request.args.get('end_date', datetime.now().strftime('%Y-%m-%d'))
    
    start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
    end_dt = datetime.strptime(end_date, '%Y-%m-%d').date()
    
    # Receitas (fretes ativos/concluídos)
    from models import Freight
    receitas = db.session.query(func.sum(Freight.agreed_price)).filter(
        and_(
            Freight.created_at >= datetime.combine(start_dt, datetime.min.time()),
            Freight.created_at <= datetime.combine(end_dt, datetime.max.time()),
            Freight.status.in_(['ofertado', 'aceito', 'em_transito', 'entregue'])
        )
    ).scalar() or 0
    
    # Custos (pagamentos para motoristas)
    custos = db.session.query(func.sum(Payment.amount)).filter(
        and_(
            Payment.payment_date >= start_dt,
            Payment.payment_date <= end_dt,
            Payment.payment_type.in_(['frete_total', 'carregamento_70', 'finalizacao_30'])
        )
    ).scalar() or 0
    
    # Margem de lucro
    margem = float(receitas) - float(custos)
    
    return jsonify({
        'success': True,
        'data': {
            'receitas': float(receitas),
            'custos': float(custos),
            'margem_lucro': margem,
            'periodo': f"{start_date} até {end_date}"
        }
    })

@financial_bp.route('/export')
@login_required
def export():
    if current_user.role not in ['admin', 'operador', 'vendedor']:
        flash('Acesso negado.', 'error')
        return redirect(url_for('financial.index'))
    
    # This would generate Excel export
    # Implementation depends on requirements
    flash('Exportação em desenvolvimento.', 'info')
    return redirect(url_for('financial.index'))


@financial_bp.route("/mark-paid/<int:payment_id>", methods=["POST"])
@login_required
def mark_paid(payment_id):
    """Mark a payment as paid"""
    if current_user.role not in ["admin", "operador", "vendedor"]:
        return jsonify({"success": False, "message": "Acesso negado."}), 403
    
    try:
        payment = Payment.query.get_or_404(payment_id)

        # Accept optional JSON body with payment_date, method, obs
        data = {}
        if request.content_type and 'application/json' in request.content_type:
            data = request.get_json(silent=True) or {}

        paid_date_str = data.get('payment_date')
        if paid_date_str:
            try:
                paid_date = datetime.strptime(paid_date_str, '%Y-%m-%d').date()
            except ValueError:
                paid_date = datetime.now().date()
        else:
            paid_date = datetime.now().date()

        method = data.get('method', '')
        obs = data.get('obs', '')

        payment.status = "pago"
        payment.paid_date = paid_date
        if obs or method:
            extra = f" | Forma: {method}" if method else ""
            extra += f" | Obs: {obs}" if obs else ""
            payment.description = (payment.description or '') + extra

        audit = AuditLog(
            user_id=current_user.id,
            action="payment_marked_paid",
            table_name="payments",
            record_id=payment.id,
            new_values=f"Pagamento de R$ {payment.amount:.2f} para {payment.driver.name} marcado como pago em {paid_date}"
        )
        db.session.add(audit)
        db.session.commit()

        return jsonify({
            "success": True,
            "message": f"Pagamento de R$ {payment.amount:.2f} registrado como pago em {paid_date.strftime('%d/%m/%Y')}!"
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": f"Erro ao marcar pagamento: {str(e)}"}), 500

@financial_bp.route("/payment-details/<int:payment_id>")
@login_required
def payment_details(payment_id):
    """Get payment details for modal display"""
    if current_user.role not in ["admin", "operador", "vendedor"]:
        return jsonify({"success": False, "message": "Acesso negado."}), 403
    
    try:
        payment = Payment.query.get_or_404(payment_id)
        
        payment_data = {
            "id": payment.id,
            "driver_name": payment.driver.name,
            "driver_bank": getattr(payment.driver, "bank_name", "Não informado"),
            "driver_agency": getattr(payment.driver, "agency", "Não informado"),
            "driver_account": getattr(payment.driver, "account", "Não informado"),
            "driver_pix": getattr(payment.driver, "pix_key", "Não informado"),
            "freight_number": payment.freight.freight_number if payment.freight else "N/A",
            "freight_origin": getattr(payment.freight, 'origin', 'N/A') if payment.freight else "N/A",
            "freight_destination": getattr(payment.freight, 'destination', 'N/A') if payment.freight else "N/A",
            "payment_type_display": "Pagamento Frete" if payment.payment_type == "frete_total" else payment.payment_type.title(),
            "amount": float(payment.amount),
            "status": payment.status.title(),
            "payment_date": payment.payment_date.strftime("%d/%m/%Y"),
            "description": payment.description,
            "paid_date": payment.paid_date.strftime("%d/%m/%Y") if payment.paid_date else None,
            "payment_70_status": payment.payment_70_status or "pendente",
            "payment_30_status": payment.payment_30_status or "pendente", 
            "payment_70_date": payment.payment_70_date.strftime("%Y-%m-%d") if payment.payment_70_date else None,
            "payment_30_date": payment.payment_30_date.strftime("%Y-%m-%d") if payment.payment_30_date else None
        }
        
        return jsonify({"success": True, "payment": payment_data})
        
    except Exception as e:
        return jsonify({"success": False, "message": f"Erro ao carregar detalhes: {str(e)}"}), 500



@financial_bp.route("/mark-partial-payment/<int:payment_id>", methods=["POST"])
@login_required
def mark_partial_payment(payment_id):
    """Mark a partial payment (70% or 30%) as paid"""
    if current_user.role not in ["admin", "operador", "vendedor"]:
        return jsonify({"success": False, "message": "Acesso negado."}), 403
    
    try:
        data = request.get_json()
        percentage = data.get("percentage")
        payment_date = data.get("payment_date")
        
        if percentage not in [70, 30]:
            return jsonify({"success": False, "message": "Porcentagem inválida"}), 400
            
        payment = Payment.query.get_or_404(payment_id)
        
        # Update the specific percentage payment
        if percentage == 70:
            setattr(payment, "payment_70_status", "pago")
            setattr(payment, "payment_70_date", datetime.strptime(payment_date, "%Y-%m-%d").date())
            description = f"70% do pagamento marcado como pago"
        else:
            setattr(payment, "payment_30_status", "pago")
            setattr(payment, "payment_30_date", datetime.strptime(payment_date, "%Y-%m-%d").date())
            description = f"30% do pagamento marcado como pago"
        
        # Check if both parts are paid to update overall status
        payment_70_status = getattr(payment, "payment_70_status", "pendente")
        payment_30_status = getattr(payment, "payment_30_status", "pendente")
        
        if payment_70_status == "pago" and payment_30_status == "pago":
            payment.status = "pago"
            payment.paid_date = datetime.now().date()
        elif payment_70_status == "pago" or payment_30_status == "pago":
            payment.status = "parcial"
        
        # Create audit log
        audit = AuditLog(
            user_id=current_user.id,
            action="partial_payment_marked",
            table_name="payments",
            record_id=payment.id
        )
        db.session.add(audit)
        
        db.session.commit()
        
        return jsonify({
            "success": True, 
            "message": f"Pagamento de {percentage}% marcado como pago!"
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": f"Erro ao marcar pagamento: {str(e)}"}), 500

