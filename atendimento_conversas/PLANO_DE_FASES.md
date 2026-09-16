# Plano de Fases — Central de Atendimento

Construir na ordem. Cada fase entrega algo testável e não começa antes da
anterior estar validada. A Fase 2 é a de maior risco — reservar mais tempo.

## Fase 0 — Fundação de dados
- Estender/confirmar o modelo de conversa. Precisamos de uma entidade
  `Conversation` (agrupa mensagens de um contato) separada de `WhatsAppMessage`
  (cada mensagem). Hoje só existe a mensagem solta.
- Campos de conversa: contato (telefone/motorista), status (aberta/pendente/
  resolvida), atendente_atribuido (FK user, nullable), última_atividade.
- Migração de banco. Testar contra SQLite (dev) e PostgreSQL (prod) — não usar
  SQL exclusivo de um dialeto (lição já aprendida: date_trunc quebrou em SQLite).
- Fim da fase: modelos criados, migração roda nos dois bancos.

## Fase 1 — Entrada e saída via Twilio (1 atendente)
- Webhook de recebimento Twilio: valida assinatura, cria/atualiza Conversation,
  grava WhatsAppMessage (direction=inbound), emite evento socketio.
- Camada de envio Twilio: função send_whatsapp(to, body) usando as credenciais
  de ambiente; grava a mensagem (direction=outbound).
- Reativar a tela de Conversas antiga (do git), ligada nesta nova fonte de dados.
- Fim da fase: uma pessoa consegue ver a conversa chegar e responder, ponta a
  ponta, pelo sandbox da Twilio.

## Fase 2 — Fila compartilhada e atribuição (CRÍTICO)
- Vários atendentes veem a mesma fila de conversas abertas.
- Um atendente "assume" uma conversa: ela sai da fila livre dos outros (lock).
- Tempo real via socketio: quando A assume, some da tela de B, C, D imediatamente.
- Tratar corrida: dois atendentes clicando "assumir" na mesma conversa no mesmo
  instante — só um ganha, o outro recebe aviso. Usar transação/lock no banco,
  não confiar só no evento de socket.
- Handoff: passar conversa de um atendente para outro; passar do bot para humano.
- Fim da fase: TESTAR COM DUAS SESSÕES simultâneas. Este é o ponto que quebra
  se testado só com uma pessoa.

## Fase 3 — Estados e usabilidade
- Lida/não lida por conversa, contador de não lidas na fila.
- Notificação de nova mensagem (som/visual).
- Busca de conversa por nome/telefone.
- Anexos: receber imagem (CNH/CRLV) da Twilio e salvar no S3; exibir na thread.
- Fim da fase: uso confortável no dia a dia.

## Fase 4 — Relatórios
- Por atendente: nº de conversas atendidas, tempo médio de resposta, no período.
- Volume por dia/semana, conversas resolvidas vs abertas.
- Fim da fase: supervisor consegue medir a operação.

## Fase 5 — Chatbot de regras (fase futura separada)
- Máquina de estados (sem IA) para captação de motorista e oferta de frete.
- Substitui a lógica de linguagem natural do EMA atual.
- Handoff automático bot -> humano quando sai do fluxo previsto.
- Não iniciar antes das fases 0-4 estarem estáveis.

## Ordem de risco (onde investir atenção)
1. Fase 2 (fila em tempo real) — alto risco, testar com concorrência real.
2. Fase 1 (webhook Twilio) — médio, validar assinatura.
3. Demais — baixo a médio.
