# Pendências da Central de Atendimento

Lista do que ficou para depois, levantada durante as Fases 0 a 4, em
17/09/2026. Cada item diz o que é, por que importa e o que fazer. Os itens
marcados como **verificado** foram reproduzidos ou confirmados no código; os
marcados como **a verificar** são suspeitas que ainda precisam de prova.

Ao resolver um item, apague-o daqui no mesmo commit da correção.

---

## 0. Urgente — produção

### 0.1 Publicar a trava contra SQLite
O banco de produção já é o PostgreSQL `emalog-db`, no RDS, criado por
`infra/aws/criar_banco_rds.sh`. **Conferido em 17/09/2026** em
`/conversas/api/status`: `"dialeto": "postgresql"` e `"schema_ok": true`. O
que ficou no SQLite antigo não foi copiado.

O código já recusa subir em SQLite no Elastic Beanstalk, com teste em
`testes/producao/`, mas isso ainda não foi publicado. O pacote é o
`emalog-deploy-20260917-1440.zip`. Até o deploy, se o
PostgreSQL não responder em 3 segundos no boot, a produção sobe com um SQLite
vazio sem avisar. Depois do deploy, conferir `"trava_sqlite": true` em
`/conversas/api/status`.

### 0.2 Senha do banco de produção enviada por chat
**Verificado.** A linha `DATABASE_URL` completa, com a senha do `emalog-db`,
apareceu num print enviado a uma conversa.

O controle das credenciais comprometidas passou a ficar em **`SEGREDOS.md`**,
na raiz do projeto, fora do git e fora do zip de deploy. Lá está a lista do que
vazou, o procedimento de troca de cada uma e a tabela para registrar o que já
foi rotacionado. O passo a passo desta é a seção 3.1.

### 0.3 Terminar a troca para a Evolution própria
O servidor de terceiros `evolution-evolution.iqutxq.easypanel.host`
(191.101.235.249) caiu em 17/09/2026 e não voltou. O dono dele teve acesso à
sessão do WhatsApp da empresa e à `EVOLUTION_API_KEY` antiga, que o
`/ema/webhook` aceita como autenticação.

**Feito em 17/09/2026:** `infra/aws/criar_evolution_ec2.sh` rodou até o fim no
CloudShell. A Evolution 2.4.0 está no ar no EC2 `emalog-evolution`
(`i-0b0fe70df387622f7`), acessível só pelo sistema, com os dados no banco
`evolution` dentro do `emalog-db`. A conta da Evolution Foundation foi ativada
e a porta do painel, fechada. A instância `emalog` foi criada e o ambiente
`SistemaEmalog-env` ficou `Ready`/`Green` apontando para
`http://172.31.16.200:8080`.

**Feito em 18/09/2026:** o QR code foi escaneado e a instância `emalog` está
conectada. O sistema recebe e envia mensagem pela Evolution própria. A troca
para o servidor próprio está concluída.

O que falta:
1. Trocar a `EVOLUTION_API_KEY`, que vazou num print. O procedimento está em
   `SEGREDOS.md`, seção 3.2. Agora que o WhatsApp está conectado, a troca tem
   um cuidado a mais: a sessão fica no banco `evolution` e deve sobreviver,
   mas confirmar logo depois em Cadastros EMA, e fazer num horário em que
   alguém possa escanear de novo se cair.
2. No celular da empresa, remover de Aparelhos conectados toda sessão
   desconhecida, inclusive a do servidor antigo. O dono do servidor de
   terceiros teve acesso à sessão do WhatsApp da empresa — enquanto essa
   limpeza não for feita, ele pode continuar lendo as conversas.
3. Conferir se o `.env` e o `session-secret.config` estão com as três linhas
   `EVOLUTION_*` que o script imprimiu no fim.

---

## 1. Segurança — resolver primeiro

### 1.1 Credenciais comprometidas — ver `SEGREDOS.md`
**Verificado.** Cinco credenciais estão comprometidas: três pelo histórico do
git, nos commits `22772a5` e `1646e5c`, e duas por print enviado em conversa.

O acompanhamento saiu daqui e ganhou documento próprio: **`SEGREDOS.md`**, na
raiz do projeto, fora do git e fora do zip de deploy. Ele traz a situação de
cada credencial, o procedimento de troca de cada uma e a tabela de registro do
que já foi rotacionado. Não guarda nenhum valor, de propósito.

Resumo do que está pendente de rotação: `SESSION_SECRET`,
`INITIAL_ADMIN_PASSWORD`, `EVOLUTION_API_KEY`, `TWILIO_AUTH_TOKEN` e a senha do
banco em `DATABASE_URL`.

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

Junto delas ficou código morto: a função `conversations()`, sem decorador de
rota, e a coluna `users.chatwoot_user_id`, que ninguém mais lê desde a saída
do Chatwoot.

### 4.2 Restos do Chatwoot fora do código
**Feito em 17/09/2026:** a aba Conversas da Contratação mostra o painel da
Central de Atendimento, o `frame-src` voltou a ser só `'self'`, a rota
`/contracting/api/chatwoot/sso` foi apagada e as chaves `CHATWOOT_*` saíram do
`.ebextensions`. Teste em `testes/painel_conversas/`.

O que falta, quando for cômodo: apagar `CHATWOOT_URL` e `CHATWOOT_ACCOUNT_ID`
das variáveis do ambiente, em Elastic Beanstalk > Configuração > Atualizações,
monitoramento e registro. O sistema já as ignora. E desligar o servidor do
Chatwoot, em `18.228.138.222`, se ele ainda estiver de pé e não servir a mais
nada: é custo de EC2 todo mês.

### 4.3 Duas normalizações de telefone
**Verificado.** O EMA localiza motorista e sessão com `normalize_phone`, de
`infraestrutura_critica/utils/evolution_api.py`, que decide o código de país
por prefixo e erra todo número do DDD 55. A Central usa `normalize_contact_key`,
que decide por comprimento. Motoristas de DDD 55 podem não ser reconhecidos
pelo EMA.

### 4.4 Envio simulado quando a Evolution não está configurada
**Verificado.** `send_text`, em
`infraestrutura_critica/utils/evolution_api.py`, devolve `True` e só escreve no
log quando `EVOLUTION_API_URL` ou `EVOLUTION_API_KEY` estão vazias. É proposital
para desenvolvimento, mas é a mesma armadilha que fazia a tela de oferta de
frete dizer "enviado" sem enviar: numa máquina sem essas variáveis, qualquer
envio parece ter dado certo.

Em produção as duas estão preenchidas, então isso não afeta o ambiente hoje.
Decidir se vale separar o modo simulado numa variável própria, como
`EVOLUTION_SIMULAR=1`, para que a ausência de configuração vire erro.

### 4.5 Valor em formato americano nas mensagens de frete
A oferta gerada por `generate_freight_message`, em
`execucao_entrega_frete/utils/whatsapp.py`, usa `f'{valor:,.2f}'` e manda
"R$ 2,500.00" para o motorista, com a vírgula e o ponto trocados para quem lê
em português. O mesmo acontece na resposta de
`/freight/<id>/assign-to-driver/`. A confirmação de aceite já usa `_brl`, em
`oferta_frete_motorista/utils/driver_bid_agent.py`, que formata certo. Trocar
nos outros dois lugares quando for cômodo.

### 4.6 Trocar motorista não mexe no Kanban
**Verificado.** `reassign_driver`, em `execucao_entrega_frete/freight.py`,
troca o motorista do frete, cancela os pagamentos do antigo e cria os do novo,
mas não toca em nenhuma `DriverBid`. Depois de uma troca, o quadro de
contratação continua mostrando o motorista antigo em "Contratados" e o novo
não aparece em lugar nenhum.

É o mesmo defeito que `assign_to_driver` tinha e que foi corrigido em
18/09/2026, passando a contratação por `contract_bid`. A troca precisa de um
caminho equivalente: encerrar a bid do motorista antigo com motivo e contratar
a do novo. O botão fica na ficha do frete, em "Trocar motorista".

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
