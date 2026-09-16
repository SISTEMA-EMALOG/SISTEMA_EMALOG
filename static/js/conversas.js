/* Central de Atendimento — Fase 1.
 *
 * Convenções seguidas, todas verificadas no projeto:
 *  - Reaproveita o socket compartilhado window.socket, criado por
 *    notifications.js. Abrir um segundo io() duplica handlers e ping.
 *  - Todo texto vindo do WhatsApp passa por esc() antes de ir ao DOM. O
 *    remetente não é autenticado, e showNotification() do main.js interpola
 *    sem escapar — por isso a faixa de status desta tela é própria.
 *  - O CSRF é injetado pelo wrapper global de fetch definido no
 *    dashboard_base.html, que só existe a partir de {% block scripts %}.
 */
(function () {
  'use strict';

  var $ = function (sel) { return document.querySelector(sel); };

  var state = {
    conversas: [],
    atual: null,
    filtro: 'ativas',
    busca: '',
    carregando: false
  };

  var els = {
    items: $('#convItems'),
    skeleton: $('#convSkeleton'),
    messages: $('#convMessages'),
    form: $('#convForm'),
    input: $('#convInput'),
    send: $('#convSend'),
    title: $('#convTitle'),
    subtitle: $('#convSubtitle'),
    pill: $('#convStatusPill'),
    resolve: $('#convResolve'),
    context: $('#convContext'),
    search: $('#convSearch'),
    status: $('#convStatus'),
    refresh: $('#convRefresh')
  };

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function aviso(msg, tipo) {
    if (!els.status) return;
    els.status.textContent = msg;
    els.status.className = 'rounded-lg border px-4 py-3 text-sm mb-4 ' +
      (tipo === 'success'
        ? 'bg-green-50 border-green-200 text-green-800'
        : 'bg-red-50 border-red-200 text-red-800');
    setTimeout(function () { els.status.classList.add('hidden'); }, 4500);
  }

  function hora(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d)) return '';
    var hoje = new Date();
    var mesmoDia = d.toDateString() === hoje.toDateString();
    return mesmoDia
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

  // ── Lista ──────────────────────────────────────────────────────────────

  async function carregarLista() {
    if (state.carregando) return;
    state.carregando = true;
    try {
      var params = new URLSearchParams();
      if (state.filtro) params.set('status', state.filtro);
      if (state.busca) params.set('busca', state.busca);
      var r = await fetch('/conversas/api/conversas?' + params.toString(), {
        credentials: 'include'
      });
      if (!r.ok) throw new Error('Não foi possível carregar as conversas.');
      var data = await r.json();
      state.conversas = data.conversas || [];
      renderLista();
    } catch (e) {
      aviso(e.message || 'Não foi possível carregar as conversas.');
      if (els.skeleton) els.skeleton.remove();
    } finally {
      state.carregando = false;
    }
  }

  function renderLista() {
    if (els.skeleton) { els.skeleton.remove(); els.skeleton = null; }
    if (!state.conversas.length) {
      els.items.innerHTML =
        '<div class="p-6 text-center text-gray-400">' +
        '<i class="fas fa-inbox text-2xl mb-2" aria-hidden="true"></i>' +
        '<p class="text-sm">Nenhuma conversa aqui.</p></div>';
      return;
    }
    els.items.innerHTML = state.conversas.map(function (c) {
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
          '<div class="mt-1"><span class="status-pill ' + (CORES_STATUS[c.status] || CORES_STATUS.resolvida) + '">' +
            esc(c.status) + '</span></div>' +
        '</button>';
    }).join('');
  }

  // ── Thread ─────────────────────────────────────────────────────────────

  async function abrir(id) {
    state.atual = id;
    renderLista();
    els.messages.innerHTML = '<div class="p-4 space-y-3">' +
      '<div class="skeleton h-12 rounded-lg w-2/3"></div>' +
      '<div class="skeleton h-12 rounded-lg w-1/2 ml-auto"></div></div>';
    try {
      var r = await fetch('/conversas/api/conversas/' + encodeURIComponent(id), {
        credentials: 'include'
      });
      if (!r.ok) throw new Error('Não foi possível abrir a conversa.');
      var data = await r.json();
      renderThread(data);
      var item = state.conversas.find(function (c) { return c.id === id; });
      if (item) { item.nao_lidas = 0; renderLista(); }
    } catch (e) {
      aviso(e.message || 'Não foi possível abrir a conversa.');
      els.messages.innerHTML = '';
    }
  }

  function renderThread(data) {
    var c = data.conversa || {};
    els.title.textContent = c.nome || telefoneLegivel(c.contact_phone);
    els.subtitle.textContent = telefoneLegivel(c.contact_phone);

    els.pill.className = 'status-pill ' + (CORES_STATUS[c.status] || CORES_STATUS.resolvida);
    els.pill.textContent = c.status || '';
    els.pill.classList.remove('hidden');
    els.resolve.classList.toggle('hidden', c.status === 'resolvida');

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

    var podeEnviar = !!data.pode_enviar;
    els.input.disabled = !podeEnviar;
    els.send.disabled = !podeEnviar;
    els.input.placeholder = podeEnviar
      ? 'Escreva a resposta e pressione Enter'
      : 'Configure um canal de WhatsApp para poder responder';

    var m = data.motorista;
    els.context.innerHTML = m
      ? '<div class="p-4 space-y-3">' +
          '<div><p class="text-xs uppercase tracking-wider text-gray-400 font-bold">Motorista</p>' +
          '<p class="font-extrabold text-gray-900 mt-1">' + esc(m.nome) + '</p></div>' +
          linha('Telefone', m.telefone) + linha('Cidade', [m.cidade, m.uf].filter(Boolean).join(' / ')) +
          linha('Veículo', m.veiculo) + linha('Placa', m.placa) +
          '<div><span class="status-pill ' + (m.validado ? 'bg-green-100 text-green-800' : 'bg-yellow-100 text-yellow-800') + '">' +
            (m.validado ? 'Cadastro validado' : 'Cadastro pendente') + '</span></div>' +
        '</div>'
      : '<div class="p-4 text-sm text-gray-400">' +
        'Número não vinculado a nenhum motorista cadastrado.</div>';
  }

  function linha(rotulo, valor) {
    if (!valor) return '';
    return '<div><p class="text-xs text-gray-400">' + esc(rotulo) + '</p>' +
           '<p class="text-sm text-gray-800">' + esc(valor) + '</p></div>';
  }

  // ── Envio ──────────────────────────────────────────────────────────────

  async function enviar(ev) {
    ev.preventDefault();
    if (!state.atual) return;
    var texto = els.input.value.trim();
    if (!texto) return;

    els.send.disabled = true;
    try {
      var r = await fetch('/conversas/api/conversas/' + encodeURIComponent(state.atual) + '/enviar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ texto: texto })
      });
      var body = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(body.error || 'Não foi possível enviar a mensagem.');
      els.input.value = '';
      await abrir(state.atual);
      carregarLista();
    } catch (e) {
      aviso(e.message || 'Não foi possível enviar a mensagem.');
    } finally {
      els.send.disabled = false;
    }
  }

  async function resolver() {
    if (!state.atual) return;
    try {
      var r = await fetch('/conversas/api/conversas/' + encodeURIComponent(state.atual) + '/status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ status: 'resolvida' })
      });
      var body = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(body.error || 'Não foi possível resolver a conversa.');
      aviso('Conversa resolvida.', 'success');
      await abrir(state.atual);
      carregarLista();
    } catch (e) {
      aviso(e.message || 'Não foi possível resolver a conversa.');
    }
  }

  // ── Tempo real ─────────────────────────────────────────────────────────

  function bindSocket(s) {
    if (!s || !s.on) return;
    s.on('conversa_atualizada', function (data) {
      carregarLista();
      if (data && data.conversation_id === state.atual) abrir(state.atual);
    });
  }

  // ── Ligações ───────────────────────────────────────────────────────────

  els.items.addEventListener('click', function (ev) {
    var btn = ev.target.closest('.conv-item');
    if (btn) abrir(parseInt(btn.dataset.id, 10));
  });

  els.form.addEventListener('submit', enviar);

  els.input.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); enviar(ev); }
  });

  els.resolve.addEventListener('click', resolver);

  if (els.refresh) els.refresh.addEventListener('click', function () { carregarLista(); });

  var debounce;
  els.search.addEventListener('input', function (ev) {
    clearTimeout(debounce);
    var v = ev.target.value.trim();
    debounce = setTimeout(function () { state.busca = v; carregarLista(); }, 300);
  });

  document.getElementById('convFilters').addEventListener('click', function (ev) {
    var btn = ev.target.closest('.conv-filter');
    if (!btn) return;
    state.filtro = btn.dataset.status;
    Array.prototype.forEach.call(this.querySelectorAll('.conv-filter'), function (b) {
      var on = b === btn;
      b.className = 'conv-filter px-2.5 py-1.5 rounded-lg text-xs font-bold ' +
        (on ? 'bg-gray-900 text-white' : 'bg-gray-100 text-gray-700');
    });
    carregarLista();
  });

  // window.socket ainda pode não existir quando este script roda, porque
  // notifications.js só instancia dentro do DOMContentLoaded. Liga nos dois
  // caminhos, como faz static/js/contracting.js.
  bindSocket(window.socket);
  window.addEventListener('emalog:socket-ready', function (e) {
    bindSocket(e.detail && e.detail.socket);
  }, { once: true });

  carregarLista();
})();
