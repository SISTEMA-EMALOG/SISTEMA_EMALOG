"""
Email service for automatic notifications
"""
import os
import json
import logging
from flask import current_app
from flask_mail import Message
from app import mail
from models import Client

def get_client_notification_emails(client_id):
    """Get all notification emails for a client"""
    try:
        client = Client.query.get(client_id)
        if not client:
            return []

        return client.all_notification_emails

    except Exception as e:
        logging.error(f"Error getting client notification emails: {e}")
        return []

def send_quote_priced_email(quote):
    """Send email when quote is priced"""
    try:
        # Verificar se as configurações de email estão definidas
        if not current_app.config.get('MAIL_USERNAME'):
            logging.error("MAIL_USERNAME não configurado - não é possível enviar emails")
            return False

        if not current_app.config.get('MAIL_PASSWORD'):
            logging.error("MAIL_PASSWORD não configurado - não é possível enviar emails")
            return False

        client_emails = get_client_notification_emails(quote.client_id)
        if not client_emails:
            logging.warning(f"No notification emails found for client {quote.client_id}")
            return False

        # Email content for quote pricing
        subject = f"Cotação Precificada - {quote.quote_number}"

        body = f"""
        Prezado(a) {quote.client.company_name},

        Sua cotação foi precificada!

        Detalhes da Cotação:
        - Número: {quote.quote_number}
        - Origem: {quote.origin_city}, {quote.origin_state}
        - Destino: {quote.destination_city}, {quote.destination_state}
        - Valor: R$ {quote.sale_value:,.2f}

        Acesse o sistema para mais detalhes.

        Atenciosamente,
        Equipe EMALOG
        """

        # Send email to all client notification addresses
        msg = Message(
            subject=subject,
            recipients=client_emails,
            body=body,
            sender=current_app.config['MAIL_DEFAULT_SENDER']
        )

        mail.send(msg)
        logging.info(f"Quote priced email sent to {len(client_emails)} recipients for quote {quote.quote_number}")
        return True

    except Exception as e:
        logging.error(f"Error sending quote priced email: {e}")
        return False

def send_quote_email(quote):
    """Send quote email to client"""
    try:
        # Verificar se as configurações de email estão definidas
        if not current_app.config.get('MAIL_USERNAME'):
            logging.error("MAIL_USERNAME não configurado - não é possível enviar emails")
            return False

        if not current_app.config.get('MAIL_PASSWORD'):
            logging.error("MAIL_PASSWORD não configurado - não é possível enviar emails")
            return False

        client_emails = get_client_notification_emails(quote.client_id)
        if not client_emails:
            logging.warning(f"No notification emails found for client {quote.client_id}")
            return False

        # Email content for quote
        subject = f"Nova Cotação - {quote.quote_number}"

        body = f"""
        Prezado(a) {quote.client.company_name},

        Segue em anexo a cotação solicitada.

        Detalhes da Cotação:
        - Número: {quote.quote_number}
        - Origem: {quote.origin_city}, {quote.origin_state}
        - Destino: {quote.destination_city}, {quote.destination_state}
        - Peso: {quote.load_weight}kg
        - Tipo de Carga: {quote.load_type}

        Acesse o sistema para mais detalhes.

        Atenciosamente,
        Equipe EMALOG
        """

        # Send email
        msg = Message(
            subject=subject,
            recipients=client_emails,
            body=body,
            sender=current_app.config['MAIL_DEFAULT_SENDER']
        )

        mail.send(msg)
        logging.info(f"Quote email sent to {len(client_emails)} recipients for quote {quote.quote_number}")
        return True

    except Exception as e:
        logging.error(f"Error sending quote email: {e}")
        return False

def get_all_freight_recipient_emails(freight):
    """Coleta todos os destinatários de email para um frete: contatos do cliente + usuários portal"""
    emails = set()
    try:
        client_emails = get_client_notification_emails(freight.client_id)
        for e in client_emails:
            if e:
                emails.add(e.strip())
        # Incluir emails de usuários portal do cliente
        from models import User
        portal_users = User.query.filter_by(client_id=freight.client_id, active=True).all()
        for u in portal_users:
            if u.email:
                emails.add(u.email.strip())
    except Exception as exc:
        logging.warning(f"Erro ao coletar destinatários do frete {freight.id}: {exc}")
    return list(emails)


def send_freight_completed_email(freight, nf_files):
    """Send email when freight is completed with NF attachments"""
    try:
        if not current_app.config.get('MAIL_PASSWORD'):
            logging.warning("MAIL_PASSWORD não configurado - email de finalização não enviado")
            return False
        if not current_app.config.get('MAIL_USERNAME'):
            logging.warning("MAIL_USERNAME não configurado - email de finalização não enviado")
            return False

        recipients = get_all_freight_recipient_emails(freight)
        if not recipients:
            logging.warning(f"Nenhum destinatário encontrado para o cliente {freight.client_id}")
            return False

        from datetime import datetime as _dt
        quote = getattr(freight, 'quote', None)
        produto = (quote.load_type if quote and quote.load_type else 'N/D')
        peso    = (f"{int(quote.load_weight)} kg" if quote and quote.load_weight else 'N/D')
        driver_name = freight.assigned_driver.name if freight.assigned_driver else 'Não atribuído'
        placa   = freight.assigned_driver.vehicle_plate if freight.assigned_driver else 'N/D'
        finalizado_em = _dt.now().strftime('%d/%m/%Y às %H:%M')
        valor   = float(freight.agreed_price or 0)

        subject = f"EMALOG — Frete {freight.freight_number} Finalizado com Sucesso"

        # ── Plain text fallback ──────────────────────────────────────────────
        body_text = f"""Prezado(a) {freight.client.company_name},

Informamos que o frete abaixo foi finalizado com sucesso.

DETALHES DO FRETE:
  Número   : {freight.freight_number}
  Origem   : {freight.origin or 'N/D'}
  Destino  : {freight.destination or 'N/D'}
  Produto  : {produto}
  Peso     : {peso}
  Valor    : R$ {valor:,.2f}
  Status   : FINALIZADO

MOTORISTA:
  Nome     : {driver_name}
  Placa    : {placa}

  Finalizado em: {finalizado_em}

{f"Os comprovantes de entrega ({len(nf_files)} arquivo(s)) estão anexados neste e-mail." if nf_files else ""}

Atenciosamente,
Equipe EMALOG
comercial@emalog.com.br
"""

        # ── HTML email ───────────────────────────────────────────────────────
        body_html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;background:#f3f4f6;margin:0;padding:20px;">
  <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1);">
    <div style="background:#1d4ed8;padding:24px 32px;">
      <h1 style="color:#fff;margin:0;font-size:20px;">EMALOG — Frete Finalizado</h1>
      <p style="color:#93c5fd;margin:4px 0 0;font-size:14px;">Comprovante de entrega</p>
    </div>
    <div style="padding:28px 32px;">
      <p style="color:#374151;margin:0 0 20px;">Prezado(a) <strong>{freight.client.company_name}</strong>,</p>
      <p style="color:#374151;margin:0 0 20px;">Informamos que o seu frete foi <strong style="color:#16a34a;">finalizado com sucesso</strong>.</p>

      <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
        <tr style="background:#f9fafb;">
          <th colspan="2" style="text-align:left;padding:10px 14px;color:#1d4ed8;font-size:13px;border-bottom:2px solid #e5e7eb;">DETALHES DO FRETE</th>
        </tr>
        <tr>
          <td style="padding:8px 14px;color:#6b7280;font-size:13px;width:40%;">Número</td>
          <td style="padding:8px 14px;color:#111827;font-size:13px;font-weight:600;">{freight.freight_number}</td>
        </tr>
        <tr style="background:#f9fafb;">
          <td style="padding:8px 14px;color:#6b7280;font-size:13px;">Origem</td>
          <td style="padding:8px 14px;color:#111827;font-size:13px;">{freight.origin or 'N/D'}</td>
        </tr>
        <tr>
          <td style="padding:8px 14px;color:#6b7280;font-size:13px;">Destino</td>
          <td style="padding:8px 14px;color:#111827;font-size:13px;">{freight.destination or 'N/D'}</td>
        </tr>
        <tr style="background:#f9fafb;">
          <td style="padding:8px 14px;color:#6b7280;font-size:13px;">Produto</td>
          <td style="padding:8px 14px;color:#111827;font-size:13px;">{produto}</td>
        </tr>
        <tr>
          <td style="padding:8px 14px;color:#6b7280;font-size:13px;">Peso</td>
          <td style="padding:8px 14px;color:#111827;font-size:13px;">{peso}</td>
        </tr>
        <tr style="background:#f9fafb;">
          <td style="padding:8px 14px;color:#6b7280;font-size:13px;">Valor</td>
          <td style="padding:8px 14px;color:#1d4ed8;font-size:14px;font-weight:700;">R$ {valor:,.2f}</td>
        </tr>
        <tr>
          <td style="padding:8px 14px;color:#6b7280;font-size:13px;">Motorista</td>
          <td style="padding:8px 14px;color:#111827;font-size:13px;">{driver_name} &nbsp;({placa})</td>
        </tr>
        <tr style="background:#f9fafb;">
          <td style="padding:8px 14px;color:#6b7280;font-size:13px;">Finalizado em</td>
          <td style="padding:8px 14px;color:#111827;font-size:13px;">{finalizado_em}</td>
        </tr>
      </table>

      {"<p style='color:#374151;font-size:13px;background:#f0fdf4;border-left:4px solid #16a34a;padding:12px 16px;border-radius:4px;'>Os comprovantes de entrega estão <strong>anexados</strong> neste e-mail (" + str(len(nf_files)) + " arquivo(s)).</p>" if nf_files else ""}
    </div>
    <div style="background:#f9fafb;padding:16px 32px;border-top:1px solid #e5e7eb;text-align:center;">
      <p style="color:#9ca3af;font-size:12px;margin:0;">EMALOG — Sistema de Gestão Logística</p>
      <p style="color:#9ca3af;font-size:12px;margin:2px 0 0;">comercial@emalog.com.br</p>
    </div>
  </div>
</body>
</html>"""

        msg = Message(
            subject=subject,
            recipients=recipients,
            body=body_text,
            html=body_html,
            sender=current_app.config.get('MAIL_DEFAULT_SENDER', 'comercial@emalog.com.br')
        )

        # Add NF files as attachments — use storage abstraction (works with Object Storage or local FS)
        from utils.storage import get_file as _storage_get, file_exists as _storage_exists
        for nf_file in nf_files:
            file_key = nf_file.get('path', '')
            if not file_key:
                continue
            file_data = _storage_get(file_key)
            if file_data:
                msg.attach(
                    filename=nf_file['filename'],
                    content_type="application/octet-stream",
                    data=file_data.read()
                )
            else:
                logging.warning(f"Anexo NF não encontrado no storage: {file_key}")

        mail.send(msg)
        logging.info(f"Email de frete finalizado enviado para {len(recipients)} destinatário(s) — frete {freight.freight_number} com {len(nf_files)} anexo(s)")
        return True

    except Exception as e:
        logging.error(f"Erro ao enviar email de frete finalizado: {e}")
        return False

def test_email_connection():
    """Test email configuration"""
    try:
        with mail.connect() as conn:
            logging.info("Email connection test successful")
            return True
    except Exception as e:
        logging.error(f"Email connection test failed: {e}")
        return False