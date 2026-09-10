import requests
import os
import logging
from models import WhatsAppMessage
from app import db
from datetime import datetime

def send_freight_offer(freight, driver):
    """Send WhatsApp message with freight offer to driver"""
    try:
        api_url = os.environ.get('WHATSAPP_API_URL', '')
        api_token = os.environ.get('WHATSAPP_API_TOKEN', '')

        if not api_url or not api_token:
            # If WhatsApp API is not configured, just log the message
            logging.warning("WhatsApp API not configured. Message would be sent to driver.")
            message_content = generate_freight_message(freight, driver)

            # Save message record
            whatsapp_msg = WhatsAppMessage(
                freight_id=freight.id,
                driver_id=driver.id,
                message_content=message_content,
                phone_number=driver.phone,
                status='enviado'  # Mark as sent since we're simulating
            )
            db.session.add(whatsapp_msg)
            db.session.commit()

            logging.info(f"WhatsApp message logged for {driver.name} ({driver.phone}): {message_content}")
            return True

        # Generate message content
        message_content = generate_freight_message(freight, driver)

        # Format phone number (remove special characters)
        phone = driver.phone.replace('(', '').replace(')', '').replace('-', '').replace(' ', '')
        if not phone.startswith('55'):
            phone = '55' + phone

        # Prepare API request
        payload = {
            'phone': phone,
            'message': message_content
        }

        headers = {
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json'
        }

        # Send request
        response = requests.post(f"{api_url}/send-message", json=payload, headers=headers, timeout=30)

        # Save message record
        whatsapp_msg = WhatsAppMessage(
            freight_id=freight.id,
            driver_id=driver.id,
            message_content=message_content,
            phone_number=driver.phone,
            status='enviado' if response.status_code == 200 else 'erro'
        )

        if response.status_code == 200:
            whatsapp_msg.delivered_at = datetime.utcnow()

        db.session.add(whatsapp_msg)
        db.session.commit()

        if response.status_code == 200:
            logging.info(f"WhatsApp message sent successfully to {driver.name}")
            return True
        else:
            logging.error(f"Failed to send WhatsApp message: {response.text}")
            return False

    except Exception as e:
        logging.error(f"Error sending WhatsApp message: {str(e)}")

        # Save error record
        whatsapp_msg = WhatsAppMessage(
            freight_id=freight.id,
            driver_id=driver.id,
            message_content=generate_freight_message(freight, driver),
            phone_number=driver.phone,
            status='erro'
        )
        db.session.add(whatsapp_msg)
        db.session.commit()

        return False

def generate_freight_message(freight, driver):
    """Generate WhatsApp message content for freight offer"""
    # Check if there's a custom message
    if hasattr(freight, 'custom_whatsapp_message') and freight.custom_whatsapp_message:
        # Use custom message with variable replacement
        message = freight.custom_whatsapp_message

        # Get quote data for variables
        quote = freight.quote if freight.quote_id else None
        payment_value = quote.driver_cost if quote else freight.agreed_price

        # Build dimensions string
        dimensions = ""
        if quote and (quote.load_length or quote.load_width or quote.load_height):
            dims = []
            if quote.load_length:
                dims.append(f"C:{quote.load_length:.0f}cm")
            if quote.load_width:
                dims.append(f"L:{quote.load_width:.0f}cm")
            if quote.load_height:
                dims.append(f"A:{quote.load_height:.0f}cm")
            dimensions = " x ".join(dims)

        # Replace variables
        replacements = {
            '{driver_name}': driver.name,
            '{freight_number}': freight.freight_number,
            '{product}': freight.product,
            '{weight}': f"{freight.weight:.0f} kg",
            '{volume}': f"{quote.load_volume:.2f} m³" if quote and quote.load_volume else 'Não especificado',
            '{origin}': freight.origin,
            '{destination}': freight.destination,
            '{driver_cost}': f"R$ {payment_value:.2f}",
            '{pickup_date}': freight.pickup_date.strftime('%d/%m/%Y') if freight.pickup_date else 'A combinar',
            '{delivery_date}': freight.delivery_date.strftime('%d/%m/%Y') if freight.delivery_date else 'A combinar',
            '{load_type}': quote.load_type.title() if quote and quote.load_type else 'Não especificado',
            '{vehicle_type}': quote.vehicle_type.title() if quote and quote.vehicle_type else 'Não especificado',
            '{dimensions}': dimensions or 'Não especificado'
        }

        for variable, value in replacements.items():
            message = message.replace(variable, str(value))

        return message

    # Default message generation
    # Get quote data for additional cargo info and driver cost
    quote = freight.quote if hasattr(freight, 'quote') and freight.quote else None

    # Determine the payment value (driver cost from quote, or agreed price if no quote)
    payment_value = quote.driver_cost if quote and quote.driver_cost else freight.agreed_price

    # Build cargo info
    cargo_info = []
    if quote:
        # Load type (dedicated/fractional)
        load_type_text = "Dedicado" if quote.load_type == "dedicado" else "Fracionado"
        cargo_info.append(f"📋 *Tipo:* {load_type_text}")

        # Dimensions if available
        if quote.load_length or quote.load_width or quote.load_height:
            dimensions = []
            if quote.load_length:
                dimensions.append(f"C:{quote.load_length:.0f}cm")
            if quote.load_width:
                dimensions.append(f"L:{quote.load_width:.0f}cm")
            if quote.load_height:
                dimensions.append(f"A:{quote.load_height:.0f}cm")
            if dimensions:
                cargo_info.append(f"📐 *Dimensões:* {' x '.join(dimensions)}")

        # Volume if available
        if quote.load_volume:
            cargo_info.append(f"📦 *Volume:* {quote.load_volume:.2f} m³")

        # Vehicle type
        vehicle_types = {
            'vuc': 'VUC',
            '3/4': '3/4',
            'truck': 'Truck',
            'carreta': 'Carreta',
            'van': 'Van',
            'fiorino': 'Fiorino',
            'toco': 'Toco'
        }
        vehicle_text = vehicle_types.get(quote.vehicle_type, quote.vehicle_type.title())
        cargo_info.append(f"🚚 *Veículo:* {vehicle_text}")

    cargo_section = '\n'.join(cargo_info) if cargo_info else ""

    template = f"""
🚛 *NOVA OFERTA DE FRETE - EMALOG*

Olá {driver.name}! 

Temos uma nova oferta de frete para você:

📋 *Frete:* {freight.freight_number}
📦 *Produto:* {freight.product}
⚖️ *Peso:* {freight.weight:,.0f} kg
📍 *Origem:* {freight.origin}
📍 *Destino:* {freight.destination}
💰 *Valor para você:* R$ {payment_value:,.2f}

{cargo_section}

📅 *Coleta:* {freight.pickup_date.strftime('%d/%m/%Y') if freight.pickup_date else 'A combinar'}
📅 *Entrega:* {freight.delivery_date.strftime('%d/%m/%Y') if freight.delivery_date else 'A combinar'}

Para aceitar esta oferta, responda:
✅ *ACEITO* - para aceitar o frete
❌ *RECUSO* - para recusar

⏰ *Importante:* Esta oferta tem prazo limitado!

EMALOG - Conectando você aos melhores fretes! 🚚
""".strip()

    return template

def send_custom_message(phone, message):
    """Send custom WhatsApp message"""
    try:
        api_url = os.environ.get('WHATSAPP_API_URL', '')
        api_token = os.environ.get('WHATSAPP_API_TOKEN', '')

        if not api_url or not api_token:
            logging.warning("WhatsApp API not configured.")
            return False

        # Format phone number
        phone = phone.replace('(', '').replace(')', '').replace('-', '').replace(' ', '')
        if not phone.startswith('55'):
            phone = '55' + phone

        payload = {
            'phone': phone,
            'message': message
        }

        headers = {
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json'
        }

        response = requests.post(f"{api_url}/send-message", json=payload, headers=headers, timeout=30)

        return response.status_code == 200

    except Exception as e:
        logging.error(f"Error sending custom WhatsApp message: {str(e)}")
        return False

def process_whatsapp_response(phone, message, freight_id=None):
    """Process incoming WhatsApp responses"""
    try:
        message_lower = message.lower().strip()

        # Find the freight and driver
        if freight_id:
            from models import Freight, Driver
            freight = Freight.query.get(freight_id)
            driver = Driver.query.filter_by(phone=phone).first()

            if freight and driver:
                # Process response
                if 'aceito' in message_lower or 'aceitar' in message_lower:
                    # Driver accepted the freight
                    freight.status = 'aceito'
                    freight.assigned_driver_id = driver.id

                    # Update WhatsApp responses
                    import json
                    responses = json.loads(freight.whatsapp_responses) if freight.whatsapp_responses else []
                    responses.append({
                        'driver_id': driver.id,
                        'driver_name': driver.name,
                        'response': 'aceito',
                        'timestamp': datetime.utcnow().isoformat(),
                        'message': message
                    })
                    freight.whatsapp_responses = json.dumps(responses)

                    db.session.commit()

                    # Send confirmation
                    confirmation = f"✅ Perfeito {driver.name}! Frete {freight.freight_number} confirmado para você. Nossa equipe entrará em contato para os detalhes."
                    send_custom_message(phone, confirmation)

                    return True

                elif 'recuso' in message_lower or 'recusar' in message_lower:
                    # Driver refused the freight
                    import json
                    responses = json.loads(freight.whatsapp_responses) if freight.whatsapp_responses else []
                    responses.append({
                        'driver_id': driver.id,
                        'driver_name': driver.name,
                        'response': 'recusado',
                        'timestamp': datetime.utcnow().isoformat(),
                        'message': message
                    })
                    freight.whatsapp_responses = json.dumps(responses)

                    db.session.commit()

                    # Send confirmation
                    confirmation = f"❌ Entendido {driver.name}. Obrigado pela resposta rápida!"
                    send_custom_message(phone, confirmation)

                    return True

        return False

    except Exception as e:
        logging.error(f"Error processing WhatsApp response: {str(e)}")
        return False

def get_message_templates():
    """Get available WhatsApp message templates"""
    return {
        'freight_offer': {
            'name': 'Oferta de Frete',
            'template': generate_freight_message,
            'description': 'Template padrão para ofertas de frete'
        },
        'payment_reminder': {
            'name': 'Lembrete de Pagamento',
            'template': 'Olá {driver_name}! Lembramos que você possui um saldo devedor de R$ {amount:,.2f}. Entre em contato conosco para regularizar.',
            'description': 'Lembrete para motoristas com saldo devedor'
        },
        'document_reminder': {
            'name': 'Lembrete de Documentos',
            'template': 'Olá {driver_name}! Seus documentos estão próximos do vencimento. Por favor, envie as versões atualizadas: {documents}',
            'description': 'Lembrete para renovação de documentos'
        }
    }