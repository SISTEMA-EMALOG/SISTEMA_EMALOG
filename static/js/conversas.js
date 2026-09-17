/* Central de Atendimento — Fase 2, fila compartilhada.
 *
 * Princípio: a tela NUNCA decide quem é dono de uma conversa. Quem decide é
 * o banco, no servidor. A tela só mostra o último estado conhecido, e
 * qualquer clique em cima de estado velho volta 409 com a explicação.
 *
 * O evento de socket serve para a tela se atualizar rápido. Se um evento se
 * perder, nada quebra: a ação seguinte do atendente é recusada pelo servidor
 * e a tela recarrega. Na reconexão do socket a tela recarrega tudo, para
 * recuperar o que passou enquanto estava desconectada.
 *
 * Convenções do projeto seguidas:
 *  - socket compartilhado window.socket, de notifications.js, sem abrir outro;
 *  - todo texto vindo do WhatsApp passa por esc() antes do DOM;
 *  - CSRF injetado pelo wrapper global de fetch do dashboard_base.html.
 */
(function () {
  'use strict';

  var $ = function (sel) { return document.querySelector(sel); };
  var app = $('#convApp');
  if (!app) return;

  var EU = parseInt(app.dataset.userId, 10);

  var state = {
    aba: 'fila',
    busca: '',
    atual: null,
    detalhe: null,
    atendentes: null,
    carregandoLista: false,
    timerLista: null,
    timerDetalhe: null
  };

  var els = {
    items: $('#convItems'),
    skeleton: $('#convSkeleton'),
    tabs: $('#convTabs'),
    messages: $('#convMessages'),
    form: $('#convForm'),
    input: $('#convInput'),
    send: $('#convSend'),
    title: $('#convTitle'),
    subtitle: $('#convSubtitle'),
    owner: $('#convOwner'),
    pill: $('#convStatusPill'),
    actions: $('#convActions'),
    transfer: $('#convTransfer'),
    transferTo: $('#convTransferTo'),
    transferOk: $('#convTransferOk'),
    transferCancel: $('#convTransferCancel'),
    lock: $('#convLock'),
    context: $('#convContext'),
    search: $('#convSearch'),
    status: $('#convStatus'),
    refresh: $('#convRefresh')
  };

  // ── Utilidades ─────────────────────────────────────────────────────────

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var timerAviso = null;
  function aviso(msg, tipo) {
    if (!els.status) return;
    els.status.textContent = msg;
    els.status.className = 'rounded-lg border px-4 py-3 text-sm mb-4 ' +
      (tipo === 'success'
        ? 'bg-green-50 border-green-200 text-green-800'
        : tipo === 'info'
          ? 'bg-blue-50 border-blue-200 text-blue-800'
          : 'bg-red-50 border-red-200 text-red-800');
    clearTimeout(timerAviso);
    timerAviso = setTimeout(function () { els.status.classList.add('hidden'); }, 5000);
  }

  function hora(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d)) return '';
    var hoje = new Date();
    return d.toDateString() === hoje.toDateString()
      ? d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })
      : d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' });
  }

  function telefoneLegivel(chave) {
    var d = String(chave || '').replace(/\D/g, '');
    if (d.length === 13 && d.indexOf('55') === 0) {
      return '+55 (' + d.slice(2, 4) + ') ' + d.slice(4, 9) + '-' + d.slice(9);
    }
    if (d.length === 12 && d.indexOf('55') === 0) {
      return '+55 (' + d.slice(2, 4) + ') ' + d.slice(4, 8) + '-' + d.slice(8);
    }
    return chave || '';
  }

  var CORES_STATUS = {
    aberta: 'bg-green-100 text-green-800',
    pendente: 'bg-yellow-100 text-yellow-800',
    resolvida: 'bg-gray-100 text-gray-600'
  };

  var NOMES_ACAO = {
    assumiu: 'assumiu', transferiu: 'transferiu', liberou: 'liberou para a fila',
    devolveu_bot: 'devolveu ao EMA', escalou_humano: 'EMA pediu atendimento humano',
    resolveu: 'resolveu', reabriu: 'reabriu'
  };

  function dono(c) {
    if (!c) return '';
    if (c.status === 'resolvida') return 'Resolvida';
    if (c.assigned_agent_id === EU) return 'Com você';
    if (c.assigned_agent_id) return 'Com ' + (c.assigned_agent || 'outro atendente');
    if (c.handling_mode === 'auto') return 'EMA atendendo';
    return 'Livre na fila';
  }

  function corDono(c) {
    if (!c || c.status === 'resolvida') return 'text-gray-400';
    if (c.assigned_agent_id === EU) return 'text-green-700';
    if (c.assigned_agent_id) return 'text-yellow-700';
    if (c.handling_mode === 'auto') return 'text-blue-700';
    return 'text-red-600';
  }

  async function pedir(url, opcoes) {
    var r = await fetch(url, Object.assign({ credentials: 'include' }, opcoes || {}));
    var corpo = await r.json().catch(function () { return {}; });
    return { status: r.status, ok: r.ok, corpo: corpo };
  }

  function postar(url, dados) {
    return pedir(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(dados || {})
    });
  }

  // ── Lista ──────────────────────────────────────────────────────────────

  async function carregarLista() {
    if (state.carregandoLista) { agendarLista(); return; }
    state.carregandoLista = true;
    try {
      var params = new URLSearchParams({ aba: state.aba });
      if (state.busca) params.set('busca', state.busca);
      var res = await pedir('/conversas/api/conversas?' + params.toString());
      if (!res.ok) throw new Error(res.corpo.error || 'Não foi possível carregar as conversas.');
      renderContagens(res.corpo.contagens || {});
      renderLista(res.corpo.conversas || []);
    } catch (e) {
      aviso(e.message || 'Não foi possível carregar as conversas.');
    } finally {
      state.carregandoLista = false;
      if (els.skeleton) { els.skeleton.remove(); els.skeleton = null; }
    }
  }

  function agendarLista() {
    clearTimeout(state.timerLista);
    state.timerLista = setTimeout(carregarLista, 250);
  }

  function renderContagens(cont) {
    ['fila', 'minhas'].forEach(function (k) {
      var el = els.tabs.querySelector('[data-contagem="' + k + '"]');
      if (!el) return;
      var n = cont[k] || 0;
      el.textContent = n > 99 ? '99+' : n;
      el.classList.toggle('hidden', n === 0);
    });
  }

  function renderLista(conversas) {
    if (!conversas.length) {
      var vazio = {
        fila: 'Ninguém esperando atendimento.',
        minhas: 'Você não está com nenhuma conversa.',
        ativas: 'Nenhuma conversa ativa.',
        resolvidas: 'Nenhuma conversa resolvida.'
      }[state.aba] || 'Nenhuma conversa aqui.';
      els.items.innerHTML =
        '<div class="p-6 text-center text-gray-400">' +
        '<i class="fas fa-inbox text-2xl mb-2" aria-hidden="true"></i>' +
        '<p class="text-sm">' + esc(vazio) + '</p></div>';
      return;
    }
    els.items.innerHTML = conversas.map(function (c) {
      var ativo = state.atual === c.id ? ' is-active' : '';
      var badge = c.nao_lidas > 0
        ? '<span class="ml-2 inline-flex items-center justify-center min-w-5 h-5 px-1 rounded-full bg-red-500 text-white text-xs font-bold">' +
          (c.nao_lidas > 9 ? '9+' : c.nao_lidas) + '</span>'
        : '';
      return '' +
        '<button type="button" class="conv-item' + ativo + '" data-id="' + c.id + '">' +
          '<div class="flex items-center justify-between gap-2">' +
            '<span class="font-bold text-sm text-gray-900 truncate">' + esc(c.nome) + '</span>' +
            '<span class="text-xs text-gray-400 flex-shrink-0">' + esc(hora(c.last_activity_at)) + '</span>' +
          '</div>' +
          '<div class="flex items-center justify-between gap-2 mt-0.5">' +
            '<span class="text-xs text-gray-500 truncate">' + esc(c.ultima_mensagem || '—') + '</span>' +
            badge +
          '</div>' +
          '<div class="mt-1 text-[11px] font-bold ' + corDono(c) + '">' + esc(dono(c)) + '</div>' +
        '</button>';
    }).join('');
  }

  // ── Thread ─────────────────────────────────────────────────────────────

  async function abrir(id, silencioso) {
    state.atual = id;
    esconderTransferencia();
    if (!silencioso) {
      els.messages.innerHTML = '<div class="p-4 space-y-3">' +
        '<div class="skeleton h-12 rounded-lg w-2/3"></div>' +
        '<div class="skeleton h-12 rounded-lg w-1/2 ml-auto"></div></div>';
    }
    try {
      var res = await pedir('/conversas/api/conversas/' + encodeURIComponent(id));
      if (!res.ok) throw new Error(res.corpo.error || 'Não foi possível abrir a conversa.');
      if (state.atual !== id) return;   // o atendente já abriu outra
      state.detalhe = res.corpo;
      renderThread(res.corpo);
      agendarLista();
    } catch (e) {
      aviso(e.message || 'Não foi possível abrir a conversa.');
    }
  }

  function agendarDetalhe() {
    if (!state.atual) return;
    clearTimeout(state.timerDetalhe);
    var id = state.atual;
    state.timerDetalhe = setTimeout(function () { abrir(id, true); }, 250);
  }

  function renderThread(data) {
    var c = data.conversa || {};
    var p = data.permissoes || {};

    els.title.textContent = c.nome || telefoneLegivel(c.contact_phone);
    els.subtitle.textContent = telefoneLegivel(c.contact_phone);
    els.owner.textContent = dono(c);
    els.owner.className = 'text-xs mt-1 font-bold ' + corDono(c);

    els.pill.className = 'status-pill ' + (CORES_STATUS[c.status] || CORES_STATUS.resolvida);
    els.pill.textContent = c.status || '';
    els.pill.classList.remove('hidden');

    renderAcoes(c, p, data);

    // Conversa com outra pessoa: somente leitura, e a tela diz por quê.
    if (c.status !== 'resolvida' && c.assigned_agent_id && c.assigned_agent_id !== EU) {
      els.lock.textContent = 'Em atendimento com ' + (c.assigned_agent || 'outro atendente') +
        '. Você pode ler, mas só quem está com a conversa responde.';
      els.lock.classList.remove('hidden');
    } else {
      els.lock.classList.add('hidden');
    }

    var msgs = data.mensagens || [];
    els.messages.innerHTML = msgs.length
      ? msgs.map(function (m) {
          var saida = m.direction === 'outbound';
          return '<div class="' + (saida ? 'msg-out' : 'msg-in') + '">' +
            esc(m.texto) +
            '<div class="text-[10px] mt-1 ' + (saida ? 'text-gray-300' : 'text-gray-400') + '">' +
              esc(hora(m.timestamp)) + (saida ? ' · ' + esc(m.status) : '') +
            '</div></div>';
        }).join('')
      : '<div class="h-full flex items-center justify-center text-sm text-gray-400">' +
        'Nenhuma mensagem nesta conversa.</div>';
    els.messages.scrollTop = els.messages.scrollHeight;

    var podeEnviar = !!p.enviar;
    els.input.disabled = !podeEnviar;
    els.send.disabled = !podeEnviar;
    if (!data.pode_enviar) {
      els.input.placeholder = 'Configure um canal de WhatsApp para poder responder';
    } else if (c.status === 'resolvida') {
      els.input.placeholder = 'Conversa resolvida. Reabra para responder.';
    } else if (c.assigned_agent_id && c.assigned_agent_id !== EU) {
      els.input.placeholder = 'Só ' + (c.assigned_agent || 'o responsável') + ' pode responder';
    } else if (!c.assigned_agent_id) {
      els.input.placeholder = 'Responder assume a conversa para você';
    } else {
      els.input.placeholder = 'Escreva a resposta e pressione Enter';
    }

    renderContexto(data);
  }

  function botao(acao, rotulo, classes) {
    return '<button type="button" class="acao ' + classes + '" data-acao="' + acao + '">' +
      esc(rotulo) + '</button>';
  }

  function renderAcoes(c, p) {
    var html = '';
    if (p.assumir) html += botao('assumir', 'Assumir', 'bg-primary text-white');
    if (p.transferir) html += botao('transferir', 'Transferir', 'bg-gray-900 text-white');
    if (p.atribuir) html += botao('transferir', 'Atribuir a…', 'bg-gray-900 text-white');
    if (p.liberar) html += botao('liberar', 'Liberar para a fila', 'bg-gray-100 text-gray-700');
    if (p.devolver_bot) html += botao('devolver-bot', 'Devolver ao EMA', 'bg-blue-50 text-blue-700');
    if (p.resolver) html += botao('resolver', 'Resolver', 'bg-green-50 text-green-800');
    if (p.reabrir) html += botao('reabrir', 'Reabrir', 'bg-gray-100 text-gray-700');
    els.actions.innerHTML = html;
  }

  function renderContexto(data) {
    var m = data.motorista;
    var partes = [];
    if (m) {
      partes.push(
        '<div><p class="text-xs uppercase tracking-wider text-gray-400 font-bold">Motorista</p>' +
        '<p class="font-extrabold text-gray-900 mt-1">' + esc(m.nome) + '</p></div>' +
        linha('Telefone', m.telefone) +
        linha('Cidade', [m.cidade, m.uf].filter(Boolean).join(' / ')) +
        linha('Veículo', m.veiculo) + linha('Placa', m.placa) +
        '<div><span class="status-pill ' + (m.validado ? 'bg-green-100 text-green-800' : 'bg-yellow-100 text-yellow-800') + '">' +
          (m.validado ? 'Cadastro validado' : 'Cadastro pendente') + '</span></div>'
      );
    } else {
      partes.push('<p class="text-sm text-gray-400">Número não vinculado a nenhum motorista cadastrado.</p>');
    }

    var ev = data.eventos || [];
    if (ev.length) {
      partes.push(
        '<div class="pt-3 border-t border-gray-100">' +
        '<p class="text-xs uppercase tracking-wider text-gray-400 font-bold mb-2">Histórico</p>' +
        ev.map(function (e) {
          var alvo = e.para ? ' → ' + esc(e.para) : '';
          return '<div class="text-xs text-gray-600 mb-1.5">' +
            '<span class="text-gray-400">' + esc(hora(e.quando)) + '</span> ' +
            '<strong>' + esc(e.ator) + '</strong> ' + esc(NOMES_ACAO[e.acao] || e.acao) + alvo +
            '</div>';
        }).join('') +
        '</div>'
      );
    }
    els.context.innerHTML = '<div class="p-4 space-y-3">' + partes.join('') + '</div>';
  }

  function linha(rotulo, valor) {
    if (!valor) return '';
    return '<div><p class="text-xs text-gray-400">' + esc(rotulo) + '</p>' +
           '<p class="text-sm text-gray-800">' + esc(valor) + '</p></div>';
  }

  // ── Ações da fila ──────────────────────────────────────────────────────

  async function executar(acao) {
    if (!state.atual || !state.detalhe) return;
    var id = state.atual;
    var c = state.detalhe.conversa || {};
    var base = '/conversas/api/conversas/' + encodeURIComponent(id);
    var res;

    if (acao === 'transferir') { mostrarTransferencia(); return; }

    // "de" é o dono que ESTA tela viu. Se mudou, o servidor recusa em vez de
    // deixar um admin atropelar uma transferência que acabou de acontecer.
    var de = c.assigned_agent_id || null;

    if (acao === 'assumir') res = await postar(base + '/assumir');
    else if (acao === 'liberar') res = await postar(base + '/liberar', { de: de });
    else if (acao === 'devolver-bot') res = await postar(base + '/devolver-bot', { de: de });
    else if (acao === 'reabrir') res = await postar(base + '/status', { status: 'aberta' });
    else if (acao === 'resolver') {
      // Última atividade que esta tela conhecia, exatamente como veio do
      // servidor. Se chegou mensagem depois, a resolução é recusada.
      res = await postar(base + '/status', { status: 'resolvida', visto_ate: c.last_activity_at });
    } else return;

    tratarResposta(res, id);
  }

  function tratarResposta(res, id) {
    var corpo = res.corpo || {};
    if (res.ok) {
      aviso(corpo.mensagem || 'Feito.', corpo.codigo === 'ja_era_sua' ? 'info' : 'success');
    } else if (res.status === 409) {
      // Alguém agiu antes. O servidor já diz quem e o quê.
      aviso(corpo.error || 'A conversa mudou. A tela foi atualizada.');
    } else {
      aviso(corpo.error || 'Não foi possível concluir a ação.');
    }
    abrir(id, true);
    agendarLista();
  }

  async function mostrarTransferencia() {
    if (!state.atendentes) {
      var res = await pedir('/conversas/api/atendentes');
      if (!res.ok) { aviso('Não foi possível carregar os atendentes.'); return; }
      state.atendentes = res.corpo.atendentes || [];
    }
    var c = (state.detalhe && state.detalhe.conversa) || {};
    var opcoes = state.atendentes.filter(function (a) { return a.id !== c.assigned_agent_id; });
    if (!opcoes.length) { aviso('Não há outro atendente ativo para receber a conversa.'); return; }
    els.transferTo.innerHTML = opcoes.map(function (a) {
      return '<option value="' + a.id + '">' + esc(a.nome) + (a.id === EU ? ' (você)' : '') + '</option>';
    }).join('');
    els.transfer.classList.remove('hidden');
    els.transferTo.focus();
  }

  function esconderTransferencia() {
    els.transfer.classList.add('hidden');
  }

  async function confirmarTransferencia() {
    if (!state.atual || !state.detalhe) return;
    var id = state.atual;
    var c = state.detalhe.conversa || {};
    var para = parseInt(els.transferTo.value, 10);
    esconderTransferencia();
    var res = await postar('/conversas/api/conversas/' + encodeURIComponent(id) + '/transferir',
                           { para: para, de: c.assigned_agent_id || null });
    tratarResposta(res, id);
  }

  // ── Envio ──────────────────────────────────────────────────────────────

  async function enviar(ev) {
    ev.preventDefault();
    if (!state.atual) return;
    var texto = els.input.value.trim();
    if (!texto) return;
    var id = state.atual;

    els.send.disabled = true;
    try {
      var res = await postar('/conversas/api/conversas/' + encodeURIComponent(id) + '/enviar',
                             { texto: texto });
      if (res.ok) {
        els.input.value = '';
      } else if (res.status === 409) {
        // Outro atendente assumiu antes. A mensagem NÃO foi enviada e o
        // texto fica no campo, para não se perder.
        aviso((res.corpo.error || 'Outro atendente assumiu esta conversa.') +
              ' Sua mensagem não foi enviada.');
      } else {
        aviso(res.corpo.error || 'Não foi possível enviar a mensagem.');
      }
      abrir(id, true);
      agendarLista();
    } catch (e) {
      aviso(e.message || 'Não foi possível enviar a mensagem.');
    } finally {
      els.send.disabled = false;
    }
  }

  // ── Tempo real ─────────────────────────────────────────────────────────

  function aoAtualizar(data) {
    agendarLista();
    if (!data || data.conversation_id !== state.atual) return;

    // Reação imediata, antes do recarregamento: se outra pessoa acabou de
    // pegar a conversa que está aberta aqui, o campo trava na hora.
    if (data.assigned_agent_id && data.assigned_agent_id !== EU && data.status !== 'resolvida') {
      els.input.disabled = true;
      els.send.disabled = true;
      els.lock.textContent = 'Em atendimento com ' + (data.assigned_agent || 'outro atendente') +
        '. Você pode ler, mas só quem está com a conversa responde.';
      els.lock.classList.remove('hidden');
    }
    agendarDetalhe();
  }

  var socketLigado = null;
  function bindSocket(s) {
    if (!s || !s.on || socketLigado === s) return;
    socketLigado = s;
    s.on('conversa_atualizada', aoAtualizar);
    // Reconexão: eventos podem ter se perdido enquanto estava fora.
    s.on('connect', function () {
      agendarLista();
      agendarDetalhe();
    });
  }

  // ── Ligações ───────────────────────────────────────────────────────────

  els.items.addEventListener('click', function (ev) {
    var btn = ev.target.closest('.conv-item');
    if (btn) abrir(parseInt(btn.dataset.id, 10));
  });

  els.actions.addEventListener('click', function (ev) {
    var btn = ev.target.closest('[data-acao]');
    if (!btn) return;
    btn.disabled = true;
    executar(btn.dataset.acao).finally(function () { btn.disabled = false; });
  });

  els.transferOk.addEventListener('click', confirmarTransferencia);
  els.transferCancel.addEventListener('click', esconderTransferencia);

  els.form.addEventListener('submit', enviar);
  els.input.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); enviar(ev); }
  });

  if (els.refresh) {
    els.refresh.addEventListener('click', function () {
      carregarLista();
      if (state.atual) abrir(state.atual, true);
    });
  }

  var debounce;
  els.search.addEventListener('input', function (ev) {
    clearTimeout(debounce);
    var v = ev.target.value.trim();
    debounce = setTimeout(function () { state.busca = v; carregarLista(); }, 300);
  });

  els.tabs.addEventListener('click', function (ev) {
    var btn = ev.target.closest('.conv-tab');
    if (!btn) return;
    state.aba = btn.dataset.aba;
    Array.prototype.forEach.call(els.tabs.querySelectorAll('.conv-tab'), function (b) {
      b.classList.toggle('is-on', b === btn);
    });
    carregarLista();
  });

  // window.socket pode ainda não existir: notifications.js só instancia no
  // DOMContentLoaded. Liga nos dois caminhos, como static/js/contracting.js.
  bindSocket(window.socket);
  window.addEventListener('emalog:socket-ready', function (e) {
    bindSocket(e.detail && e.detail.socket);
  }, { once: true });

  carregarLista();
})();
