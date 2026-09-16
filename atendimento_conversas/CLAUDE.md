# Módulo: Central de Atendimento (atendimento_conversas)

Instruções para o Claude Code construir a central de atendimento de WhatsApp
própria do sistema Emalog, integrada à Twilio, substituindo o Chatwoot na aba
"Conversas".

## Objetivo

Construir, dentro do próprio sistema Emalog, uma central de atendimento de
WhatsApp para ~5 atendentes simultâneos, com fila compartilhada, atribuição de
conversa e relatórios. O WhatsApp entra e sai pela Twilio. Nada de iframe, nada
de Chatwoot — a tela é nossa, no mesmo domínio, sem X-Frame-Options.

## Regras de arquitetura (não violar)

1. Núcleo, modelos e utilitários compartilhados ficam em `infraestrutura_critica/`.
   Importar de lá: `from infraestrutura_critica.models import ...`,
   `from infraestrutura_critica.app import db, socketio`.
2. Este módulo só contém lógica de atendimento. Rotas em
   `atendimento_conversas/`, utilitários específicos em
   `atendimento_conversas/utils/`.
3. Nomes de pasta em snake_case ASCII (Python não importa pasta com espaço/acento).
4. Blueprint registrado condicionalmente em `infraestrutura_critica/app.py`
   (padrão dos outros módulos).
5. Segredos NUNCA no código — sempre via variável de ambiente, declarada em
   `.ebextensions/session-secret.config`.

## O que reaproveitar (não recriar)

- Modelo `WhatsAppMessage` (infraestrutura_critica/models.py): já tem `direction`,
  `source`, `external_message_id`, `status`, FK para freight/driver/user. Estender,
  não substituir.
- A tela de Conversas anterior (histórico do git): tinha lista de conversas,
  thread de mensagens, campo de envio e painel de contexto do frete. Serve de
  base para a UI de UM atendente. NÃO cobre fila/atribuição/relatório.
- `socketio` já configurado em `infraestrutura_critica/app.py` — usar para o
  tempo real, não instalar outra lib.
- Twilio: conta já existe; credenciais entram via env (ver .ebextensions).

## O que é construção nova (o grosso do trabalho)

- Recebimento Twilio: webhook que entende o payload da Twilio (diferente de
  Evolution/Chatwoot), valida a assinatura, associa ao motorista/frete, grava.
- Envio Twilio: camada que troca as chamadas antigas (Evolution) por Twilio.
- Fila compartilhada em tempo real: o ponto crítico. Ver Fase 2.
- Atribuição e handoff entre atendentes.
- Relatórios de atendimento.
- Estados de mensagem (lida/não lida), notificação, busca, anexos.

## Fases (construir e validar uma de cada vez)

Ver PLANO_DE_FASES.md neste diretório. Não pular fase. Cada fase termina com
o app subindo (`from infraestrutura_critica.main import app`) e um teste real
do que foi construído antes de seguir.

## Variáveis de ambiente necessárias (declarar em .ebextensions)

- TWILIO_ACCOUNT_SID
- TWILIO_AUTH_TOKEN
- TWILIO_WHATSAPP_NUMBER   (formato: whatsapp:+55...)
- TWILIO_WEBHOOK_TOKEN     (validação do webhook de entrada)

## Validação obrigatória a cada mudança

1. `python3 -m py_compile` em todos os .py alterados.
2. Boot real: subir a app e confirmar que os blueprints registram.
3. Para a fila em tempo real (Fase 2): testar com DOIS clientes simultâneos
   (duas sessões de navegador), não só um. O bug clássico dessa parte só
   aparece com dois atendentes agindo ao mesmo tempo.

## Cuidados de segurança

- Validar assinatura do webhook Twilio (X-Twilio-Signature) — sem isso, qualquer
  um posta mensagem falsa no sistema.
- Não logar conteúdo de mensagem em texto puro em log de produção.
- Anexos (foto de CNH/CRLV) vão para o S3 (infraestrutura_critica/utils/storage.py),
  nunca para o filesystem local (some no deploy).
