import pandas as pd
from io import BytesIO
from flask import make_response
from models import Driver, Client, Quote, Freight, Payment
from app import db
from datetime import datetime
import logging

def export_drivers_excel():
    """Export drivers data to Excel"""
    try:
        # Get all drivers
        drivers = Driver.query.all()
        
        # Prepare data
        data = []
        for driver in drivers:
            data.append({
                'ID': driver.id,
                'Nome': driver.name,
                'CPF': driver.cpf,
                'RG': driver.rg,
                'Data Nascimento': driver.birth_date.strftime('%d/%m/%Y'),
                'Telefone': driver.phone,
                'Email': driver.email or '',
                'Endereço': driver.address,
                'CNH': driver.cnh_number,
                'Categoria CNH': driver.cnh_category,
                'Validade CNH': driver.cnh_expiry.strftime('%d/%m/%Y'),
                'Placa Veículo': driver.vehicle_plate,
                'Modelo Veículo': driver.vehicle_model,
                'Ano Veículo': driver.vehicle_year,
                'Banco': driver.bank_name or '',
                'Agência': driver.agency or '',
                'Conta': driver.account or '',
                'PIX': driver.pix_key or '',
                'Status': 'Ativo' if driver.is_active else 'Inativo',
                'Data Cadastro': driver.created_at.strftime('%d/%m/%Y %H:%M')
            })
        
        # Create DataFrame
        df = pd.DataFrame(data)
        
        # Create Excel file in memory
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Motoristas')
            
            # Auto-adjust column widths
            worksheet = writer.sheets['Motoristas']
            for column in worksheet.columns:
                max_length = 0
                column = [cell for cell in column]
                for cell in column:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except:
                        pass
                adjusted_width = min(max_length + 2, 50)
                worksheet.column_dimensions[column[0].column_letter].width = adjusted_width
        
        output.seek(0)
        
        # Create response
        response = make_response(output.getvalue())
        response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        response.headers['Content-Disposition'] = f'attachment; filename=motoristas_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
        
        return response
        
    except Exception as e:
        logging.error(f"Error exporting drivers to Excel: {str(e)}")
        raise

def import_drivers_excel(file, user_id):
    """Import drivers from Excel file"""
    try:
        # Read Excel file
        df = pd.read_excel(file)
        
        # Validate required columns
        required_columns = ['Nome', 'CPF', 'RG', 'Data Nascimento', 'Telefone', 'Endereço', 
                          'CNH', 'Categoria CNH', 'Validade CNH', 'Placa Veículo', 
                          'Modelo Veículo', 'Ano Veículo']
        
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            return {
                'success': False, 
                'message': f'Colunas obrigatórias ausentes: {", ".join(missing_columns)}'
            }
        
        imported_count = 0
        errors = []
        
        for index, row in df.iterrows():
            try:
                # Check if driver already exists
                existing = Driver.query.filter_by(cpf=row['CPF']).first()
                if existing:
                    errors.append(f'Linha {index + 2}: CPF {row["CPF"]} já cadastrado')
                    continue
                
                # Parse dates
                birth_date = pd.to_datetime(row['Data Nascimento']).date()
                cnh_expiry = pd.to_datetime(row['Validade CNH']).date()
                
                # Create driver
                driver = Driver(
                    name=str(row['Nome']),
                    cpf=str(row['CPF']),
                    rg=str(row['RG']),
                    birth_date=birth_date,
                    phone=str(row['Telefone']),
                    email=str(row.get('Email', '')) if pd.notna(row.get('Email')) else None,
                    address=str(row['Endereço']),
                    cnh_number=str(row['CNH']),
                    cnh_category=str(row['Categoria CNH']),
                    cnh_expiry=cnh_expiry,
                    vehicle_plate=str(row['Placa Veículo']),
                    vehicle_model=str(row['Modelo Veículo']),
                    vehicle_year=int(row['Ano Veículo']),
                    bank_name=str(row.get('Banco', '')) if pd.notna(row.get('Banco')) else None,
                    agency=str(row.get('Agência', '')) if pd.notna(row.get('Agência')) else None,
                    account=str(row.get('Conta', '')) if pd.notna(row.get('Conta')) else None,
                    pix_key=str(row.get('PIX', '')) if pd.notna(row.get('PIX')) else None,
                    created_by=user_id
                )
                
                db.session.add(driver)
                imported_count += 1
                
            except Exception as e:
                errors.append(f'Linha {index + 2}: {str(e)}')
        
        if imported_count > 0:
            db.session.commit()
        
        result = {
            'success': True,
            'message': f'{imported_count} motoristas importados com sucesso.',
            'imported_count': imported_count
        }
        
        if errors:
            result['warnings'] = errors
            
        return result
        
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error importing drivers from Excel: {str(e)}")
        return {
            'success': False,
            'message': f'Erro ao importar arquivo: {str(e)}'
        }

def export_quotes_to_excel(quotes):
    """Export quotes data to Excel"""
    try:
        # Prepare data
        data = []
        for quote in quotes:
            data.append({
                'Número': quote.quote_number,
                'Cliente': quote.client.company_name if quote.client else '',
                'Origem': f"{quote.origin_city}/{quote.origin_state}" if quote.origin_city else quote.origin_cep,
                'Destino': f"{quote.destination_city}/{quote.destination_state}" if quote.destination_city else quote.destination_cep,
                'Tipo Carga': quote.cargo_type or '',
                'Peso (kg)': quote.cargo_weight or 0,
                'Valor Carga (R$)': quote.cargo_value or 0,
                'Valor Venda (R$)': quote.sale_value or 0,
                'Status': quote.status,
                'Data Criação': quote.created_at.strftime('%d/%m/%Y %H:%M'),
                'Data Cotação': quote.quoted_at.strftime('%d/%m/%Y %H:%M') if quote.quoted_at else '',
                'Válido até': quote.valid_until.strftime('%d/%m/%Y') if quote.valid_until else '',
                'Observações': quote.observations or ''
            })
        
        # Create DataFrame
        df = pd.DataFrame(data)
        
        # Create Excel file in memory
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Cotações')
            
            # Auto-adjust column widths
            worksheet = writer.sheets['Cotações']
            for column in worksheet.columns:
                max_length = 0
                column = [cell for cell in column]
                for cell in column:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except:
                        pass
                adjusted_width = min(max_length + 2, 50)
                worksheet.column_dimensions[column[0].column_letter].width = adjusted_width
        
        output.seek(0)
        
        # Save to temporary file
        import tempfile
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
        temp_file.write(output.getvalue())
        temp_file.close()
        
        return temp_file.name
        
    except Exception as e:
        logging.error(f"Error exporting quotes to Excel: {str(e)}")
        raise

def generate_report_excel(report_type, start_date, end_date):
    """Generate Excel reports"""
    try:
        output = BytesIO()
        
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            if report_type == 'drivers':
                # Driver performance report
                drivers_data = []
                drivers = Driver.query.all()
                
                for driver in drivers:
                    total_freights = len(driver.assigned_freights)
                    completed_freights = len([f for f in driver.assigned_freights if f.status == 'entregue'])
                    total_revenue = sum([f.agreed_price for f in driver.assigned_freights if f.status == 'entregue'])
                    
                    drivers_data.append({
                        'Nome': driver.name,
                        'CPF': driver.cpf,
                        'Telefone': driver.phone,
                        'Total Fretes': total_freights,
                        'Fretes Entregues': completed_freights,
                        'Taxa Conclusão (%)': (completed_freights / total_freights * 100) if total_freights > 0 else 0,
                        'Receita Total (R$)': total_revenue,
                        'Status': 'Ativo' if driver.is_active else 'Inativo'
                    })
                
                df_drivers = pd.DataFrame(drivers_data)
                df_drivers.to_excel(writer, index=False, sheet_name='Relatório Motoristas')
                
            elif report_type == 'clients':
                # Client activity report
                clients_data = []
                clients = Client.query.all()
                
                for client in clients:
                    total_quotes = len(client.quotes)
                    approved_quotes = len([q for q in client.quotes if q.status == 'aprovado'])
                    total_freights = len(client.freights)
                    total_spent = sum([f.agreed_price for f in client.freights if f.status == 'entregue'])
                    
                    clients_data.append({
                        'Empresa': client.company_name,
                        'CNPJ': client.cnpj,
                        'Telefone': client.phone,
                        'Email': client.email,
                        'Total Cotações': total_quotes,
                        'Cotações Aprovadas': approved_quotes,
                        'Taxa Aprovação (%)': (approved_quotes / total_quotes * 100) if total_quotes > 0 else 0,
                        'Total Fretes': total_freights,
                        'Total Gasto (R$)': total_spent,
                        'Status': 'Ativo' if client.is_active else 'Inativo'
                    })
                
                df_clients = pd.DataFrame(clients_data)
                df_clients.to_excel(writer, index=False, sheet_name='Relatório Clientes')
                
            elif report_type == 'financial':
                # Financial report
                start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
                end_dt = datetime.strptime(end_date, '%Y-%m-%d').date()
                
                # Payments data
                payments = Payment.query.filter(
                    Payment.payment_date.between(start_dt, end_dt)
                ).all()
                
                payments_data = []
                for payment in payments:
                    payments_data.append({
                        'Data': payment.payment_date.strftime('%d/%m/%Y'),
                        'Motorista': payment.driver.name,
                        'Tipo': payment.payment_type,
                        'Valor (R$)': payment.amount,
                        'Descrição': payment.description or '',
                        'Frete': payment.freight.freight_number if payment.freight else ''
                    })
                
                df_payments = pd.DataFrame(payments_data)
                df_payments.to_excel(writer, index=False, sheet_name='Pagamentos')
                
                # Freights data
                freights = Freight.query.filter(
                    Freight.created_at.between(
                        datetime.combine(start_dt, datetime.min.time()),
                        datetime.combine(end_dt, datetime.max.time())
                    )
                ).all()
                
                freights_data = []
                for freight in freights:
                    freights_data.append({
                        'Número': freight.freight_number,
                        'Data': freight.created_at.strftime('%d/%m/%Y'),
                        'Cliente': freight.client.company_name,
                        'Origem': freight.origin,
                        'Destino': freight.destination,
                        'Produto': freight.product,
                        'Peso (kg)': freight.weight,
                        'Valor (R$)': freight.agreed_price,
                        'Motorista': freight.assigned_driver.name if freight.assigned_driver else '',
                        'Status': freight.status
                    })
                
                df_freights = pd.DataFrame(freights_data)
                df_freights.to_excel(writer, index=False, sheet_name='Fretes')
        
        output.seek(0)
        return output.getvalue()
        
    except Exception as e:
        logging.error(f"Error generating Excel report: {str(e)}")
        raise
