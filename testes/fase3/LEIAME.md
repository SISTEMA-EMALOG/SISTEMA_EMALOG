# Testes da Fase 3: estados e usabilidade

Lida e não lida, aviso de mensagem nova, busca e anexos no S3.

Esta pasta não entra no zip de deploy. A trava de segurança é a mesma da
Fase 2, em `testes/_ambiente.py`: os testes recusam banco que não seja local.

## O que cada arquivo prova

**`leitura_e_vinculo.py`**

- A regressão do defeito que motivou a coluna `read_at`. O bot responde, um
  atendente assume e lê, devolve ao EMA, e a Evolution reentrega a mesma
  mensagem. O bot não pode responder de novo.
- Só o responsável marca como lida; um colega que abre para olhar não apaga o
  sinal de espera. Só as mensagens exibidas são marcadas.
- Resposta do bot e oferta de frete aparecem na conversa. Oferta a motorista
  sem conversa não abre conversa, e entra nela quando ele responde, se tiver
  até 7 dias.
- Um aviso em tempo real por mensagem, de qualquer canal, sem duplicata e
  sem o texto da mensagem.
- Busca por telefone com máscara, só dígitos, nome do motorista e nome do
  contato.
- O alerta global carrega para atendente e não carrega para cliente.

**`anexos_s3.py`**: o S3 é simulado pelo Stubber do botocore, que confere
cada chamada contra o modelo oficial da API do S3.

- Com o placeholder `PREENCHER` na configuração, o anexo fica registrado como
  indisponível e nada é baixado.
- Foto pela Twilio vai para o S3, com download autenticado, tipo e
  criptografia conferidos, e é servida com `nosniff` e `sandbox`.
- Executável disfarçado de JPEG, arquivo acima de 10 MB e URL fora da Twilio
  são recusados.
- PDF pela Evolution vai para o S3 sem tirar o arquivo do EMA, que ainda o usa
  para ler a CNH ou o CRLV.
- Cliente e visitante sem login não abrem anexo.
- Falha no meio do processamento deixa o anexo registrado como erro.
- Só no PostgreSQL: nenhuma transação aberta durante o upload.

**`migracao_read_at.py`**: cada boot em processo separado, como em produção.

- Com falha forçada no preenchimento, a coluna não fica criada pela metade.
- Sem falha, mensagens recebidas antigas ficam lidas, e o `status='lido'`
  gravado pela Central antiga é desfeito só nas recebidas. Recibo de leitura
  de mensagem enviada fica intocado.
- O boot seguinte não repete o preenchimento sobre mensagem nova.

## Como rodar

SQLite:

```
del instance\emalog.db
python testes\fase3\leitura_e_vinculo.py
python testes\fase3\anexos_s3.py
python testes\fase3\migracao_read_at.py
```

PostgreSQL local, um banco vazio por script:

```
set DATABASE_URL=postgresql://postgres:SENHA@localhost:5432/emalog_teste?sslmode=disable
python testes\fase3\anexos_s3.py
```

## Teste manual

**Lida e não lida, com dois atendentes.** Mande duas mensagens de um celular
para uma conversa livre que não esteja com o EMA. O atendente 2 abre a
conversa só para olhar. Na lista, o contador vermelho continua em 2. O
atendente 1 assume e abre: o contador zera. Mande mais uma mensagem: volta
para 1.

**Aviso de mensagem nova.** Com o atendente em outra tela do sistema, como
Fretes, mande uma mensagem para uma conversa dele. O item Conversas do menu
mostra o número, o título da aba do navegador mostra o número entre
parênteses, e toca um som curto. O som só funciona depois do primeiro clique
na página, por regra do navegador. Em Conversas, o botão Som liga e desliga, e
Ativar avisos pede permissão para notificação com a aba em segundo plano. A
notificação nunca mostra o texto da mensagem.

Conversa que o EMA está conduzindo não apita, para não gerar ruído o dia todo.

**Busca.** Digite o telefone como o motorista informou, por exemplo
`(11) 98888-7777`, ou só parte dos dígitos, ou o nome do motorista.

**Anexos.** Só funcionam com S3 configurado; veja abaixo. Mande uma foto e um
PDF pelo WhatsApp. A foto aparece na conversa; o PDF aparece como link.

## Configurar o S3

Sem S3, os anexos chegam registrados como "armazenamento não configurado" e o
arquivo não é guardado. Nunca vão para o disco do servidor, que some a cada
deploy.

1. Crie um bucket privado, sem acesso público.
2. Crie um usuário IAM com permissão só para esse bucket:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["s3:PutObject", "s3:GetObject"],
       "Resource": "arn:aws:s3:::NOME-DO-BUCKET/conversas/*"
     }]
   }
   ```

3. Preencha `S3_BUCKET_NAME`, `AWS_REGION`, `AWS_ACCESS_KEY_ID` e
   `AWS_SECRET_ACCESS_KEY` no `.ebextensions/session-secret.config`.
4. Depois do deploy, `/conversas/api/status` deve mostrar
   `"s3_configurado": true`.

Anexos recebidos antes da configuração continuam indisponíveis. A referência
do provedor fica guardada, mas não há reprocessamento automático.

## Limitações conhecidas

- A busca pode não igualar maiúscula e minúscula em letra acentuada: `JOÃO`
  pode não encontrar `João`. No SQLite nunca iguala. No PostgreSQL depende da
  configuração regional do banco: com locale `C` não iguala, o que foi
  verificado; com locale UTF-8 ou collation ICU iguala. Sem acento, como
  `PEREIRA` e `pereira`, funciona nos dois. Para conferir a produção, rode
  `SHOW lc_ctype;` no banco.
- Tipos aceitos como anexo: JPEG, PNG, WEBP e PDF, até 10 MB. Áudio e vídeo
  não são guardados.
