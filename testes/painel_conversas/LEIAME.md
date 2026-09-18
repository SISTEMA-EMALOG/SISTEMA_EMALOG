# Testes do painel de conversas nas duas telas

A fila de WhatsApp é atendida no mesmo painel em dois lugares:

- `/conversas`, a Central de Atendimento em página inteira;
- a aba **Conversas** da Central de Contratação, ao lado do Kanban.

O corpo do painel fica em `templates/conversas/_painel.html`, o estilo em
`static/css/conversas.css` e o comportamento em `static/js/conversas.js`. Os
dois templates incluem o mesmo arquivo, com os mesmos ids, então o painel
nunca pode ser incluído duas vezes na mesma página.

O Chatwoot saiu: a aba mostrava um botão que abria o painel dele em outra
janela. A rota `/contracting/api/chatwoot/sso` foi apagada e a política de
segurança não libera mais iframe de terceiro.

Esta pasta não entra no zip de deploy. A trava de segurança é a de
`testes/_ambiente.py`.

## O que `nas_duas_telas.py` prova

- As duas telas trazem o painel, uma vez só em cada uma, com o CSS e o
  JavaScript que o fazem funcionar, a caixa de resposta e as abas da fila.
- A aba da Contratação não tem mais nenhuma palavra "chatwoot", nem o botão
  que abria a janela, nem o CSS órfão da tela antiga. A rota do login do
  Chatwoot responde 404.
- `frame-src` fica em `'self'`, mesmo com `CHATWOOT_URL` ainda definida no
  ambiente — o teste define essa variável de propósito.
- O Kanban e as três abas continuam na mesma página.
- O botão Relatórios do painel só aparece para administrador.
- Cliente recebe 403 nas duas telas.

Teste de mutação feito durante o desenvolvimento, e não incluído: tirando o
`{% include %}` da aba da Contratação, o teste reprovou em 6 verificações.

## Como rodar

```
del instance\emalog.db
python testes\painel_conversas\nas_duas_telas.py
```

PostgreSQL local, com banco vazio:

```
set DATABASE_URL=postgresql://postgres:SENHA@localhost:5432/emalog_teste?sslmode=disable
python testes\painel_conversas\nas_duas_telas.py
```

## Teste manual

Entre como administrador e abra Central de contratação → Conversas.

1. A fila aparece ali, com as mesmas abas de `/conversas`, e a conversa abre
   ao lado.
2. Assuma uma conversa e responda: a mensagem sai e aparece na hora na outra
   tela aberta em `/conversas`.
3. Troque para o Kanban e volte para Conversas: a fila continua funcionando.
4. O botão Atualizar do topo recarrega a fila quando a aba Conversas está
   aberta, e o quadro quando o Kanban está aberto.
5. Em tela estreita, de celular, a lista fica em cima e a conversa embaixo.
