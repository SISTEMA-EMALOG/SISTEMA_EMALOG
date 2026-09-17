/* Relatórios da Central de Atendimento — Fase 4.
 *
 * A tela só formata. Toda conta é feita no servidor, em
 * atendimento_conversas/utils/relatorios.py, para que a tela, o CSV e os
 * testes mostrem exatamente os mesmos números.
 */
(function () {
  'use strict';

  var $ = function (s) { return document.querySelector(s); };
  var app = $('#relApp');
  if (!app) return;

  var HOJE = app.dataset.hoje;
  var grafico = null;

  var els = {
    form: $('#relForm'), de: $('#relDe'), ate: $('#relAte'), status: $('#relStatus'),
    atendentes: $('#relAtendentes'), semanas: $('#relSemanas'),
    csvAtendentes: $('#relCsvAtendentes'), csvDias: $('#relCsvDias')
  };

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function num(n) { return (n == null ? 0 : n).toLocaleString('pt-BR'); }

  function duracao(segundos) {
    if (segundos == null) return '—';
    var s = Math.round(segundos);
    if (s < 60) return s + ' s';
    var m = Math.floor(s / 60);
    if (m < 60) return m + ' min' + (s % 60 ? ' ' + (s % 60) + ' s' : '');
    var h = Math.floor(m / 60);
    return h + ' h ' + String(m % 60).padStart(2, '0') + ' min';
  }

  function dataCurta(iso) {
    var p = iso.split('-');
    return p[2] + '/' + p[1];
  }

  function somarDias(iso, dias) {
    var d = new Date(iso + 'T12:00:00');
    d.setDate(d.getDate() + dias);
    return d.toISOString().slice(0, 10);
  }

  function aviso(msg) {
    els.status.textContent = msg;
    els.status.className = 'rounded-lg border px-4 py-3 text-sm bg-red-50 border-red-200 text-red-800';
  }

  function atualizarCsv() {
    var q = 'de=' + encodeURIComponent(els.de.value) + '&ate=' + encodeURIComponent(els.ate.value);
    els.csvAtendentes.href = '/conversas/api/relatorios.csv?tipo=atendentes&' + q;
    els.csvDias.href = '/conversas/api/relatorios.csv?tipo=dias&' + q;
  }

  function kpi(nome, valor) {
    var el = document.querySelector('[data-kpi="' + nome + '"]');
    if (el) el.textContent = valor;
  }

  async function carregar() {
    atualizarCsv();
    els.status.classList.add('hidden');
    var q = 'de=' + encodeURIComponent(els.de.value) + '&ate=' + encodeURIComponent(els.ate.value);
    try {
      var r = await fetch('/conversas/api/relatorios?' + q, { credentials: 'include' });
      var corpo = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(corpo.error || 'Não foi possível gerar o relatório.');
      render(corpo);
    } catch (e) {
      aviso(e.message || 'Não foi possível gerar o relatório.');
    }
  }

  function render(rel) {
    var s = rel.resumo, t = s.tempo_resposta;
    kpi('recebidas', num(s.recebidas));
    kpi('enviadas_atendente', num(s.enviadas_atendente));
    kpi('bot', num(s.enviadas_bot) + ' do bot, ' + num(s.enviadas_automacao) + ' de automação');
    kpi('mediana', duracao(t.mediana_segundos));
    kpi('media', t.quantidade ? 'média ' + duracao(t.media_segundos) + ', ' + num(t.quantidade) + ' respostas' : 'sem respostas medidas');
    kpi('sem_resposta', num(s.esperas_sem_resposta));
    kpi('abertas_resolvidas', num(s.conversas_abertas) + ' / ' + num(s.conversas_resolvidas));

    Object.keys(rel.agora).forEach(function (k) {
      var el = document.querySelector('[data-agora="' + k + '"]');
      if (el) el.textContent = num(rel.agora[k]);
    });

    els.atendentes.innerHTML = rel.atendentes.length
      ? rel.atendentes.map(function (a) {
          var tr = a.tempo_resposta;
          return '<tr><td class="font-bold text-gray-900">' + esc(a.nome) + '</td>' +
            '<td class="num">' + num(a.conversas_atendidas) + '</td>' +
            '<td class="num">' + num(a.mensagens_enviadas) + '</td>' +
            '<td class="num">' + num(a.assumidas) + '</td>' +
            '<td class="num">' + num(a.recebidas_por_transferencia) + '</td>' +
            '<td class="num">' + num(a.resolvidas) + '</td>' +
            '<td class="num">' + duracao(tr.mediana_segundos) + '</td>' +
            '<td class="num">' + duracao(tr.media_segundos) + '</td>' +
            '<td class="num">' + num(tr.quantidade) + '</td></tr>';
        }).join('')
      : '<tr><td colspan="9" class="text-gray-400">Nenhum atendente atuou neste período.</td></tr>';

    els.semanas.innerHTML = rel.semanas.map(function (w) {
      var rotulo = dataCurta(w.inicio) + ' a ' + dataCurta(w.fim) +
        (w.dias_no_periodo < 7 ? ' <span class="text-gray-400">(' + w.dias_no_periodo + ' dia' + (w.dias_no_periodo > 1 ? 's' : '') + ' no período)</span>' : '');
      return '<tr><td>' + rotulo + '</td>' +
        '<td class="num">' + num(w.recebidas) + '</td>' +
        '<td class="num">' + num(w.enviadas_atendente) + '</td>' +
        '<td class="num">' + num(w.enviadas_bot) + '</td>' +
        '<td class="num">' + num(w.enviadas_automacao) + '</td>' +
        '<td class="num">' + num(w.conversas_abertas) + '</td>' +
        '<td class="num">' + num(w.conversas_resolvidas) + '</td></tr>';
    }).join('');

    desenharGrafico(rel.dias);
  }

  function desenharGrafico(dias) {
    if (typeof Chart === 'undefined') return;
    if (grafico) grafico.destroy();
    grafico = new Chart(document.getElementById('relGrafico'), {
      type: 'bar',
      data: {
        labels: dias.map(function (d) { return dataCurta(d.dia); }),
        datasets: [
          { label: 'Recebidas', data: dias.map(function (d) { return d.recebidas; }), backgroundColor: '#374151' },
          { label: 'Atendentes', data: dias.map(function (d) { return d.enviadas_atendente; }), backgroundColor: '#E61D43' },
          { label: 'Bot', data: dias.map(function (d) { return d.enviadas_bot; }), backgroundColor: '#3B82F6' }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom' } },
        scales: { y: { beginAtZero: true, ticks: { precision: 0 } } }
      }
    });
  }

  function aplicarPreset(dias) {
    els.ate.value = HOJE;
    els.de.value = somarDias(HOJE, -(dias - 1));
    Array.prototype.forEach.call(document.querySelectorAll('.preset'), function (b) {
      b.classList.toggle('is-on', parseInt(b.dataset.dias, 10) === dias);
    });
  }

  document.querySelector('[role="group"]').addEventListener('click', function (ev) {
    var b = ev.target.closest('.preset');
    if (!b) return;
    aplicarPreset(parseInt(b.dataset.dias, 10));
    carregar();
  });

  els.form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    Array.prototype.forEach.call(document.querySelectorAll('.preset'), function (b) {
      b.classList.remove('is-on');
    });
    carregar();
  });

  [els.de, els.ate].forEach(function (el) { el.addEventListener('change', atualizarCsv); });

  aplicarPreset(7);
  carregar();
})();
