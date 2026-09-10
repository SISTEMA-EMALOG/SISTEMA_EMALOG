import requests

def validate_and_get_address_data(cep):
    """
    Validates Brazilian CEP and returns address data
    Returns dict with address information or None if invalid
    """
    if not cep:
        return None
    
    # Remove any formatting and validate
    clean_cep = cep.replace('-', '').replace('.', '').replace(' ', '')
    
    if len(clean_cep) != 8 or not clean_cep.isdigit():
        return None
    
    try:
        # Call ViaCEP API
        response = requests.get(f'https://viacep.com.br/ws/{clean_cep}/json/', timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            # Check if CEP exists
            if 'erro' not in data:
                return {
                    'success': True,
                    'cep': data.get('cep', ''),
                    'street': data.get('logradouro', ''),
                    'neighborhood': data.get('bairro', ''),
                    'city': data.get('localidade', ''),
                    'state': data.get('uf', '')
                }
        
        return {'success': False, 'error': 'CEP não encontrado'}
        
    except Exception as e:
        print(f"Erro ao consultar CEP: {e}")
        return {'success': False, 'error': 'Erro ao consultar CEP'}