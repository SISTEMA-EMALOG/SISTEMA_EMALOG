/**
 * Sistema de Notificações EMALOG - Versão Corrigida
 * SocketIO com debug melhorado
 */

class NotificationManager {
    constructor() {
        this.socket = null;
        this.isConnected = false;
        this.debugMode = true;
        this.init();
    }

    init() {
        console.log('🔔 Inicializando sistema de notificações EMALOG...');
        console.log('👤 Usuário atual:', window.currentUser);
        this.setupSocket();
    }

    log(message, data = null) {
        if (this.debugMode) {
            console.log(`[NotificationManager] ${message}`, data || '');
        }
    }

    setupSocket() {
        if (typeof io === 'undefined') {
            console.error('❌ SocketIO não encontrado');
            return;
        }

        console.log('📡 Configurando SocketIO...');

        this.socket = io({
            autoConnect: true,
            reconnection: true,
            reconnectionDelay: 2000,
            reconnectionAttempts: 10,
            timeout: 10000,
            transports: ['websocket', 'polling']
        });
        // Shared connection for pages that need live updates (for example the
        // contracting Kanban). Consumers must not create a second socket.
        window.socket = this.socket;
        window.dispatchEvent(new CustomEvent('emalog:socket-ready', { detail: { socket: this.socket } }));

        // Eventos de conexão
        this.socket.on('connect', () => {
            this.log('✅ SocketIO conectado');
            this.isConnected = true;
            this.socket.emit('join_notifications');
        });

        this.socket.on('disconnect', () => {
            this.log('❌ SocketIO desconectado');
            this.isConnected = false;
        });

        // Status de notificações
        this.socket.on('notification_status', (data) => {
            this.log('📡 Status de notificações recebido:', data);
        });

        this.socket.on('test_connectivity', (data) => {
            this.log('🧪 Teste de conectividade:', data);
        });

        // Notificações específicas
        this.socket.on('quote_priced_notification', (data) => {
            this.log('🎉 COTAÇÃO PRECIFICADA recebida!', data);
            if (window.currentUser && window.currentUser.role === 'cliente') {
                this.log('✅ Exibindo para cliente');
                this.showQuotePricedNotification(data);
            } else {
                this.log('ℹ️ Ignorando - usuário não é cliente');
            }
        });

        this.socket.on('new_quote_notification', (data) => {
            this.log('🚨 NOVA COTAÇÃO recebida!', data);
            if (window.currentUser && window.currentUser.role !== 'cliente') {
                this.log('✅ Exibindo para operador/admin');
                this.showNewQuoteNotification(data);
            } else {
                this.log('ℹ️ Ignorando - usuário é cliente');
            }
        });

        this.socket.on('quote_approved_notification', (data) => {
            this.log('✅ Cotação aprovada recebida:', data);
            this.showQuoteApprovedNotification(data);
        });

        this.socket.on('freight_status_notification', (data) => {
            this.log('🚛 Status do frete recebido:', data);
            this.showFreightStatusNotification(data);
        });

        // Notificações de negociação
        this.socket.on('quote_counter_proposal_notification', (data) => {
            this.log('📩 CONTRA-PROPOSTA recebida!', data);
            if (window.currentUser && window.currentUser.role !== 'cliente') {
                this.log('✅ Exibindo contra-proposta para operador/admin');
                this.showCounterProposalNotification(data);
            } else {
                this.log('ℹ️ Ignorando contra-proposta - usuário é cliente');
            }
        });

        this.socket.on('quote_negotiation_response_notification', (data) => {
            this.log('📤 RESPOSTA DE NEGOCIAÇÃO recebida!', data);
            if (window.currentUser && window.currentUser.role === 'cliente') {
                this.log('✅ Exibindo resposta para cliente');
                this.showNegotiationResponseNotification(data);
            } else {
                this.log('ℹ️ Ignorando resposta - usuário não é cliente');
            }
        });
    }

    showQuotePricedNotification(data) {
        this.createNotificationBalloon(
            '🎉 COTAÇÃO AVALIADA!',
            `Cotação ${data.quote_number} foi avaliada por R$ ${parseFloat(data.sale_value).toLocaleString('pt-BR', {minimumFractionDigits: 2})}`,
            'success',
            () => window.location.href = `/quotes/${data.quote_id}`,
            true
        );
        this.playSuccessSound();
    }

    showNewQuoteNotification(data) {
        this.createNotificationBalloon(
            '🚨 NOVA COTAÇÃO!',
            `${data.client_name} enviou a cotação ${data.quote_number}`,
            'urgent',
            () => window.location.href = `/quotes/${data.quote_id}`,
            true
        );
        this.playUrgentSound();
    }

    showQuoteApprovedNotification(data) {
        this.createNotificationBalloon(
            '✅ Cotação Aprovada!',
            `${data.client_name} aprovou a cotação ${data.quote_number}`,
            'success',
            () => window.location.href = `/freight/${data.freight_id}`
        );
    }

    showFreightStatusNotification(data) {
        this.createNotificationBalloon(
            '🚛 Frete Atualizado!',
            `Frete ${data.freight_number} - ${data.new_status}`,
            'info',
            () => window.location.href = `/freight/${data.freight_id}`
        );
    }

    createNotificationBalloon(title, message, type, onClick, isUrgent = false) {
        // Remover balões existentes
        const existing = document.querySelectorAll('.notification-balloon');
        existing.forEach(b => b.remove());

        const colors = {
            'success': { bg: 'bg-green-500', border: 'border-green-500', icon: 'fas fa-check-circle' },
            'urgent': { bg: 'bg-red-500', border: 'border-red-500', icon: 'fas fa-exclamation-triangle' },
            'info': { bg: 'bg-blue-500', border: 'border-blue-500', icon: 'fas fa-info-circle' }
        };

        const config = colors[type] || colors.info;
        const urgentClass = isUrgent ? 'animate-bounce shadow-2xl border-4' : 'shadow-xl border-l-4';

        const balloon = document.createElement('div');
        balloon.className = `notification-balloon fixed top-4 right-4 z-50 max-w-lg w-full transform transition-all duration-300 translate-x-full`;

        balloon.innerHTML = `
            <div class="bg-white rounded-lg ${urgentClass} ${config.border} p-6">
                <div class="flex items-start">
                    <div class="flex-shrink-0">
                        <div class="w-12 h-12 ${config.bg} rounded-full flex items-center justify-center ${isUrgent ? 'animate-pulse' : ''}">
                            <i class="${config.icon} text-white text-xl"></i>
                        </div>
                    </div>
                    <div class="ml-4 w-0 flex-1">
                        <p class="text-lg font-bold text-gray-800 ${isUrgent ? 'animate-pulse' : ''}">${title}</p>
                        <p class="mt-2 text-sm text-gray-600">${message}</p>
                        <div class="mt-4 flex space-x-3">
                            <button data-action="go"
                                    class="flex-1 ${config.bg} text-white px-4 py-3 rounded-lg font-bold hover:opacity-90 transition-all duration-200 ${isUrgent ? 'animate-bounce text-lg' : ''}">
                                🚀 VER AGORA
                            </button>
                            <button data-action="close"
                                    class="px-4 py-2 bg-gray-300 text-gray-700 rounded-lg hover:bg-gray-400 transition-colors">
                                Fechar
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `;

        // Attach event listeners properly to preserve closure over onClick
        balloon.querySelector('[data-action="go"]').addEventListener('click', function() {
            onClick();
            balloon.remove();
        });
        balloon.querySelector('[data-action="close"]').addEventListener('click', function() {
            balloon.remove();
        });

        document.body.appendChild(balloon);
        setTimeout(() => balloon.classList.remove('translate-x-full'), 100);

        // Remover automaticamente se não for urgente
        if (!isUrgent) {
            setTimeout(() => {
                if (balloon.parentElement) {
                    balloon.classList.add('translate-x-full');
                    setTimeout(() => balloon.remove(), 300);
                }
            }, 15000);
        }
    }

    playSuccessSound() {
        try {
            const audioContext = new (window.AudioContext || window.webkitAudioContext)();
            [600, 800, 1000].forEach((freq, i) => {
                setTimeout(() => {
                    const osc = audioContext.createOscillator();
                    const gain = audioContext.createGain();
                    osc.connect(gain);
                    gain.connect(audioContext.destination);
                    osc.frequency.value = freq;
                    osc.type = 'sine';
                    gain.gain.setValueAtTime(0.1, audioContext.currentTime);
                    gain.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.3);
                    osc.start();
                    osc.stop(audioContext.currentTime + 0.3);
                }, i * 150);
            });
        } catch (e) {
            console.log('Som não disponível');
        }
    }

    playUrgentSound() {
        try {
            const audioContext = new (window.AudioContext || window.webkitAudioContext)();
            [0, 0.3, 0.6].forEach((delay) => {
                setTimeout(() => {
                    const osc = audioContext.createOscillator();
                    const gain = audioContext.createGain();
                    osc.connect(gain);
                    gain.connect(audioContext.destination);
                    osc.frequency.value = 1200;
                    osc.type = 'square';
                    gain.gain.setValueAtTime(0.15, audioContext.currentTime);
                    gain.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.2);
                    osc.start();
                    osc.stop(audioContext.currentTime + 0.2);
                }, delay * 1000);
            });
        } catch (e) {
            console.log('Som não disponível');
        }
    }

    showCounterProposalNotification(data) {
        const title = '📩 Nova Contra-Proposta!';
        const message = `${data.client_name} propôs R$ ${parseFloat(data.counter_value).toLocaleString('pt-BR', {minimumFractionDigits: 2})} (original: R$ ${parseFloat(data.original_value).toLocaleString('pt-BR', {minimumFractionDigits: 2})})`;
        
        this.createNotificationBalloon(
            title,
            `Cotação ${data.quote_number}: ${message}`,
            'urgent',
            () => window.location.href = `/quotes/${data.quote_id}`,
            true
        );
        this.playUrgentSound();
    }

    showNegotiationResponseNotification(data) {
        const title = data.is_final ? '⚠️ Proposta Final!' : '🔄 Nova Proposta!';
        const message = `${data.is_final ? 'FINAL' : 'Nova proposta'}: R$ ${parseFloat(data.new_value).toLocaleString('pt-BR', {minimumFractionDigits: 2})}`;
        
        this.createNotificationBalloon(
            title,
            `Cotação ${data.quote_number}: ${message}`,
            data.is_final ? 'urgent' : 'info',
            () => window.location.href = `/quotes/${data.quote_id}`,
            data.is_final
        );
        
        if (data.is_final) {
            this.playUrgentSound();
        } else {
            this.playSuccessSound();
        }
    }

    test() {
        console.log('🧪 Testando notificação...');
        const testData = {
            quote_id: 999,
            quote_number: 'TESTE-' + Date.now(),
            sale_value: 1500.75,
            client_name: 'Teste Cliente'
        };
        this.showQuotePricedNotification(testData);
    }
}

// Inicializar quando DOM estiver pronto
let notificationManager;
document.addEventListener('DOMContentLoaded', () => {
    notificationManager = new NotificationManager();
    window.notificationManager = notificationManager;
});

// Função de teste global
function testNotification() {
    if (notificationManager) {
        notificationManager.test();
    } else {
        alert('Sistema não inicializado');
    }
}
