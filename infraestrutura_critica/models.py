from infraestrutura_critica.app import db
from flask_login import UserMixin
from datetime import datetime
from sqlalchemy import func

class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='operador')  # admin, operador, cliente
    active = db.Column(db.Boolean, default=True)
    first_login = db.Column(db.Boolean, default=True)
    must_change_password = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    # Relationship with clients (for client users)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=True)
    client = db.relationship('Client', foreign_keys=[client_id], backref='users')

    @property
    def is_active(self):
        """Flask-Login required property"""
        return self.active

class Driver(db.Model):
    __tablename__ = 'drivers'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    cpf = db.Column(db.String(14), unique=True, nullable=True)   # opcional — EMA coleta
    rg = db.Column(db.String(20), nullable=True)
    birth_date = db.Column(db.Date, nullable=True)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120))

    # Address data (Brazilian format)
    cep = db.Column(db.String(10), nullable=True)
    street = db.Column(db.String(200), nullable=True)
    number = db.Column(db.String(10), nullable=True)
    complement = db.Column(db.String(100))
    neighborhood = db.Column(db.String(100), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(2), nullable=True)

    # CNH data
    cnh_expiry = db.Column(db.Date, nullable=True)
    antt_number = db.Column(db.String(20))  # Número ANTT

    # Vehicle data with truck type
    truck_type = db.Column(db.String(20), nullable=True)
    has_tracker = db.Column(db.Boolean, default=False)
    tracker_type = db.Column(db.String(20))
    vehicle_plate = db.Column(db.String(10), nullable=True)
    vehicle_model = db.Column(db.String(50), nullable=True)
    vehicle_year = db.Column(db.Integer, nullable=True)

    # Banking data
    bank_name = db.Column(db.String(50))
    agency = db.Column(db.String(10))
    account = db.Column(db.String(20))
    pix_key = db.Column(db.String(100))

    # Documents
    cnh_document = db.Column(db.String(255))
    crlv_document = db.Column(db.String(255))
    antt_document = db.Column(db.String(255))
    address_proof = db.Column(db.String(255))
    vehicle_photo = db.Column(db.String(255))

    # Cavalinho data (only for carreta type — tractor unit that pulls the trailer)
    cavalinho_plate = db.Column(db.String(10))
    cavalinho_model = db.Column(db.String(50))
    cavalinho_year = db.Column(db.Integer)
    cavalinho_crlv = db.Column(db.String(255))

    active = db.Column(db.Boolean, default=True)
    is_active = db.Column(db.Boolean, default=True)
    availability_status = db.Column(db.String(20), default='disponivel')  # disponivel, em_frete, indisponivel
    validated = db.Column(db.Boolean, default=False)  # operator validated EMA data
    whatsapp_mode = db.Column(db.String(10), default='auto', server_default=db.text("'auto'"), nullable=False)
    whatsapp_assigned_to = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))

    # Relationships
    creator = db.relationship('User', foreign_keys=[created_by], backref='created_drivers')

class Client(db.Model):
    __tablename__ = 'clients'

    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(100), nullable=False)
    cnpj = db.Column(db.String(18), unique=True, nullable=False)
    trade_name = db.Column(db.String(100))
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120), nullable=False)

    # Address data (Brazilian format)
    cep = db.Column(db.String(10))
    street = db.Column(db.String(200))
    number = db.Column(db.String(10))
    complement = db.Column(db.String(100))
    neighborhood = db.Column(db.String(100))
    city = db.Column(db.String(100))
    state = db.Column(db.String(2))

    # Legacy address field for backward compatibility
    address = db.Column(db.Text)

    # Multiple email contacts for notifications (max 5)
    notification_emails = db.Column(db.Text)  # JSON string with email list
    
    # Responsible contacts (legacy)
    contacts = db.Column(db.Text)  # JSON string with multiple contacts

    active = db.Column(db.Boolean, default=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))

    # Relationships
    creator = db.relationship('User', foreign_keys=[created_by], backref='created_clients')

    @property
    def parsed_contacts(self):
        """Parse contacts JSON string into Python objects"""
        if self.contacts:
            try:
                import json
                return json.loads(self.contacts)
            except:
                return []
        return []

    @property
    def parsed_notification_emails(self):
        """Parse notification emails JSON string into Python list"""
        if self.notification_emails:
            try:
                import json
                emails = json.loads(self.notification_emails)
                return [email.strip() for email in emails if email.strip()] if isinstance(emails, list) else []
            except:
                return []
        return []

    @property
    def all_notification_emails(self):
        """Get all notification emails including main email"""
        emails = [self.email] if self.email else []
        emails.extend(self.parsed_notification_emails)
        return list(set(emails))[:5]  # Maximum 5 emails

    @property
    def full_address(self):
        """Generate full address string from individual fields"""
        if self.street:
            parts = [self.street]
            if self.number:
                parts.append(self.number)
            if self.complement:
                parts.append(f"- {self.complement}")
            if self.neighborhood:
                parts.append(f"{self.neighborhood}")
            if self.city and self.state:
                parts.append(f"{self.city}/{self.state}")
            if self.cep:
                parts.append(f"CEP: {self.cep}")
            return ", ".join(parts)
        return self.address or ""

class Quote(db.Model):
    __tablename__ = 'quotes'

    id = db.Column(db.Integer, primary_key=True)
    quote_number = db.Column(db.String(50), unique=True, nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)

    # Origin data with CEP integration
    origin_cep = db.Column(db.String(10), nullable=False)
    origin_street = db.Column(db.String(200))
    origin_number = db.Column(db.String(10))
    origin_complement = db.Column(db.String(100))
    origin_neighborhood = db.Column(db.String(100))
    origin_city = db.Column(db.String(100))
    origin_state = db.Column(db.String(2))
    pickup_date = db.Column(db.Date, nullable=False)
    urgent_pickup = db.Column(db.Boolean, default=False)

    # Destination data with CEP integration
    destination_cep = db.Column(db.String(10), nullable=False)
    destination_street = db.Column(db.String(200))
    destination_number = db.Column(db.String(10))
    destination_complement = db.Column(db.String(100))
    destination_neighborhood = db.Column(db.String(100))
    destination_city = db.Column(db.String(100))
    destination_state = db.Column(db.String(2))
    delivery_deadline = db.Column(db.Integer)  # dias para entrega
    vehicle_type = db.Column(db.String(20), nullable=False)  # vuc, 3/4, truck, carreta, van, fiorino, toco

    # Load data
    load_type = db.Column(db.String(20), nullable=False)  # dedicado, fracionado
    load_height = db.Column(db.Float)  # centimetros (primeiro item)
    load_length = db.Column(db.Float)  # centimetros (primeiro item)
    load_width = db.Column(db.Float)  # centimetros (primeiro item)
    load_weight = db.Column(db.Float, nullable=False)  # kg (total)
    load_volume = db.Column(db.Float)  # volume em m³
    invoice_value = db.Column(db.Float)  # valor da NF em reais
    invoice_file = db.Column(db.String(255))  # path para arquivo PDF da NF
    cargo_items_json = db.Column(db.Text)  # JSON array de itens da carga

    # CNPJ Origem / Destino (empresa que embarca / que recebe)
    origin_cnpj    = db.Column(db.String(20))
    origin_company = db.Column(db.String(200))
    destination_cnpj    = db.Column(db.String(20))
    destination_company = db.Column(db.String(200))

    # Endereços alternativos de coleta/entrega (quando diferem do CNPJ)
    pickup_address_notes   = db.Column(db.Text)
    delivery_address_notes = db.Column(db.Text)

    # Pricing (separated cost and sale)
    driver_cost = db.Column(db.Float, nullable=False)  # valor a pagar motorista
    sale_value = db.Column(db.Float, nullable=False)  # valor de venda ao cliente

    # Negotiation fields
    client_counter_value = db.Column(db.Float)  # valor da contra-proposta do cliente
    client_counter_message = db.Column(db.Text)  # mensagem da contra-proposta
    client_counter_at = db.Column(db.DateTime)  # quando cliente fez contra-proposta
    operator_response_value = db.Column(db.Float)  # resposta do operador
    operator_response_message = db.Column(db.Text)  # mensagem da resposta do operador
    operator_response_at = db.Column(db.DateTime)  # quando operador respondeu
    negotiation_round = db.Column(db.Integer, default=0)  # número da rodada de negociação

    # Additional info
    additional_info = db.Column(db.Text)
    valid_until = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default='pendente')  # pendente, cotada, aguardando_cliente, negociacao, aprovada, rejeitada
    
    # Campos de controle de status
    quoted_at = db.Column(db.DateTime)  # quando foi cotada
    quoted_by = db.Column(db.Integer, db.ForeignKey('users.id'))  # quem cotou
    approved_at = db.Column(db.DateTime)  # quando foi aprovada
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'))  # quem aprovou
    rejected_at = db.Column(db.DateTime)  # quando foi rejeitada
    rejected_by = db.Column(db.Integer, db.ForeignKey('users.id'))  # quem rejeitou
    rejection_reason = db.Column(db.Text)  # motivo da rejeição

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))

    # CRM linkage — optional FK to CRM opportunity
    opportunity_id = db.Column(db.Integer, db.ForeignKey('opportunities.id'), nullable=True)

    # Multi-stop support
    is_multi_stop = db.Column(db.Boolean, default=False, server_default='false', nullable=False)
    stops_json = db.Column(db.Text)  # JSON array of stops when is_multi_stop=True

    # Relationships
    client = db.relationship('Client', backref='quotes')
    creator = db.relationship('User', foreign_keys=[created_by], backref='created_quotes')
    quoter = db.relationship('User', foreign_keys=[quoted_by], backref='quoted_quotes')
    approver = db.relationship('User', foreign_keys=[approved_by], backref='approved_quotes')
    rejecter = db.relationship('User', foreign_keys=[rejected_by], backref='rejected_quotes')
    crm_opportunity = db.relationship('Opportunity', foreign_keys=[opportunity_id], backref='linked_quotes')

    @property
    def origin_full_address(self):
        """Generate full origin address string"""
        parts = []
        if self.origin_street:
            parts.append(self.origin_street)
        if self.origin_number:
            parts.append(self.origin_number)
        if self.origin_complement:
            parts.append(f"- {self.origin_complement}")
        if self.origin_neighborhood:
            parts.append(self.origin_neighborhood)
        if self.origin_city and self.origin_state:
            parts.append(f"{self.origin_city}/{self.origin_state}")
        if self.origin_cep:
            parts.append(f"CEP: {self.origin_cep}")
        return ", ".join(parts)

    @property
    def destination_full_address(self):
        """Generate full destination address string"""
        parts = []
        if self.destination_street:
            parts.append(self.destination_street)
        if self.destination_number:
            parts.append(self.destination_number)
        if self.destination_complement:
            parts.append(f"- {self.destination_complement}")
        if self.destination_neighborhood:
            parts.append(self.destination_neighborhood)
        if self.destination_city and self.destination_state:
            parts.append(f"{self.destination_city}/{self.destination_state}")
        if self.destination_cep:
            parts.append(f"CEP: {self.destination_cep}")
        return ", ".join(parts)

    @property
    def cargo_items(self):
        """Parse cargo_items_json into a Python list."""
        if not self.cargo_items_json:
            return []
        try:
            import json as _j
            return _j.loads(self.cargo_items_json)
        except Exception:
            return []

    @property
    def stops(self):
        """Parse stops_json into a Python list of stop dicts."""
        if not self.stops_json:
            return []
        try:
            import json as _j
            return _j.loads(self.stops_json)
        except Exception:
            return []

    @property
    def load_dimensions_text(self):
        """Generate load dimensions text"""
        items = self.cargo_items
        if items:
            count = len(items)
            return f"{count} item{'ns' if count > 1 else ''}"
        dims = []
        if self.load_length:
            dims.append(f"C: {self.load_length}cm")
        if self.load_width:
            dims.append(f"L: {self.load_width}cm")
        if self.load_height:
            dims.append(f"A: {self.load_height}cm")
        return " x ".join(dims) if dims else "N/A"

class Freight(db.Model):
    __tablename__ = 'freights'

    id = db.Column(db.Integer, primary_key=True)
    freight_number = db.Column(db.String(30), unique=True, nullable=False)
    quote_id = db.Column(db.Integer, db.ForeignKey('quotes.id'))
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)

    # Route and load (can be different from quote)
    origin = db.Column(db.String(200), nullable=False)
    destination = db.Column(db.String(200), nullable=False)
    product = db.Column(db.String(100), nullable=False)
    weight = db.Column(db.Float, nullable=False)

    # Pricing
    agreed_price = db.Column(db.Float, nullable=False)
    driver_cost = db.Column(db.Float)  # valor a pagar ao motorista

    # Driver assignment
    selected_drivers = db.Column(db.Text)  # JSON with driver IDs
    assigned_driver_id = db.Column(db.Integer, db.ForeignKey('drivers.id'))

    # Status tracking
    status = db.Column(db.String(20), default='ofertado', index=True)  # ofertado, aceito, em_transito, entregue, cancelado

    # Location details for reporting
    origin_city = db.Column(db.String(100))
    origin_state = db.Column(db.String(2))  
    destination_city = db.Column(db.String(100))
    destination_state = db.Column(db.String(2))

    # Dates
    pickup_date = db.Column(db.Date)
    delivery_date = db.Column(db.Date)

    # WhatsApp tracking
    whatsapp_sent = db.Column(db.Boolean, default=False)
    whatsapp_responses = db.Column(db.Text)  # JSON with responses

    notes = db.Column(db.Text)
    custom_whatsapp_message = db.Column(db.Text)  # Custom WhatsApp message template
    stops_json = db.Column(db.Text)  # JSON array of stops for multi-stop freights
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))

    # Relationships
    quote = db.relationship('Quote', backref='freights')
    client = db.relationship('Client', backref='freights')
    assigned_driver = db.relationship('Driver', backref='assigned_freights')
    creator = db.relationship('User', foreign_keys=[created_by], backref='created_freights')

    @property
    def stops(self):
        """Parse stops_json into a Python list of stop dicts."""
        if not self.stops_json:
            return []
        try:
            import json as _j
            return _j.loads(self.stops_json)
        except Exception:
            return []

class Payment(db.Model):
    __tablename__ = 'payments'

    id = db.Column(db.Integer, primary_key=True)
    driver_id = db.Column(db.Integer, db.ForeignKey('drivers.id'), nullable=True)
    freight_id = db.Column(db.Integer, db.ForeignKey('freights.id'))

    payment_type = db.Column(db.String(30), nullable=False, index=True)  # carregamento_70, finalizacao_30, adiantamento, desconto
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(200))
    status = db.Column(db.String(20), default='pendente')  # pendente, pago, cancelado

    # Payment tracking
    due_date = db.Column(db.Date, index=True)  # data prevista para pagamento
    paid_date = db.Column(db.Date)  # data efetiva do pagamento
    milestone = db.Column(db.String(50))  # carregamento, finalizacao

    # Split payment tracking (70% loading + 30% completion)
    payment_70_status = db.Column(db.String(20), default='pendente')  # pendente, pago
    payment_30_status = db.Column(db.String(20), default='pendente')  # pendente, pago
    payment_70_date = db.Column(db.Date)  # data do pagamento dos 70%
    payment_30_date = db.Column(db.Date)  # data do pagamento dos 30%

    payment_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))

    # Relationships
    driver = db.relationship('Driver', backref='payments')
    freight = db.relationship('Freight', backref='payments')
    creator = db.relationship('User', foreign_keys=[created_by], backref='created_payments')

class WhatsAppMessage(db.Model):
    __tablename__ = 'whatsapp_messages'

    id = db.Column(db.Integer, primary_key=True)
    freight_id = db.Column(db.Integer, db.ForeignKey('freights.id'))
    driver_id = db.Column(db.Integer, db.ForeignKey('drivers.id'))

    message_content = db.Column(db.Text, nullable=False)
    phone_number = db.Column(db.String(20), nullable=False)

    status = db.Column(db.String(20), default='enviando')  # enviando, enviado, entregue, lido, erro
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    delivered_at = db.Column(db.DateTime)

    response_content = db.Column(db.Text)
    response_at = db.Column(db.DateTime)
    direction = db.Column(db.String(10), default='outbound', server_default=db.text("'outbound'"), nullable=False)
    source = db.Column(db.String(20), default='legacy', server_default=db.text("'legacy'"), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    external_message_id = db.Column(db.String(120), unique=True, index=True)

    # Central de Atendimento (Fase 0). Nullable de propósito: a tabela já tem
    # dados em produção e o auto-migrator gera ALTER TABLE ADD COLUMN sem
    # DEFAULT para colunas NOT NULL, o que falha em tabela populada nos dois
    # bancos — e o erro é engolido em log, deixando a coluna faltando.
    conversation_id = db.Column(db.Integer, db.ForeignKey('conversations.id'))

    # Central de Atendimento (Fase 3): quando o RESPONSÁVEL pela conversa leu
    # esta mensagem recebida. Nulo significa não lida.
    #
    # Coluna própria, e não status='lido', por um defeito real: o webhook do
    # EMA usa status='processado' para ignorar reentrega da Evolution. Gravar
    # 'lido' por cima fazia uma reentrega ser processada de novo, e o bot
    # respondia duas vezes ao motorista. status pertence ao ciclo de entrega
    # e processamento; leitura humana mora aqui.
    read_at = db.Column(db.DateTime)

    # Relationships
    freight = db.relationship('Freight', backref='whatsapp_messages')
    driver = db.relationship('Driver', backref='whatsapp_messages')
    creator = db.relationship('User', foreign_keys=[created_by], backref='whatsapp_messages')
    conversation = db.relationship('Conversation', backref='messages')

class ChatMessage(db.Model):
    __tablename__ = 'chat_messages'

    id = db.Column(db.Integer, primary_key=True)
    room = db.Column(db.String(50), nullable=False, index=True)  # chat room identifier
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    message_content = db.Column(db.Text, nullable=False)
    message_type = db.Column(db.String(20), default='text')  # text, file, image
    file_path = db.Column(db.String(255))

    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False, index=True)

    # Relationships
    sender = db.relationship('User', backref='chat_messages')

class AuditLog(db.Model):
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    action = db.Column(db.String(20), nullable=False)  # CREATE, UPDATE, DELETE
    table_name = db.Column(db.String(50), nullable=False)
    record_id = db.Column(db.Integer, nullable=False)

    old_values = db.Column(db.Text)
    new_values = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref='audit_logs')

class FreightDocument(db.Model):
    __tablename__ = 'freight_documents'
    
    id = db.Column(db.Integer, primary_key=True)
    freight_id = db.Column(db.Integer, db.ForeignKey('freights.id'), nullable=False)
    document_type = db.Column(db.String(50), nullable=False, default='nf_assinada')  # nf_assinada, comprovante, etc
    filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    
    # Relationships
    freight = db.relationship('Freight', backref='documents')
    uploader = db.relationship('User')

class FreightStatusLog(db.Model):
    __tablename__ = 'freight_status_logs'

    id = db.Column(db.Integer, primary_key=True)
    freight_id = db.Column(db.Integer, db.ForeignKey('freights.id'), nullable=False)
    old_status = db.Column(db.String(30))
    new_status = db.Column(db.String(30), nullable=False)
    notes = db.Column(db.Text)
    changed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    freight = db.relationship('Freight', backref='status_logs')
    user = db.relationship('User', backref='status_changes')


class DriverRating(db.Model):
    __tablename__ = 'driver_ratings'

    id = db.Column(db.Integer, primary_key=True)
    freight_id = db.Column(db.Integer, db.ForeignKey('freights.id'), nullable=False)
    driver_id = db.Column(db.Integer, db.ForeignKey('drivers.id'), nullable=False)
    rating = db.Column(db.Integer, nullable=False)  # 1-5
    punctuality = db.Column(db.Integer)   # 1-5
    cargo_care = db.Column(db.Integer)    # 1-5
    communication = db.Column(db.Integer) # 1-5
    comment = db.Column(db.Text)
    rated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    freight = db.relationship('Freight', backref='ratings')
    driver = db.relationship('Driver', backref='ratings')
    rater = db.relationship('User', backref='given_ratings')


class QuoteNegotiation(db.Model):
    __tablename__ = 'quote_negotiations'

    id = db.Column(db.Integer, primary_key=True)
    quote_id = db.Column(db.Integer, db.ForeignKey('quotes.id'), nullable=False)
    round_number = db.Column(db.Integer, nullable=False)
    
    # Quem fez a proposta (client ou operator)
    proposer_type = db.Column(db.String(20), nullable=False)  # client, operator
    proposer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    proposed_value = db.Column(db.Float, nullable=False)
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    quote = db.relationship('Quote', backref='negotiations')
    proposer = db.relationship('User', backref='negotiations')


# ═══════════════════════════════════════════════════════════════════
# CRM — Leads, Opportunities, Activities
# ═══════════════════════════════════════════════════════════════════

class Lead(db.Model):
    __tablename__ = 'crm_leads'

    id                     = db.Column(db.Integer, primary_key=True)
    company_name           = db.Column(db.String(200), nullable=False)
    cnpj                   = db.Column(db.String(20))
    contact_name           = db.Column(db.String(150))
    contact_phone          = db.Column(db.String(50))
    contact_email          = db.Column(db.String(150))
    city                   = db.Column(db.String(150))
    state                  = db.Column(db.String(2))
    segment                = db.Column(db.String(255))
    # indicacao | linkedin | site | cold_call | whatsapp | evento | outro
    source                 = db.Column(db.String(50), default='outro')
    # novo | em_contato | qualificado | proposta | convertido | perdido
    status                 = db.Column(db.String(30), default='novo')
    estimated_monthly_value = db.Column(db.Float)
    notes                  = db.Column(db.Text)
    assigned_to            = db.Column(db.Integer, db.ForeignKey('users.id'))
    converted_client_id    = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=True)
    created_at             = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at             = db.Column(db.DateTime, default=datetime.utcnow)
    # Campos extras para enriquecimento / importação
    industry_type          = db.Column(db.String(255))   # ex: Alimentício, Farmacêutico
    website                = db.Column(db.String(300))
    employee_count         = db.Column(db.String(50))    # faixa: "50-100"
    zip_code               = db.Column(db.String(10))    # CEP
    address                = db.Column(db.String(300))
    import_batch           = db.Column(db.String(100))   # identificador do lote de importação
    ai_enriched            = db.Column(db.Boolean, default=False)  # foi processado pelo agente
    is_hot                 = db.Column(db.Boolean, default=False)  # lead quente (gerado operacionalmente)
    operational_count      = db.Column(db.Integer, default=0)      # nº de vezes que apareceu em fretes
    last_freight_ref       = db.Column(db.String(200))             # referência do último frete que gerou o lead
    # Campos de endereço estruturado (espelha Client)
    whatsapp               = db.Column(db.String(50))
    street                 = db.Column(db.String(200))
    number_addr            = db.Column(db.String(20))
    complement             = db.Column(db.String(200))
    neighborhood           = db.Column(db.String(200))

    next_contact_at        = db.Column(db.DateTime)        # próximo follow-up agendado

    salesperson       = db.relationship('User',   foreign_keys=[assigned_to],         backref='assigned_leads')
    converted_client  = db.relationship('Client', foreign_keys=[converted_client_id])
    opportunities     = db.relationship('Opportunity', backref='lead', lazy='dynamic',
                                        cascade='all, delete-orphan')
    activities        = db.relationship('CRMActivity', backref='lead', lazy='dynamic',
                                        cascade='all, delete-orphan')

    @property
    def active_opportunity_count(self):
        return self.opportunities.filter(
            Opportunity.stage.notin_(['ganho', 'perdido'])
        ).count()


class Opportunity(db.Model):
    __tablename__ = 'opportunities'

    id                    = db.Column(db.Integer, primary_key=True)
    lead_id               = db.Column(db.Integer, db.ForeignKey('crm_leads.id'), nullable=False)
    title                 = db.Column(db.String(200), nullable=False)
    # prospeccao | contato | reuniao | proposta | negociacao | ganho | perdido
    stage                 = db.Column(db.String(30), default='prospeccao')
    value                 = db.Column(db.Float)
    freight_origin        = db.Column(db.String(200))
    freight_destination   = db.Column(db.String(200))
    cargo_type            = db.Column(db.String(100))
    vehicle_type          = db.Column(db.String(50))
    frequency             = db.Column(db.String(50))
    probability           = db.Column(db.Integer, default=10)
    expected_close_date   = db.Column(db.Date)
    lost_reason           = db.Column(db.String(300))
    notes                 = db.Column(db.Text)
    assigned_to           = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at            = db.Column(db.DateTime, default=datetime.utcnow)
    # Arquivamento: ganho/perdido com > 90 dias são arquivados automaticamente
    archived              = db.Column(db.Boolean, default=False, server_default='false', nullable=False)

    salesperson    = db.relationship('User', foreign_keys=[assigned_to], backref='assigned_opportunities')
    opp_activities = db.relationship('CRMActivity', backref='opportunity', lazy='dynamic')


class CRMActivity(db.Model):
    __tablename__ = 'crm_activities'

    id              = db.Column(db.Integer, primary_key=True)
    lead_id         = db.Column(db.Integer, db.ForeignKey('crm_leads.id'),  nullable=True)
    opportunity_id  = db.Column(db.Integer, db.ForeignKey('opportunities.id'), nullable=True)
    # ligacao | email | reuniao | visita | proposta | tarefa | nota
    activity_type   = db.Column(db.String(30), default='nota')
    title           = db.Column(db.String(200))
    notes           = db.Column(db.Text)
    scheduled_at    = db.Column(db.DateTime)
    completed_at    = db.Column(db.DateTime)
    is_done         = db.Column(db.Boolean, default=False)
    outcome         = db.Column(db.String(500))
    created_by      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[created_by], backref='crm_activities')


class LeadBatch(db.Model):
    __tablename__ = 'lead_batches'

    id            = db.Column(db.Integer, primary_key=True)
    batch_id      = db.Column(db.String(100), unique=True, nullable=False)  # IMP-20260515120000
    name          = db.Column(db.String(200), nullable=False)               # "Transportadoras SP - Mai/26"
    filename      = db.Column(db.String(300))
    source        = db.Column(db.String(50))
    assigned_to   = db.Column(db.Integer, db.ForeignKey('users.id'))
    total_leads   = db.Column(db.Integer, default=0)
    created_by    = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    notes         = db.Column(db.Text)

    salesperson   = db.relationship('User', foreign_keys=[assigned_to], backref='lead_batches')
    creator       = db.relationship('User', foreign_keys=[created_by], backref='created_batches')


class AssistantMessage(db.Model):
    __tablename__ = 'assistant_messages'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    role       = db.Column(db.String(10), nullable=False)   # 'user' ou 'model'
    text       = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='assistant_messages')


class DriverBid(db.Model):
    """Driver price consultation for a specific freight."""
    __tablename__ = 'driver_bids'

    id         = db.Column(db.Integer, primary_key=True)
    freight_id = db.Column(db.Integer, db.ForeignKey('freights.id'), nullable=False)
    driver_id  = db.Column(db.Integer, db.ForeignKey('drivers.id'),  nullable=False)
    # sent / responded / no_price / accepted / declined / refused
    status          = db.Column(db.String(20), default='sent')
    driver_price    = db.Column(db.Numeric(10, 2))   # price the driver quoted
    driver_message  = db.Column(db.Text)              # last raw message from driver
    history         = db.Column(db.JSON, default=list)  # full conversation turns
    # Contracting Kanban: awaiting_response / conversation / interested /
    # validating / contracted / closed
    kanban_stage     = db.Column(db.String(30), default='awaiting_response',
                                 server_default=db.text("'awaiting_response'"), nullable=False, index=True)
    kanban_history   = db.Column(db.JSON, default=list)
    internal_notes   = db.Column(db.Text)
    closed_reason    = db.Column(db.String(300))
    stage_changed_at = db.Column(db.DateTime, default=datetime.utcnow)
    accepted_at      = db.Column(db.DateTime)
    assigned_to      = db.Column(db.Integer, db.ForeignKey('users.id'))
    sent_at         = db.Column(db.DateTime)
    responded_at    = db.Column(db.DateTime)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    freight = db.relationship('Freight', backref='driver_bids')
    driver  = db.relationship('Driver',  backref='driver_bids')
    assignee = db.relationship('User', foreign_keys=[assigned_to], backref='assigned_driver_bids')


class EmaSession(db.Model):
    """Tracks a driver data-enrichment conversation managed by the EMA agent."""
    __tablename__ = 'ema_sessions'

    id           = db.Column(db.Integer, primary_key=True)
    driver_id    = db.Column(db.Integer, db.ForeignKey('drivers.id'), nullable=False)
    # pending / active / awaiting_file / completed / error / paused
    status       = db.Column(db.String(20), default='pending')
    current_field= db.Column(db.String(50))          # which field we're collecting now
    history      = db.Column(db.JSON, default=list)  # [{role, text, ts}]
    staged_data  = db.Column(db.JSON, default=dict)  # collected but not yet saved
    error_msg    = db.Column(db.Text)
    started_at   = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    revalidation       = db.Column(db.Boolean, default=False)  # True = confirma TODOS os campos
    last_driver_msg_at = db.Column(db.DateTime)   # última mensagem recebida DO motorista
    reminder_sent_at   = db.Column(db.DateTime)   # quando o lembrete de inatividade foi enviado
    abandoned_reason   = db.Column(db.String(200)) # motivo do abandono (inatividade/etc.)

    driver = db.relationship('Driver', backref='ema_sessions')


# ── Central de Atendimento (atendimento_conversas) ───────────────────────────
# Fase 0 do PLANO_DE_FASES.md. Agrupa as mensagens soltas de whatsapp_messages
# em uma conversa por contato, com status, atendente atribuído e atividade.
#
# Não substitui EmaSession: aquela é a máquina de estados do bot de captação,
# é obrigatoriamente ligada a um motorista já cadastrado (driver_id NOT NULL) e
# não guarda mensagens. Conversation aceita número desconhecido e é o container
# das mensagens.

# Status possíveis de uma conversa.
CONVERSATION_STATUS_OPEN     = 'aberta'
CONVERSATION_STATUS_PENDING  = 'pendente'
CONVERSATION_STATUS_RESOLVED = 'resolvida'

CONVERSATION_STATUSES = (
    CONVERSATION_STATUS_OPEN,
    CONVERSATION_STATUS_PENDING,
    CONVERSATION_STATUS_RESOLVED,
)

# Uma conversa não-resolvida é a "ativa" daquele contato. Só pode haver uma,
# garantido por índice parcial único no banco (ver utils/migrations.py).
CONVERSATION_ACTIVE_STATUSES = (
    CONVERSATION_STATUS_OPEN,
    CONVERSATION_STATUS_PENDING,
)


class Conversation(db.Model):
    """Uma conversa de WhatsApp com um contato, atendida por um operador."""
    __tablename__ = 'conversations'

    id = db.Column(db.Integer, primary_key=True)

    # Chave do contato: dígitos em E.164 sem o '+', ex. '5511999998888'.
    # 32 de largura porque o valor cru da Twilio ('whatsapp:+55...') tem 23
    # caracteres e phone_number em whatsapp_messages só tem 20 — normalizar
    # antes de gravar (atendimento_conversas/utils/phone.py).
    contact_phone = db.Column(db.String(32), nullable=False, index=True)
    contact_name  = db.Column(db.String(120))

    # Nullable: número desconhecido abre conversa antes de existir motorista.
    # A exclusão de motorista zera esta coluna, nunca apaga a conversa.
    driver_id = db.Column(db.Integer, db.ForeignKey('drivers.id'))

    # aberta / pendente / resolvida
    status = db.Column(db.String(20), default=CONVERSATION_STATUS_OPEN,
                       server_default=db.text("'aberta'"), nullable=False, index=True)

    # Atribuição. Fonte de verdade da Fase 2; Driver.whatsapp_assigned_to passa
    # a ser cópia legada (mantida em sincronia, nunca lida como verdade).
    assigned_agent_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    assigned_at       = db.Column(db.DateTime)

    # bot / humano — espelha Driver.whatsapp_mode ('auto'/'manual') por contato.
    handling_mode = db.Column(db.String(10), default='auto',
                              server_default=db.text("'auto'"), nullable=False)

    # NOT NULL de propósito. A fila da Fase 2 ordena por esta coluna, e nulo
    # ordena diferente nos dois bancos: em ORDER BY ... DESC o PostgreSQL põe
    # NULL primeiro e o SQLite põe por último. Sem nulo, não há divergência.
    # Registro de origem desconhecida recebe a época (1970), que afunda na
    # ordenação nos dois dialetos.
    last_activity_at = db.Column(db.DateTime, nullable=False,
                                 default=datetime.utcnow,
                                 server_default=db.text('CURRENT_TIMESTAMP'),
                                 index=True)
    resolved_at      = db.Column(db.DateTime)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow,
                                 onupdate=datetime.utcnow)

    driver = db.relationship('Driver', backref='conversations')
    assigned_agent = db.relationship('User', foreign_keys=[assigned_agent_id],
                                     backref='assigned_conversations')

    @property
    def is_active(self):
        return self.status in CONVERSATION_ACTIVE_STATUSES

    def __repr__(self):
        return f'<Conversation {self.id} {self.contact_phone} {self.status}>'


# Ações registradas no histórico de uma conversa (Fase 2).
CONVERSATION_EVENT_ACTIONS = (
    'assumiu',         # atendente pegou conversa livre
    'transferiu',      # dono ou admin passou para outro atendente
    'liberou',         # dono devolveu para a fila humana
    'devolveu_bot',    # conversa voltou para o EMA
    'escalou_humano',  # o EMA pediu atendimento humano
    'resolveu',
    'reabriu',
)


class ConversationEvent(db.Model):
    """
    Quem fez o quê numa conversa, e quando.

    Tabela nova: o db.create_all() cria nos dois bancos, sem migração de
    coluna. Serve para rastrear transferências e é a base dos relatórios por
    atendente da Fase 4.

    ator_id nulo significa que a ação foi do sistema, por exemplo o EMA
    escalando para humano.
    """
    __tablename__ = 'conversation_events'

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('conversations.id'),
                                nullable=False, index=True)
    acao = db.Column(db.String(20), nullable=False)
    de_agente_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    para_agente_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    ator_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                           server_default=db.text('CURRENT_TIMESTAMP'), index=True)

    conversation = db.relationship(
        'Conversation',
        backref=db.backref('eventos', order_by='ConversationEvent.id'),
    )

    def __repr__(self):
        return f'<ConversationEvent {self.id} conv={self.conversation_id} {self.acao}>'


# Estados de um anexo recebido (Fase 3).
ANEXO_PENDENTE = 'pendente'            # registrado, bytes ainda não processados
ANEXO_ARMAZENADO = 'armazenado'        # no S3, pronto para exibir
ANEXO_INDISPONIVEL = 'indisponivel'    # S3 não configurado; só metadados
ANEXO_BLOQUEADO = 'bloqueado'          # tipo ou tamanho não aceito
ANEXO_ERRO = 'erro'                    # falha ao baixar ou gravar


class MessageAttachment(db.Model):
    """
    Arquivo recebido junto com uma mensagem de WhatsApp: foto de CNH, CRLV,
    comprovante em PDF.

    Os bytes vão só para o S3, nunca para o disco local, que some a cada
    deploy. Sem S3 configurado o anexo fica registrado como indisponível,
    com a referência do provedor guardada para um reprocessamento futuro.

    Tabela nova: o db.create_all() cria nos dois bancos, sem migração de
    coluna.
    """
    __tablename__ = 'message_attachments'

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('whatsapp_messages.id'),
                           nullable=False, index=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('conversations.id'), index=True)

    status = db.Column(db.String(20), nullable=False, default=ANEXO_PENDENTE,
                       server_default=db.text("'pendente'"))
    storage_key = db.Column(db.String(300))
    content_type = db.Column(db.String(100))
    size_bytes = db.Column(db.Integer)
    original_name = db.Column(db.String(200))
    provider = db.Column(db.String(20))       # twilio | evolution
    provider_ref = db.Column(db.Text)         # URL da Twilio ou chave da Evolution
    error_msg = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                           server_default=db.text('CURRENT_TIMESTAMP'))

    message = db.relationship('WhatsAppMessage', backref='anexos')

    def __repr__(self):
        return f'<MessageAttachment {self.id} msg={self.message_id} {self.status}>'
