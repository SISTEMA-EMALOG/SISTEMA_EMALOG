# Central de Atendimento — arranque

Ponto de partida para construir a central de atendimento de WhatsApp própria do
Emalog no Claude Code.

## Leia nesta ordem
1. `CLAUDE.md` — regras de arquitetura, o que reaproveitar, o que é novo.
2. `PLANO_DE_FASES.md` — as fases, em ordem, com a Fase 2 marcada como crítica.

## Primeiro comando no Claude Code
Abrir o repositório e pedir ao Claude Code para começar pela **Fase 0**
(modelagem de dados), lendo antes `CLAUDE.md` e `PLANO_DE_FASES.md` deste
diretório. Não deixar ele pular para envio/recebimento antes dos modelos.

## Contexto que o Claude Code precisa ter em mãos
- A app sobe por `infraestrutura_critica/main.py` (`main:app` no Procfile).
- Modelos em `infraestrutura_critica/models.py` — já existe `WhatsAppMessage`,
  `EmaSession`, `ChatMessage`. Estender, não duplicar.
- `db` e `socketio` vêm de `infraestrutura_critica/app.py`.
- Blueprints são registrados em `infraestrutura_critica/app.py`, no mesmo bloco
  dos outros módulos.
- Credenciais Twilio já têm placeholders em
  `.ebextensions/session-secret.config` (linhas TWILIO_*).

## Não repetir erros já cometidos neste projeto
- Procfile SEMPRE na raiz do zip de deploy.
- Pastas de dados (instance/, uploads/) com permissão de escrita.
- Nada de SQL exclusivo de um dialeto (date_trunc quebrou em SQLite).
- Bump do parâmetro de versão (?v=) ao alterar arquivo .js estático, senão o
  navegador serve versão em cache.
- Anexos no S3, não no filesystem local.
