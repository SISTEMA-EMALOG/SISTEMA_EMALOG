# Pendências da Central de Atendimento

Lista do que ficou para depois, levantada durante as Fases 0 a 4, em
17/09/2026. Cada item diz o que é, por que importa e o que fazer. Os itens
marcados como **verificado** foram reproduzidos ou confirmados no código; os
marcados como **a verificar** são suspeitas que ainda precisam de prova.

Ao resolver um item, apague-o daqui no mesmo commit da correção.

---

## 0. Urgente — produção sem banco persistente

### 0.1 Produção roda em SQLite e perde tudo a cada deploy
**Verificado em 17/09/2026**, por `/conversas/api/status` em produção:
`"dialeto": "sqlite"`. Nenhuma configuração define `DATABASE_URL`: o
`.ebextensions/session-secret.config` não tem a chave e o `.env` a deixa em
branco. Sem ela, `infraestrutura_critica/app.py` usa `instance/emalog.db`
dentro da pasta da aplicação, que o Elastic Beanstalk substitui a cada deploy.

Consequências:
- Tudo o que foi cadastrado em produção entre dois deploys se perdeu no deploy
  seguinte. Isso já acontecia antes da Central: o zip original levava um banco
  vazio, num caminho que a aplicação nem lê.
- O backup diário grava em `backups/`, na mesma pasta, e se perde junto. O
  destino externo, `PRIVATE_OBJECT_DIR`, é do Replit e não existe na AWS.
- Com mais de uma instância, cada uma teria um banco diferente.

O que fazer:
1. Não publicar outro deploy até resolver, se houver dados reais em produção.
2. Criar um PostgreSQL persistente no Amazon RDS, em `sa-east-1`, e definir
   `DATABASE_URL` no Elastic Beanstalk. O script `infra/aws/criar_banco_rds.sh`
   faz os dois passos e deve ser rodado no AWS CloudShell.
3. Depois de confirmar `"dialeto": "postgresql"`, fazer a aplicação recusar
   subir em SQLite quando estiver no Elastic Beanstalk. Hoje, se o PostgreSQL
   estiver fora do ar no boot, ela cai para SQLite vazio sem avisar ninguém.

### 0.2 Servidor da Evolution fora do ar
**Verificado em 17/09/2026.** `evolution-evolution.iqutxq.easypanel.host`
(191.101.235.249) não aceita conexão nas portas 80 e 443, nem a partir da AWS
nem a partir de fora. É o servidor ou o firewall dele, não o código. Enquanto
isso, nenhuma mensagem sai pela Central nem pelo EMA, e o webhook de entrada
também não chega. Conferir o servidor no painel do Easypanel.

---

## 1. Segurança — resolver primeiro

### 1.1 Segredos vazados no histórico do git
**Verificado.** Os commits `22772a5` e `1646e5c` contêm o
`.ebextensions/session-secret.config` com valores reais, num repositório que
tem remoto. Tirar o arquivo do versionamento não desfaz o que já foi enviado.

Rotacionar: `SESSION_SECRET`, `INITIAL_ADMIN_PASSWORD`, `EVOLUTION_API_KEY`.
Depois, atualizar `.env` e `.ebextensions/session-secret.config`, que estão
fora do git.

### 1.2 Token da Twilio enviado por chat
**Verificado.** `TWILIO_AUTH_TOKEN` passou por uma conversa e deve ser
considerado comprometido. Rotacionar no console da Twilio, em Account, API
keys & tokens.

### 1.3 Ambiente sem HTTPS
**Verificado.** A porta 443 do ambiente
`sistemaemalog-env.eba-gyy2n3xm.sa-east-1.elasticbeanstalk.com` não responde.
Conversas com motoristas trafegam em texto aberto, e com anexos isso inclui
CNH e CRLV. Questão de LGPD.

Ao instalar certificado, trocar `http` por `https` ao mesmo tempo em
`TWILIO_WEBHOOK_URL`, no console da Twilio e no webhook da Evolution. Se
trocar só um lado, a assinatura da Twilio para de validar.

### 1.4 Token do webhook da Evolution na URL
**Verificado.** O webhook da Evolution chama `/ema/webhook?token=...`. O token
em query string fica gravado nos logs de acesso do balanceador. Preferir o
cabeçalho `apikey`, que o próprio webhook já aceita.

---

## 2. Produção — decisões e conferências

### 2.1 S3 não configurado quebra upload de documento de motorista
**Verificado.** `S3_BUCKET_NAME` vale "PREENCHER-nome-do-bucket", e
`infraestrutura_critica/utils/storage.py` trata esse texto como bucket real.
Todo upload falha com `InvalidAccessKeyId`. Isso afeta hoje o cadastro de
motorista com documento, em `prospeccao_captacao_motorista/drivers.py`.

Decisão pendente. Recomendado: criar o bucket e o usuário IAM, conforme
`testes/fase3/LEIAME.md`. A alternativa, fazer `storage.py` cair para o disco
quando vê o placeholder, evita o erro mas perde os arquivos a cada deploy.

Os anexos da Central não usam `storage.py` e já tratam o placeholder como
"não configurado".

### 2.2 Documentos coletados pelo EMA no disco local
**Verificado.** `_save_media_to_field`, em
`prospeccao_captacao_motorista/utils/ema_agent.py`, move a CNH e o CRLV para
`infraestrutura_critica/static/uploads/drivers/`. Esse diretório é substituído
a cada deploy.

**A verificar:** esse caminho é diferente do que a ficha do motorista lê, que
usa chaves relativas ao diretório do projeto via `storage.get_file`. É possível
que documentos coletados pelo EMA nem apareçam na ficha.

### 2.4 Conferência do banco depois do deploy das Fases 1 a 4
O pacote `emalog-deploy-20260917-1049.zip` foi publicado em 17/09/2026.
**Verificado de fora, sem login:** as rotas da Fase 4 existem, os arquivos
estáticos servidos são idênticos aos do repositório e o webhook da Twilio
recusa chamada sem assinatura e aceita a assinada.

**A verificar, com login de administrador:** abrir `/conversas/api/status` e
conferir `"schema_ok": true`. **Conferido em 17/09/2026:** `schema_ok` é
`true`, mas o banco é SQLite; ver item 0.1.

### 2.5 Backfill do histórico
Mensagens anteriores à Central ainda não estão agrupadas em conversas. Rodar no
servidor, primeiro simulando:

```
python3 scripts/backfill_conversations.py --dry-run
python3 scripts/backfill_conversations.py
```

### 2.6 Configuração regional do banco de produção
**Verificado em teste local.** Com locale `C`, a busca não iguala `JOÃO` a
`João`. Rodar `SHOW lc_ctype;` no PostgreSQL de produção para saber se isso
afeta a produção.

### 2.7 Pacotes antigos de deploy
`FONTE AWS RODANDO/` tem pacotes antigos que podem ser apagados. Só o mais
recente importa.

---

## 3. Mensagens que não aparecem na Central

### 3.1 Lembretes de inatividade do EMA
**Verificado.** `_ema_inactivity_check`, em `infraestrutura_critica/app.py`,
envia lembrete e aviso de encerramento com `send_text` e só grava no
histórico da sessão. Não cria `WhatsAppMessage`, então não aparece na conversa.

### 3.2 Envio manual pela tela de sessão do EMA
**Verificado.** `session_send`, em `prospeccao_captacao_motorista/ema_agent.py`,
também não cria `WhatsAppMessage`. Além de não aparecer na Central, essa
mensagem escapa da fila: um operador pode falar com o motorista sem assumir a
conversa na Central.

Nos dois casos, criar a `WhatsAppMessage` basta: o vínculo à conversa e o
aviso em tempo real já acontecem sozinhos. Enquanto isso não for feito, essas
mensagens também ficam fora dos relatórios da Fase 4.

---

## 4. Código legado que contorna a fila

### 4.1 Rotas antigas de conversa da Contratação
**Verificado.** `/contracting/api/conversations/<id>/mode` e `/send`, em
`kanban_contratacao/contracting.py`, continuam registradas e alteram modo e
dono do motorista sem passar pela disputa no banco da Fase 2. Nenhuma tela as
chama hoje. Decidir: remover, ou fazer passarem por `atendimento_conversas/utils/fila.py`.

A função `conversations()` do mesmo arquivo não tem decorador de rota; é
código morto.

### 4.2 Aba Conversas da Contratação ainda abre o Chatwoot
**Verificado.** A aba abre o Chatwoot em janela, e `app.py` libera o domínio
dele na política de segurança. O plano previa a Central substituindo o
Chatwoot. Trocar a aba por um link para `/conversas` e, depois de validado,
retirar `CHATWOOT_*` e o `frame-src`.

### 4.3 Duas normalizações de telefone
**Verificado.** O EMA localiza motorista e sessão com `normalize_phone`, de
`infraestrutura_critica/utils/evolution_api.py`, que decide o código de país
por prefixo e erra todo número do DDD 55. A Central usa `normalize_contact_key`,
que decide por comprimento. Motoristas de DDD 55 podem não ser reconhecidos
pelo EMA.

---

## 5. Limitações técnicas conhecidas

- **Tempo real depende de um único worker.** O Procfile usa `-w 1`. Com mais
  workers, os avisos de socket param de chegar em todas as telas, embora a
  disputa no banco continue correta. Escalar exige fila de mensagens, como
  Redis, no `socketio`.
- **Worker eventlet descontinuado.** O Gunicorn remove o worker eventlet na
  versão 26, e o eventlet não é mais mantido. `requirements.txt` fixa o
  Gunicorn abaixo da 26, o que segura por ora.
- **Latência ao assumir durante resposta do EMA.** O webhook do EMA trava a
  linha do motorista durante a chamada de IA. Assumir essa conversa no mesmo
  instante espera a IA terminar; medido em 1,3s num teste. Não falha.
- **Dois manipuladores de `join_notifications`**, em `app.py` e em
  `cotacao_precificacao_clientes/notifications.py`. Vence o de `app.py`, por
  ordem de registro, e é ele que coloca atendentes na sala `operators`. Se a
  ordem mudar, o tempo real do sistema inteiro para em silêncio.
- **Anexos anteriores ao S3 não são recuperados.** A referência do provedor
  fica guardada, mas não há reprocessamento.
- **Áudio e vídeo não são guardados** como anexo.
- **`datetime.utcnow()`** está obsoleto a partir do Python 3.12 e aparece no
  sistema inteiro.
- **Relatório calculado na aplicação.** `atendimento_conversas/utils/relatorios.py`
  lê as mensagens do período e conta em Python, para não depender de função de
  data de um banco só. O período vai até 92 dias e 300 mil mensagens; acima
  disso a tela pede um intervalo menor, sem travar. Se o volume crescer a esse
  ponto, guardar totais por dia numa tabela.
- **Tempo de resposta em horas corridas.** Não desconta noite, fim de semana nem
  feriado. A mediana suaviza, mas uma equipe que não atende de madrugada vai
  ver médias altas.

---

## 6. Relatórios — decisões

### 6.1 Quem vê os relatórios
Hoje só administrador. Decidir se o operador deve ver os próprios números.

### 6.2 Números anteriores às fases
**Verificado.** Assumidas, transferências e resolvidas só existem a partir do
deploy da Fase 2, e respostas do bot só a partir da Fase 3. O backfill (2.5)
agrupa mensagens antigas, mas não recria esse histórico, e as conversas criadas
por ele contam como abertas no dia em que o script rodou. Para comparar
períodos, usar datas posteriores ao deploy.

---

## 7. Twilio

A conta é trial. O webhook de entrada pode ser configurado de graça, em Try
out WhatsApp, Auto-Reply settings, Custom. Mas a API de envio em conta trial
não aceita texto livre, só modelo pré-aprovado; por isso o canal de envio está
fixo em `evolution`. Para usar a Twilio de verdade: fazer o upgrade da conta,
configurar os dois webhooks e trocar `ATENDIMENTO_CANAL` para `twilio`.

---

## 8. Validação manual ainda não feita

- Roteiro com dois atendentes simultâneos: `testes/fase2/LEIAME.md`.
- Leitura, avisos, busca e anexos: `testes/fase3/LEIAME.md`.
- Relatórios, CSV no Excel e bloqueio para operador: `testes/fase4/LEIAME.md`.
- Ponta a ponta real pela Evolution em produção: mandar mensagem de um
  celular, ver na Central, responder e receber.

---

## 9. Limpeza do repositório

- `EXTRAÇÃO REPLIT_ORIGINAL/` é a versão anterior à modularização, mantida
  como referência.
- As pastas com acento na raiz contêm só um arquivo vazio `File 1`.
