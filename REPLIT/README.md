# EMALOG — Sistema de Gestão Logística

Aplicação web em Python/Flask para operação logística da EMALOG.

## Principais módulos

- clientes e usuários de clientes;
- cotações e fretes com múltiplas coletas e entregas;
- motoristas, documentos e disponibilidade;
- Central de Contratação com Kanban e conversas WhatsApp;
- Agente EMA para cadastro e atualização de motoristas;
- financeiro, pagamentos 70/30 e relatórios;
- CRM, chat e notificações em tempo real;
- geração de PDFs e exportação de planilhas.

## Tecnologias

- Python 3.11;
- Flask, Flask-SQLAlchemy, Flask-Login e Flask-SocketIO;
- PostgreSQL em produção;
- Gunicorn;
- Evolution API para WhatsApp;
- Groq para agentes de texto;
- Gemini Vision para OCR;
- Replit Object Storage opcional.

## Arquivos importantes

- `main.py`: ponto de entrada da aplicação;
- `app.py`: fábrica Flask, extensões, blueprints e tarefas agendadas;
- `models.py`: modelos SQLAlchemy;
- `routes/`: rotas por módulo;
- `utils/`: serviços, integrações, migrações e regras compartilhadas;
- `templates/`: páginas Jinja;
- `static/`: CSS, JavaScript e recursos públicos;
- `config.py`: configurações por ambiente.

## Instalação local

### 1. Pré-requisitos

- Python 3.11 ou superior;
- PostgreSQL 14 ou superior;
- Git.

### 2. Clonar e instalar

```bash
git clone URL_DO_REPOSITORIO
cd emalog
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

No Windows:

```powershell
.\.venv\Scripts\activate
```

### 3. Configurar o ambiente

Copie o arquivo de exemplo:

```bash
cp .env.example .env
```

Carregue as variáveis com a ferramenta utilizada no ambiente de execução. O
projeto não lê `.env` automaticamente, portanto também é possível exportá-las:

```bash
export SESSION_SECRET="uma-chave-segura"
export DATABASE_URL="postgresql://usuario:senha@localhost:5432/emalog"
```

Nunca envie o arquivo `.env` ao GitHub.

### 4. Criar o banco

Crie um banco PostgreSQL vazio e configure `DATABASE_URL`. No primeiro startup,
o projeto cria as tabelas e executa as migrações de compatibilidade existentes.

Para desenvolvimento sem PostgreSQL, a ausência de `DATABASE_URL` ativa SQLite
em `instance/emalog.db`. PostgreSQL continua sendo o ambiente recomendado.

### 5. Executar

Desenvolvimento:

```bash
python main.py
```

Execução semelhante à produção:

```bash
gunicorn --bind 0.0.0.0:5000 \
  --worker-class gthread --workers 1 --threads 8 --timeout 0 main:app
```

Acesse `http://localhost:5000`.

## Variáveis de ambiente

Consulte `.env.example`. As integrações externas são opcionais para abrir a
aplicação, mas suas funções dependem das respectivas configurações:

| Variável | Finalidade |
| --- | --- |
| `SESSION_SECRET` | Assinatura segura de sessão e CSRF |
| `DATABASE_URL` | Conexão PostgreSQL |
| `GROQ_API_KEY` | Agentes de texto |
| `AI_INTEGRATIONS_GEMINI_API_KEY` | OCR Gemini |
| `AI_INTEGRATIONS_GEMINI_BASE_URL` | Endpoint Gemini |
| `EVOLUTION_API_URL` | URL da Evolution API |
| `EVOLUTION_API_KEY` | Autenticação e proteção do webhook |
| `EVOLUTION_INSTANCE` | Instância WhatsApp |
| `MAIL_*` | Envio de e-mails |
| `DEFAULT_OBJECT_STORAGE_BUCKET_ID` | Armazenamento persistente no Replit |
| `SOCKETIO_ALLOWED_ORIGINS` | Origens autorizadas no Socket.IO |

## Evolution API

Após configurar as variáveis `EVOLUTION_*`, entre na Central de Contratação,
abra **Cadastros EMA** e use **Sincronizar WhatsApp**. Isso registra o webhook
autenticado da aplicação na instância.

## Publicação no Replit

O arquivo `.replit` inclui o comando de desenvolvimento e a configuração de
deployment. No novo Repl:

1. importe o repositório do GitHub;
2. configure as variáveis em **Secrets**;
3. adicione um banco PostgreSQL;
4. configure o Object Storage, se desejar persistência de documentos;
5. execute o workflow **Start application**.

## Dados e segurança

Esta distribuição não inclui:

- banco de dados;
- backups;
- documentos enviados;
- planilhas de clientes ou motoristas;
- credenciais e chaves;
- diretórios internos do Replit;
- materiais de referência usados durante o desenvolvimento.

Os dados de produção devem ser transferidos separadamente, por procedimento
seguro e autorizado. Não envie dumps ou documentos operacionais ao GitHub.