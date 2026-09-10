# EMALOG - Sistema de Gestão Logística

## Overview

EMALOG is a comprehensive logistics management system built with Python Flask. The system manages freight operations, including driver management, client relationships, quote generation, freight tracking, and financial controls. It features real-time communication capabilities, automated WhatsApp integration for driver notifications, and comprehensive reporting tools.

The application serves three primary user types: administrators (full system access), operators (operational management), and clients (view-only access to their data). The system handles the complete freight lifecycle from initial quote generation through delivery completion and payment processing.

## User Preferences

Preferred communication style: Simple, everyday language.

## Agente EMA — WhatsApp AI (May 2026)

### Sistema de Enriquecimento de Dados de Motoristas via WhatsApp
- ✅ `utils/evolution_api.py` — cliente Evolution API: `send_text()`, `download_media()`, `get_connection_status()`, `normalize_phone()`
- ✅ `utils/ema_agent.py` — core do agente: conversa via Gemini 2.5 Flash, validação CPF (dígitos verificadores), lookup CEP via ViaCEP, extração estruturada de dados, controle de campos faltantes e % de completude
- ✅ `models.py` → `EmaSession` — rastreia sessão por motorista: status, histórico JSON, campo atual, staged_data, timestamps
- ✅ `routes/ema_agent.py` — blueprint `/ema/*` com rotas: dashboard, start, webhook, session detail, send manual, reset, restart, api/status
- ✅ `templates/ema/index.html` — painel admin: tabela com % completude, badges de status, filtros, seleção múltipla, botão "Iniciar EMA"
- ✅ `templates/ema/session.html` — view de conversa: chat bubbles estilo WhatsApp, checklist de campos, envio manual, restart
- ✅ Navegação: link "Agente EMA" adicionado na sidebar para admin/operador
- ✅ Webhook `POST /ema/webhook` pronto para receber mensagens do Evolution API (texto + mídia)
- ✅ Modo simulado: quando Evolution API não está configurada, mensagens são logadas (não enviadas)

### Campos coletados pela EMA (24 no total)
CPF, RG, data nascimento, email, CEP (auto-preenche endereço via ViaCEP), número, complemento, tipo caminhão, placa, modelo, ano, rastreador, vencimento CNH, ANTT, dados bancários (banco/agência/conta/PIX), fotos: CNH + CRLV + comprovante residência + veículo

### Variáveis de ambiente necessárias (Evolution API)
- `EVOLUTION_API_URL` — URL do servidor Evolution API (ex: https://evo.seudominio.com)
- `EVOLUTION_API_KEY` — chave de autenticação
- `EVOLUTION_INSTANCE` — nome da instância (padrão: emalog)
- Webhook a configurar: `{APP_URL}/ema/webhook` com evento `messages.upsert`

## Backup & Correções (May 2026)

### Sistema de Backup Automático (Novo)
- ✅ `utils/backup.py` — utilitário de backup completo (JSON legível + cópia `.db` fiel)
- ✅ Backup automático diário às 02h00 UTC via APScheduler (daemon thread)
- ✅ Limite de 30 backups diários com rotação automática dos mais antigos
- ✅ Senhas mascaradas no JSON por segurança (`***REDACTED***`)
- ✅ Rotas admin: `/admin/backup` (listagem), criar, download JSON/SQLite, deletar, API status
- ✅ Template `templates/admin/backup.html` com UI completa de gerenciamento
- ✅ Botão "Backup" no painel admin principal
- ✅ Backup inicial criado e testado com sucesso (629 registros, 486KB JSON)
- ✅ `.gitignore` criado para excluir `backups/`, `instance/`, uploads

### Correções de Bugs (May 2026)
- ✅ Task 3: CSRF token adicionado ao fetch `update-status` em fretes (era bloqueado pela proteção CSRF)
- ✅ Task 5: Tab "Usuários Portal" adicionado na página de detalhe do cliente — admin/operador vê usuários e pode criar/gerenciar diretamente
- ✅ Task 4: Email de finalização já implementado em `utils/email_service.py` — usa MAIL_* env vars configuradas

### PostgreSQL (Status)
- ⚠️ Neon.tech endpoint retorna "password authentication failed" — credenciais expiradas por inatividade
- ✅ Código de reconexão automática pronto: tentará PG* vars → DATABASE_URL → SQLite fallback
- 📋 Quando renovar credenciais: a app detecta automaticamente e migra para PostgreSQL sem alteração de código

## Security Hardening (May 2026)

### Implementado
- ✅ CSRF protection: token gerado por sessão, validado em todos os POST requests via `before_request`
- ✅ CSRF auto-inject JS: injeta `_csrf_token` em todos os formulários e `X-CSRFToken` em todos os fetch()/XHR automaticamente
- ✅ Proteção contra open-redirect no login (`safe_redirect_url`)
- ✅ Rate limiting de login: máximo 10 tentativas por 5 minutos por IP
- ✅ Política de senha: mínimo 8 caracteres com letras e números
- ✅ Session cookie hardening: `HttpOnly`, `SameSite=Lax`, `Secure`, lifetime 12h
- ✅ Security headers: CSP, X-Frame-Options, HSTS, Referrer-Policy, Permissions-Policy
- ✅ Controle de acesso: motoristas e clientes restritos por role em todas as rotas
- ✅ `download_document` restrito a staff (admin/operador)
- ✅ Template 403 criado para acesso negado
- ✅ SocketIO CORS configurável via `SOCKETIO_ALLOWED_ORIGINS` env var

## Recent Changes (August 2025)

### Sistema de Motoristas Brasileiro (Completed)
- ✅ Implementado sistema completo de cadastro de motoristas com características brasileiras
- ✅ CEP com preenchimento automático via API ViaCEP
- ✅ Validação e formatação de CPF com verificação de dígitos
- ✅ Tipos de caminhão predefinidos (3/4, VLC, toco, truck, bitruck, carreta)
- ✅ Removido campo número da CNH conforme solicitado
- ✅ Endereço completo separado por campos individuais
- ✅ Database migrado com novos campos brasileiros

### Templates e Navegação (Completed)
- ✅ Todos os templates principais convertidos para dashboard_base.html
- ✅ Sistema de navegação unificado com botões "Voltar ao Dashboard"
- ✅ Dashboard com contadores dinâmicos carregando dados reais do banco
- ✅ Templates criados/atualizados: motoristas, clientes, cotações, fretes, financeiro, chat, relatórios
- ✅ Remoção de referências a rotas de exportação inexistentes

### Funcionalidades Ativas
- ✅ Sistema de autenticação (admin: patrick.souza@emalog.com.br / $Geraldo87)
- ✅ Cadastro completo de motoristas com validações brasileiras
- ✅ Cadastro completo de clientes com integração CNPJ
- ✅ Dashboard funcional com estatísticas em tempo real
- ✅ Navegação consistente entre todos os módulos
- ✅ Templates responsivos com TailwindCSS
- ✅ Painel administrativo completo com gerenciamento de usuários
- ✅ Sistema de senhas padrão com troca obrigatória no primeiro login

### Sistema de Cotações Brasileiro (Novo - Agosto 2025)
- ✅ Modelo de cotação completamente reformulado para logística brasileira
- ✅ Integração CEP para origem e destino com preenchimento automático
- ✅ Seleção de tipos de veículo brasileiros (VUC, 3/4, truck, carreta, van, fiorino, toco)
- ✅ Dados completos da carga (dedicado/fracionado, dimensões, peso, valor NF)
- ✅ Upload de nota fiscal em PDF
- ✅ Campos separados para custo motorista e valor de venda
- ✅ Cálculo automático de margem de lucro
- ✅ Datas de coleta e entrega com opção de urgência
- ✅ Sistema de aprovação/rejeição de cotações
- ✅ Templates modernos com visualização detalhada
- ✅ Geração automática de frete ao aprovar cotação
- ✅ Mensagens WhatsApp para motoristas com valor correto e dados da carga
- ✅ Sistema de edição de mensagens WhatsApp completamente refeito
- ✅ Preview em tempo real com dados corretos do frete
- ✅ Editor integrado na tela de seleção de motoristas
- ✅ Variáveis dinâmicas expandidas: nome, custo motorista, dimensões, tipo carga
- ✅ Salvar mensagens personalizadas via AJAX
- ✅ Reset para mensagem padrão com confirmação
- ✅ Valor correto para motorista (driver_cost) em vez de sale_value

### Sistema de Pagamentos Automáticos (Novo - Agosto 2025)
- ✅ Modelo Payment expandido com novos campos brasileiros
- ✅ Sistema de atribuição de fretes com pagamentos automáticos
- ✅ Criação automática de 70% no carregamento + 30% na finalização  
- ✅ Interface financeira com tabela de pagamentos pendentes
- ✅ Uso correto do valor do motorista (driver_cost) para cálculos
- ✅ Status de pagamento (pendente/pago/cancelado)
- ✅ Integração completa entre fretes e módulo financeiro

## System Architecture

### Backend Architecture
- **Framework**: Flask web application with SQLAlchemy ORM for database operations
- **Database**: SQLite for development with PostgreSQL migration capability via configurable DATABASE_URL
- **Authentication**: Flask-Login for session management with role-based access control (admin, operador, cliente)
- **Real-time Communication**: Socket.IO for live chat functionality between users
- **File Upload**: Werkzeug file handling with 16MB size limit for document management

### Frontend Architecture
- **Styling**: TailwindCSS utility-first framework for responsive design
- **Icons**: FontAwesome for consistent iconography
- **JavaScript**: Vanilla JavaScript with Chart.js for data visualization
- **Templates**: Jinja2 templating with modular component structure
- **Real-time Features**: Socket.IO client for chat and notifications

### Data Models and Business Logic
- **User Management**: Role-based user system with client association for restricted access
- **Driver Management**: Comprehensive driver profiles including personal data, vehicle information, and banking details
- **Client Management**: Corporate client records with CNPJ validation and multiple contact management
- **Quote System**: Automated pricing with configurable margins and PDF generation
- **Freight Management**: Complete freight lifecycle tracking with driver assignment and status updates
- **Financial Control**: Payment tracking, advances, and driver balance management

### API Integrations
- **CNPJ Validation**: Integration with ReceitaWS API for Brazilian company data validation
- **WhatsApp Messaging**: Configurable WhatsApp API for automated driver notifications
- **Document Generation**: ReportLab for PDF generation and pandas for Excel export functionality

### Security and Configuration
- **Environment Variables**: Sensitive configuration through environment variables with fallback defaults
- **Proxy Support**: ProxyFix middleware for deployment behind reverse proxies
- **File Security**: Secure filename handling and upload directory management
- **Session Management**: Configurable session secrets with development fallbacks

### Modular Route Structure
The application uses Blueprint-based route organization with separate modules for:
- Authentication and user management
- Driver operations and document handling
- Client relationship management
- Quote generation and approval workflows  
- Freight tracking and driver assignment
- Financial reporting and payment processing
- Real-time chat communication

## External Dependencies

### Core Framework Dependencies
- **Flask**: Web application framework with SQLAlchemy database integration
- **Flask-Login**: User session management and authentication
- **Flask-SocketIO**: Real-time bidirectional communication for chat features
- **Werkzeug**: WSGI utility library for file uploads and security

### Database and ORM
- **SQLAlchemy**: Database ORM with support for SQLite and PostgreSQL
- **Database**: Configurable via DATABASE_URL environment variable (defaults to SQLite)

### Document and Report Generation
- **ReportLab**: PDF generation for quotes and reports
- **Pandas**: Excel file import/export functionality for bulk operations
- **OpenPyXL**: Excel file manipulation engine

### Frontend Assets
- **TailwindCSS**: Utility-first CSS framework via CDN
- **FontAwesome**: Icon library for consistent UI elements
- **Chart.js**: Data visualization for dashboards and reports
- **Socket.IO Client**: Real-time communication client library

### External APIs
- **ReceitaWS API**: Brazilian CNPJ validation service (https://receitaws.com.br/v1/cnpj/)
- **WhatsApp API**: Configurable WhatsApp messaging service for driver notifications
- **Email Services**: SMTP configuration for email notifications

### Development and Deployment
- **Python 3.x**: Runtime environment
- **Environment Variables**: Configuration for API tokens, database connections, and service endpoints
- **File System**: Local file storage for uploads with configurable directory structure