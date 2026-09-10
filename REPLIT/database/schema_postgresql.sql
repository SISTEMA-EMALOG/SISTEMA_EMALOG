-- EMALOG - Schema PostgreSQL/AWS Aurora
-- Gerado exclusivamente dos modelos SQLAlchemy; não contém dados.
-- Execute em um banco vazio com um usuário autorizado a criar tabelas.

CREATE TABLE users (
	id SERIAL NOT NULL, 
	username VARCHAR(64) NOT NULL, 
	email VARCHAR(120) NOT NULL, 
	password_hash VARCHAR(256) NOT NULL, 
	role VARCHAR(20) NOT NULL, 
	active BOOLEAN, 
	first_login BOOLEAN, 
	must_change_password BOOLEAN, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	last_login TIMESTAMP WITHOUT TIME ZONE, 
	client_id INTEGER, 
	PRIMARY KEY (id), 
	UNIQUE (username), 
	UNIQUE (email)
);

CREATE TABLE clients (
	id SERIAL NOT NULL, 
	company_name VARCHAR(100) NOT NULL, 
	cnpj VARCHAR(18) NOT NULL, 
	trade_name VARCHAR(100), 
	phone VARCHAR(20) NOT NULL, 
	email VARCHAR(120) NOT NULL, 
	cep VARCHAR(10), 
	street VARCHAR(200), 
	number VARCHAR(10), 
	complement VARCHAR(100), 
	neighborhood VARCHAR(100), 
	city VARCHAR(100), 
	state VARCHAR(2), 
	address TEXT, 
	notification_emails TEXT, 
	contacts TEXT, 
	active BOOLEAN, 
	is_active BOOLEAN, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	created_by INTEGER, 
	PRIMARY KEY (id), 
	UNIQUE (cnpj)
);

CREATE TABLE drivers (
	id SERIAL NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	cpf VARCHAR(14), 
	rg VARCHAR(20), 
	birth_date DATE, 
	phone VARCHAR(20) NOT NULL, 
	email VARCHAR(120), 
	cep VARCHAR(10), 
	street VARCHAR(200), 
	number VARCHAR(10), 
	complement VARCHAR(100), 
	neighborhood VARCHAR(100), 
	city VARCHAR(100), 
	state VARCHAR(2), 
	cnh_expiry DATE, 
	antt_number VARCHAR(20), 
	truck_type VARCHAR(20), 
	has_tracker BOOLEAN, 
	tracker_type VARCHAR(20), 
	vehicle_plate VARCHAR(10), 
	vehicle_model VARCHAR(50), 
	vehicle_year INTEGER, 
	bank_name VARCHAR(50), 
	agency VARCHAR(10), 
	account VARCHAR(20), 
	pix_key VARCHAR(100), 
	cnh_document VARCHAR(255), 
	crlv_document VARCHAR(255), 
	antt_document VARCHAR(255), 
	address_proof VARCHAR(255), 
	vehicle_photo VARCHAR(255), 
	cavalinho_plate VARCHAR(10), 
	cavalinho_model VARCHAR(50), 
	cavalinho_year INTEGER, 
	cavalinho_crlv VARCHAR(255), 
	active BOOLEAN, 
	is_active BOOLEAN, 
	availability_status VARCHAR(20), 
	validated BOOLEAN, 
	whatsapp_mode VARCHAR(10) DEFAULT 'auto' NOT NULL, 
	whatsapp_assigned_to INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	created_by INTEGER, 
	PRIMARY KEY (id), 
	UNIQUE (cpf), 
	FOREIGN KEY(whatsapp_assigned_to) REFERENCES users (id), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);

CREATE TABLE chat_messages (
	id SERIAL NOT NULL, 
	room VARCHAR(50) NOT NULL, 
	sender_id INTEGER NOT NULL, 
	message_content TEXT NOT NULL, 
	message_type VARCHAR(20), 
	file_path VARCHAR(255), 
	sent_at TIMESTAMP WITHOUT TIME ZONE, 
	is_read BOOLEAN, 
	PRIMARY KEY (id), 
	FOREIGN KEY(sender_id) REFERENCES users (id)
);

CREATE INDEX ix_chat_messages_room ON chat_messages (room);

CREATE INDEX ix_chat_messages_is_read ON chat_messages (is_read);

CREATE TABLE audit_logs (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	action VARCHAR(20) NOT NULL, 
	table_name VARCHAR(50) NOT NULL, 
	record_id INTEGER NOT NULL, 
	old_values TEXT, 
	new_values TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE crm_leads (
	id SERIAL NOT NULL, 
	company_name VARCHAR(200) NOT NULL, 
	cnpj VARCHAR(20), 
	contact_name VARCHAR(150), 
	contact_phone VARCHAR(50), 
	contact_email VARCHAR(150), 
	city VARCHAR(150), 
	state VARCHAR(2), 
	segment VARCHAR(255), 
	source VARCHAR(50), 
	status VARCHAR(30), 
	estimated_monthly_value FLOAT, 
	notes TEXT, 
	assigned_to INTEGER, 
	converted_client_id INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	updated_at TIMESTAMP WITHOUT TIME ZONE, 
	industry_type VARCHAR(255), 
	website VARCHAR(300), 
	employee_count VARCHAR(50), 
	zip_code VARCHAR(10), 
	address VARCHAR(300), 
	import_batch VARCHAR(100), 
	ai_enriched BOOLEAN, 
	is_hot BOOLEAN, 
	operational_count INTEGER, 
	last_freight_ref VARCHAR(200), 
	whatsapp VARCHAR(50), 
	street VARCHAR(200), 
	number_addr VARCHAR(20), 
	complement VARCHAR(200), 
	neighborhood VARCHAR(200), 
	next_contact_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(assigned_to) REFERENCES users (id), 
	FOREIGN KEY(converted_client_id) REFERENCES clients (id)
);

CREATE TABLE lead_batches (
	id SERIAL NOT NULL, 
	batch_id VARCHAR(100) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	filename VARCHAR(300), 
	source VARCHAR(50), 
	assigned_to INTEGER, 
	total_leads INTEGER, 
	created_by INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	notes TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (batch_id), 
	FOREIGN KEY(assigned_to) REFERENCES users (id), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);

CREATE TABLE assistant_messages (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	role VARCHAR(10) NOT NULL, 
	text TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE opportunities (
	id SERIAL NOT NULL, 
	lead_id INTEGER NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	stage VARCHAR(30), 
	value FLOAT, 
	freight_origin VARCHAR(200), 
	freight_destination VARCHAR(200), 
	cargo_type VARCHAR(100), 
	vehicle_type VARCHAR(50), 
	frequency VARCHAR(50), 
	probability INTEGER, 
	expected_close_date DATE, 
	lost_reason VARCHAR(300), 
	notes TEXT, 
	assigned_to INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	updated_at TIMESTAMP WITHOUT TIME ZONE, 
	archived BOOLEAN DEFAULT 'false' NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(lead_id) REFERENCES crm_leads (id), 
	FOREIGN KEY(assigned_to) REFERENCES users (id)
);

CREATE TABLE ema_sessions (
	id SERIAL NOT NULL, 
	driver_id INTEGER NOT NULL, 
	status VARCHAR(20), 
	current_field VARCHAR(50), 
	history JSON, 
	staged_data JSON, 
	error_msg TEXT, 
	started_at TIMESTAMP WITHOUT TIME ZONE, 
	completed_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	updated_at TIMESTAMP WITHOUT TIME ZONE, 
	revalidation BOOLEAN, 
	last_driver_msg_at TIMESTAMP WITHOUT TIME ZONE, 
	reminder_sent_at TIMESTAMP WITHOUT TIME ZONE, 
	abandoned_reason VARCHAR(200), 
	PRIMARY KEY (id), 
	FOREIGN KEY(driver_id) REFERENCES drivers (id)
);

CREATE TABLE quotes (
	id SERIAL NOT NULL, 
	quote_number VARCHAR(50) NOT NULL, 
	client_id INTEGER NOT NULL, 
	origin_cep VARCHAR(10) NOT NULL, 
	origin_street VARCHAR(200), 
	origin_number VARCHAR(10), 
	origin_complement VARCHAR(100), 
	origin_neighborhood VARCHAR(100), 
	origin_city VARCHAR(100), 
	origin_state VARCHAR(2), 
	pickup_date DATE NOT NULL, 
	urgent_pickup BOOLEAN, 
	destination_cep VARCHAR(10) NOT NULL, 
	destination_street VARCHAR(200), 
	destination_number VARCHAR(10), 
	destination_complement VARCHAR(100), 
	destination_neighborhood VARCHAR(100), 
	destination_city VARCHAR(100), 
	destination_state VARCHAR(2), 
	delivery_deadline INTEGER, 
	vehicle_type VARCHAR(20) NOT NULL, 
	load_type VARCHAR(20) NOT NULL, 
	load_height FLOAT, 
	load_length FLOAT, 
	load_width FLOAT, 
	load_weight FLOAT NOT NULL, 
	load_volume FLOAT, 
	invoice_value FLOAT, 
	invoice_file VARCHAR(255), 
	cargo_items_json TEXT, 
	origin_cnpj VARCHAR(20), 
	origin_company VARCHAR(200), 
	destination_cnpj VARCHAR(20), 
	destination_company VARCHAR(200), 
	pickup_address_notes TEXT, 
	delivery_address_notes TEXT, 
	driver_cost FLOAT NOT NULL, 
	sale_value FLOAT NOT NULL, 
	client_counter_value FLOAT, 
	client_counter_message TEXT, 
	client_counter_at TIMESTAMP WITHOUT TIME ZONE, 
	operator_response_value FLOAT, 
	operator_response_message TEXT, 
	operator_response_at TIMESTAMP WITHOUT TIME ZONE, 
	negotiation_round INTEGER, 
	additional_info TEXT, 
	valid_until DATE NOT NULL, 
	status VARCHAR(20), 
	quoted_at TIMESTAMP WITHOUT TIME ZONE, 
	quoted_by INTEGER, 
	approved_at TIMESTAMP WITHOUT TIME ZONE, 
	approved_by INTEGER, 
	rejected_at TIMESTAMP WITHOUT TIME ZONE, 
	rejected_by INTEGER, 
	rejection_reason TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	updated_at TIMESTAMP WITHOUT TIME ZONE, 
	created_by INTEGER, 
	opportunity_id INTEGER, 
	is_multi_stop BOOLEAN DEFAULT 'false' NOT NULL, 
	stops_json TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (quote_number), 
	FOREIGN KEY(client_id) REFERENCES clients (id), 
	FOREIGN KEY(quoted_by) REFERENCES users (id), 
	FOREIGN KEY(approved_by) REFERENCES users (id), 
	FOREIGN KEY(rejected_by) REFERENCES users (id), 
	FOREIGN KEY(created_by) REFERENCES users (id), 
	FOREIGN KEY(opportunity_id) REFERENCES opportunities (id)
);

CREATE TABLE crm_activities (
	id SERIAL NOT NULL, 
	lead_id INTEGER, 
	opportunity_id INTEGER, 
	activity_type VARCHAR(30), 
	title VARCHAR(200), 
	notes TEXT, 
	scheduled_at TIMESTAMP WITHOUT TIME ZONE, 
	completed_at TIMESTAMP WITHOUT TIME ZONE, 
	is_done BOOLEAN, 
	outcome VARCHAR(500), 
	created_by INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(lead_id) REFERENCES crm_leads (id), 
	FOREIGN KEY(opportunity_id) REFERENCES opportunities (id), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);

CREATE TABLE freights (
	id SERIAL NOT NULL, 
	freight_number VARCHAR(30) NOT NULL, 
	quote_id INTEGER, 
	client_id INTEGER NOT NULL, 
	origin VARCHAR(200) NOT NULL, 
	destination VARCHAR(200) NOT NULL, 
	product VARCHAR(100) NOT NULL, 
	weight FLOAT NOT NULL, 
	agreed_price FLOAT NOT NULL, 
	driver_cost FLOAT, 
	selected_drivers TEXT, 
	assigned_driver_id INTEGER, 
	status VARCHAR(20), 
	origin_city VARCHAR(100), 
	origin_state VARCHAR(2), 
	destination_city VARCHAR(100), 
	destination_state VARCHAR(2), 
	pickup_date DATE, 
	delivery_date DATE, 
	whatsapp_sent BOOLEAN, 
	whatsapp_responses TEXT, 
	notes TEXT, 
	custom_whatsapp_message TEXT, 
	stops_json TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	updated_at TIMESTAMP WITHOUT TIME ZONE, 
	created_by INTEGER, 
	PRIMARY KEY (id), 
	UNIQUE (freight_number), 
	FOREIGN KEY(quote_id) REFERENCES quotes (id), 
	FOREIGN KEY(client_id) REFERENCES clients (id), 
	FOREIGN KEY(assigned_driver_id) REFERENCES drivers (id), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);

CREATE INDEX ix_freights_status ON freights (status);

CREATE TABLE quote_negotiations (
	id SERIAL NOT NULL, 
	quote_id INTEGER NOT NULL, 
	round_number INTEGER NOT NULL, 
	proposer_type VARCHAR(20) NOT NULL, 
	proposer_id INTEGER NOT NULL, 
	proposed_value FLOAT NOT NULL, 
	message TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(quote_id) REFERENCES quotes (id), 
	FOREIGN KEY(proposer_id) REFERENCES users (id)
);

CREATE TABLE payments (
	id SERIAL NOT NULL, 
	driver_id INTEGER, 
	freight_id INTEGER, 
	payment_type VARCHAR(30) NOT NULL, 
	amount FLOAT NOT NULL, 
	description VARCHAR(200), 
	status VARCHAR(20), 
	due_date DATE, 
	paid_date DATE, 
	milestone VARCHAR(50), 
	payment_70_status VARCHAR(20), 
	payment_30_status VARCHAR(20), 
	payment_70_date DATE, 
	payment_30_date DATE, 
	payment_date DATE NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	created_by INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(driver_id) REFERENCES drivers (id), 
	FOREIGN KEY(freight_id) REFERENCES freights (id), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);

CREATE INDEX ix_payments_payment_type ON payments (payment_type);

CREATE INDEX ix_payments_due_date ON payments (due_date);

CREATE TABLE whatsapp_messages (
	id SERIAL NOT NULL, 
	freight_id INTEGER, 
	driver_id INTEGER, 
	message_content TEXT NOT NULL, 
	phone_number VARCHAR(20) NOT NULL, 
	status VARCHAR(20), 
	sent_at TIMESTAMP WITHOUT TIME ZONE, 
	delivered_at TIMESTAMP WITHOUT TIME ZONE, 
	response_content TEXT, 
	response_at TIMESTAMP WITHOUT TIME ZONE, 
	direction VARCHAR(10) DEFAULT 'outbound' NOT NULL, 
	source VARCHAR(20) DEFAULT 'legacy' NOT NULL, 
	created_by INTEGER, 
	external_message_id VARCHAR(120), 
	PRIMARY KEY (id), 
	FOREIGN KEY(freight_id) REFERENCES freights (id), 
	FOREIGN KEY(driver_id) REFERENCES drivers (id), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);

CREATE UNIQUE INDEX ix_whatsapp_messages_external_message_id ON whatsapp_messages (external_message_id);

CREATE TABLE freight_documents (
	id SERIAL NOT NULL, 
	freight_id INTEGER NOT NULL, 
	document_type VARCHAR(50) NOT NULL, 
	filename VARCHAR(255) NOT NULL, 
	original_filename VARCHAR(255) NOT NULL, 
	file_path VARCHAR(500) NOT NULL, 
	file_size INTEGER, 
	uploaded_at TIMESTAMP WITHOUT TIME ZONE, 
	uploaded_by INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(freight_id) REFERENCES freights (id), 
	FOREIGN KEY(uploaded_by) REFERENCES users (id)
);

CREATE TABLE freight_status_logs (
	id SERIAL NOT NULL, 
	freight_id INTEGER NOT NULL, 
	old_status VARCHAR(30), 
	new_status VARCHAR(30) NOT NULL, 
	notes TEXT, 
	changed_by INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(freight_id) REFERENCES freights (id), 
	FOREIGN KEY(changed_by) REFERENCES users (id)
);

CREATE TABLE driver_ratings (
	id SERIAL NOT NULL, 
	freight_id INTEGER NOT NULL, 
	driver_id INTEGER NOT NULL, 
	rating INTEGER NOT NULL, 
	punctuality INTEGER, 
	cargo_care INTEGER, 
	communication INTEGER, 
	comment TEXT, 
	rated_by INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(freight_id) REFERENCES freights (id), 
	FOREIGN KEY(driver_id) REFERENCES drivers (id), 
	FOREIGN KEY(rated_by) REFERENCES users (id)
);

CREATE TABLE driver_bids (
	id SERIAL NOT NULL, 
	freight_id INTEGER NOT NULL, 
	driver_id INTEGER NOT NULL, 
	status VARCHAR(20), 
	driver_price NUMERIC(10, 2), 
	driver_message TEXT, 
	history JSON, 
	kanban_stage VARCHAR(30) DEFAULT 'awaiting_response' NOT NULL, 
	kanban_history JSON, 
	internal_notes TEXT, 
	closed_reason VARCHAR(300), 
	stage_changed_at TIMESTAMP WITHOUT TIME ZONE, 
	accepted_at TIMESTAMP WITHOUT TIME ZONE, 
	assigned_to INTEGER, 
	sent_at TIMESTAMP WITHOUT TIME ZONE, 
	responded_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	updated_at TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(freight_id) REFERENCES freights (id), 
	FOREIGN KEY(driver_id) REFERENCES drivers (id), 
	FOREIGN KEY(assigned_to) REFERENCES users (id)
);

CREATE INDEX ix_driver_bids_kanban_stage ON driver_bids (kanban_stage);

ALTER TABLE users ADD FOREIGN KEY(client_id) REFERENCES clients (id);

ALTER TABLE clients ADD FOREIGN KEY(created_by) REFERENCES users (id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_driver_bids_active_driver ON driver_bids (driver_id) WHERE status IN ('sent', 'responded', 'no_price');

CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_external_message ON whatsapp_messages (external_message_id) WHERE external_message_id IS NOT NULL;
