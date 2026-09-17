/* Central de Atendimento — alerta global de mensagem nova (Fase 3).
 *
 * Carregado em TODAS as telas internas, para atendentes. Quem está em Fretes
 * ou em Cotações também precisa saber que um motorista escreveu.
 *
 * Alerta só o que precisa de gente:
 *   - mensagem nova numa conversa do próprio atendente;
 *   - mensagem nova numa conversa livre que está fora do EMA.
 * Conversa que o bot está conduzindo não apita: seria ruído o dia inteiro.
 *
 * O número do badge vem do servidor, em /conversas/api/contadores, e não de
 * uma soma local de eventos. Evento perdido não deixa o número errado: a
 * reconexão do socket busca de novo.
 *
 * Nenhum texto de mensagem passa por aqui. O evento só traz identificadores,
 * e a notificação do navegador usa frase genérica, que pode aparecer na tela
 * de bloqueio.
 */
(function () {
  'use strict';

  var body = document.body;
  var PAPEIS = ['admin', 'operador', 'vendedor'];
  if (!body || PAPEIS.indexOf(body.dataset.userRole) === -1) return;
  var EU = parseInt(body.dataset.userId, 10);

  var CHAVE_SOM = 'emalog.conversas.som';

  function somLigado() {
    try { return localStorage.getItem(CHAVE_SOM) !== 'off'; } catch (e) { return true; }
  }

  function definirSom(ligado) {
    try { localStorage.setItem(CHAVE_SOM, ligado ? 'on' : 'off'); } catch (e) { /* sem armazenamento */ }
  }

  // ── Som ────────────────────────────────────────────────────────────────
  // O navegador só libera áudio depois de um gesto do usuário na página.
  var audio = null;
  function destravarAudio() {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!audio) audio = new Ctx();
    if (audio.state === 'suspended') audio.resume();
  }
  ['click', 'keydown', 'touchstart'].forEach(function (ev) {
    document.addEventListener(ev, destravarAudio, { passive: true });
  });

  function tocar() {
    if (!somLigado() || !audio || audio.state !== 'running') return;
    var agora = audio.currentTime;
    [[880, 0], [1175, 0.16]].forEach(function (par) {
      var osc = audio.createOscillator();
      var ganho = audio.createGain();
      osc.type = 'sine';
      osc.frequency.value = par[0];
      ganho.gain.setValueAtTime(0.0001, agora + par[1]);
      ganho.gain.exponentialRampToValueAtTime(0.18, agora + par[1] + 0.02);
      ganho.gain.exponentialRampToValueAtTime(0.0001, agora + par[1] + 0.14);
      osc.connect(ganho).connect(audio.destination);
      osc.start(agora + par[1]);
      osc.stop(agora + par[1] + 0.15);
    });
  }

  // ── Badge no menu e título da aba ──────────────────────────────────────
  var tituloBase = document.title;

  function linkConversas() {
    var links = document.querySelectorAll('a.sb-nav-item');
    for (var i = 0; i < links.length; i++) {
      if (/\/conversas\/?$/.test(links[i].getAttribute('href') || '')) return links[i];
    }
    return null;
  }

  function mostrarBadge(n) {
    var link = linkConversas();
    if (link) {
      var b = link.querySelector('.conv-alerta-badge');
      if (!b) {
        b = document.createElement('span');
        b.className = 'conv-alerta-badge ml-auto inline-flex min-w-5 h-5 items-center justify-center ' +
                      'rounded-full bg-red-500 px-1.5 text-[11px] font-bold text-white';
        b.setAttribute('aria-label', 'conversas esperando');
        link.appendChild(b);
      }
      b.textContent = n > 99 ? '99+' : String(n);
      b.classList.toggle('hidden', !n);
    }
    document.title = n ? '(' + (n > 99 ? '99+' : n) + ') ' + tituloBase : tituloBase;
  }

  var timer = null;
  async function atualizar() {
    try {
      var r = await fetch('/conversas/api/contadores', { credentials: 'include' });
      if (!r.ok) return;
      var c = await r.json();
      mostrarBadge(c.alerta || 0);
    } catch (e) { /* sem rede: tenta no próximo evento */ }
  }

  function agendar() {
    clearTimeout(timer);
    timer = setTimeout(atualizar, 400);
  }

  // ── Eventos ────────────────────────────────────────────────────────────

  function precisaDeGente(d) {
    if (!d || !d.nova_mensagem || d.status === 'resolvida') return false;
    if (d.assigned_agent_id === EU) return true;
    return !d.assigned_agent_id && d.handling_mode === 'manual';
  }

  function notificarNavegador(d) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    if (document.visibilityState === 'visible') return;
    try {
      var n = new Notification('Nova mensagem no WhatsApp', {
        body: d.assigned_agent_id === EU ? 'Numa conversa sua.' : 'Uma conversa está esperando atendimento.',
        tag: 'emalog-conversa-' + d.conversation_id
      });
      n.onclick = function () { window.focus(); window.location.href = '/conversas/'; };
    } catch (e) { /* navegador recusou */ }
  }

  function aoEvento(d) {
    agendar();
    if (!precisaDeGente(d)) return;
    // Quem já está com a conversa aberta e visível não precisa do apito.
    if (window.EmalogConversaAberta === d.conversation_id && document.visibilityState === 'visible') return;
    tocar();
    notificarNavegador(d);
  }

  var socketLigado = null;
  function bindSocket(s) {
    if (!s || !s.on || socketLigado === s) return;
    socketLigado = s;
    s.on('conversa_atualizada', aoEvento);
    s.on('connect', agendar);
  }

  bindSocket(window.socket);
  window.addEventListener('emalog:socket-ready', function (e) {
    bindSocket(e.detail && e.detail.socket);
  }, { once: true });

  window.EmalogAlertaConversas = {
    somLigado: somLigado,
    definirSom: definirSom,
    tocarTeste: function () { destravarAudio(); setTimeout(tocar, 50); },
    notificacoesSuportadas: function () { return 'Notification' in window; },
    permissaoNotificacoes: function () { return 'Notification' in window ? Notification.permission : 'unsupported'; },
    pedirPermissao: function () {
      return 'Notification' in window ? Notification.requestPermission() : Promise.resolve('unsupported');
    },
    atualizar: atualizar
  };

  atualizar();
})();
