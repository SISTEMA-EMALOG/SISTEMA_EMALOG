import requests
import re
import logging
from datetime import datetime

def validate_cnpj(cnpj):
    """Validate CNPJ and get company information"""
    try:
        # Remove formatting
        cnpj = re.sub(r'[^0-9]', '', cnpj)
        
        # Basic validation
        if len(cnpj) != 14:
            return {
                'valid': False,
                'message': 'CNPJ deve ter 14 dígitos'
            }
        
        # Check if all digits are the same
        if len(set(cnpj)) == 1:
            return {
                'valid': False,
                'message': 'CNPJ inválido'
            }
        
        # Calculate verification digits
        if not _validate_cnpj_digits(cnpj):
            return {
                'valid': False,
                'message': 'CNPJ inválido'
            }
        
        # Query external API for company information
        try:
            api_url = f"https://receitaws.com.br/v1/cnpj/{cnpj}"
            response = requests.get(api_url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get('status') == 'ERROR':
                    return {
                        'valid': True,  # CNPJ is mathematically valid
                        'message': 'CNPJ válido, mas não encontrado na Receita Federal',
                        'company_name': '',
                        'address': ''
                    }
                
                # Format address
                address_parts = []
                if data.get('logradouro'):
                    address_parts.append(data['logradouro'])
                if data.get('numero'):
                    address_parts.append(f"nº {data['numero']}")
                if data.get('complemento'):
                    address_parts.append(data['complemento'])
                if data.get('bairro'):
                    address_parts.append(data['bairro'])
                if data.get('municipio'):
                    address_parts.append(data['municipio'])
                if data.get('uf'):
                    address_parts.append(data['uf'])
                if data.get('cep'):
                    address_parts.append(f"CEP: {data['cep']}")
                
                return {
                    'valid': True,
                    'message': 'CNPJ válido',
                    'company_name': data.get('nome', ''),
                    'trade_name': data.get('fantasia', ''),
                    'address': ', '.join(address_parts),
                    'phone': data.get('telefone', ''),
                    'email': data.get('email', ''),
                    'activity': data.get('atividade_principal', [{}])[0].get('text', '') if data.get('atividade_principal') else '',
                    'situation': data.get('situacao', ''),
                    'opened_at': data.get('abertura', '')
                }
            else:
                # API failed, but CNPJ is mathematically valid
                return {
                    'valid': True,
                    'message': 'CNPJ válido (não foi possível consultar dados da empresa)',
                    'company_name': '',
                    'address': ''
                }
                
        except requests.RequestException as e:
            logging.warning(f"CNPJ API request failed: {str(e)}")
            return {
                'valid': True,
                'message': 'CNPJ válido (serviço de consulta indisponível)',
                'company_name': '',
                'address': ''
            }
        
    except Exception as e:
        logging.error(f"Error validating CNPJ: {str(e)}")
        return {
            'valid': False,
            'message': 'Erro na validação do CNPJ'
        }

def _validate_cnpj_digits(cnpj):
    """Validate CNPJ check digits"""
    try:
        # Convert to list of integers
        digits = [int(d) for d in cnpj]
        
        # First verification digit
        weights1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
        sum1 = sum(digits[i] * weights1[i] for i in range(12))
        remainder1 = sum1 % 11
        digit1 = 0 if remainder1 < 2 else 11 - remainder1
        
        if digits[12] != digit1:
            return False
        
        # Second verification digit
        weights2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
        sum2 = sum(digits[i] * weights2[i] for i in range(13))
        remainder2 = sum2 % 11
        digit2 = 0 if remainder2 < 2 else 11 - remainder2
        
        return digits[13] == digit2
        
    except:
        return False

def format_cnpj(cnpj):
    """Format CNPJ with mask XX.XXX.XXX/XXXX-XX"""
    cnpj = re.sub(r'[^0-9]', '', cnpj)
    if len(cnpj) == 14:
        return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:14]}"
    return cnpj

def validate_cpf(cpf):
    """Validate CPF document"""
    try:
        # Remove formatting
        cpf = re.sub(r'[^0-9]', '', cpf)
        
        # Basic validation
        if len(cpf) != 11:
            return False
        
        # Check if all digits are the same
        if len(set(cpf)) == 1:
            return False
        
        # Calculate first verification digit
        sum1 = sum(int(cpf[i]) * (10 - i) for i in range(9))
        remainder1 = sum1 % 11
        digit1 = 0 if remainder1 < 2 else 11 - remainder1
        
        if int(cpf[9]) != digit1:
            return False
        
        # Calculate second verification digit
        sum2 = sum(int(cpf[i]) * (11 - i) for i in range(10))
        remainder2 = sum2 % 11
        digit2 = 0 if remainder2 < 2 else 11 - remainder2
        
        return int(cpf[10]) == digit2
        
    except:
        return False

def format_cpf(cpf):
    """Format CPF with mask XXX.XXX.XXX-XX"""
    cpf = re.sub(r'[^0-9]', '', cpf)
    if len(cpf) == 11:
        return f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:11]}"
    return cpf
