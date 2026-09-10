/**
 * EMALOG Chat System
 * Handles real-time chat functionality with Socket.IO
 */

let socket;
let currentRoom;
let currentUser;
let currentUserId;
let isTyping = false;
let typingTimeout;

function initializeChat(roomId, username, userId) {
    currentRoom = roomId;
    currentUser = username;
    currentUserId = userId;
    
    // Initialize Socket.IO
    socket = io();
    
    // Setup event listeners
    setupSocketEvents();
    setupUIEvents();
    
    // Join the room
    socket.emit('join', { room: roomId });
    
    // Scroll to bottom of messages
    scrollToBottom();
}

function setupSocketEvents() {
    // Connection events
    socket.on('connect', function() {
        console.log('Connected to chat server');
        updateConnectionStatus(true);
    });
    
    socket.on('disconnect', function() {
        console.log('Disconnected from chat server');
        updateConnectionStatus(false);
    });
    
    // Message events
    socket.on('message', function(data) {
        addMessageToChat(data);
        scrollToBottom();
    });
    
    socket.on('file_message', function(data) {
        addFileMessageToChat(data);
        scrollToBottom();
    });
    
    // Status events
    socket.on('status', function(data) {
        showChatNotification(data.msg);
    });
    
    // Typing events
    socket.on('typing', function(data) {
        if (data.username !== currentUser) {
            showTypingIndicator(data.username, data.typing);
        }
    });
    
    // Error events
    socket.on('error', function(data) {
        showNotification(data.message, 'error');
    });
}

function setupUIEvents() {
    const messageForm = document.getElementById('messageForm');
    const messageInput = document.getElementById('messageInput');
    const fileInput = document.getElementById('fileInput');
    
    // Message form submission
    if (messageForm) {
        messageForm.addEventListener('submit', function(e) {
            e.preventDefault();
            sendMessage();
        });
    }
    
    // Message input events
    if (messageInput) {
        messageInput.addEventListener('keypress', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });
        
        // Typing indicator
        messageInput.addEventListener('input', function() {
            if (!isTyping) {
                isTyping = true;
                socket.emit('typing', { room: currentRoom, typing: true });
            }
            
            clearTimeout(typingTimeout);
            typingTimeout = setTimeout(() => {
                isTyping = false;
                socket.emit('typing', { room: currentRoom, typing: false });
            }, 1000);
        });
    }
    
    // File input change
    if (fileInput) {
        fileInput.addEventListener('change', function() {
            if (this.files.length > 0) {
                validateSelectedFile(this.files[0]);
            }
        });
    }
}

function sendMessage() {
    const messageInput = document.getElementById('messageInput');
    const message = messageInput.value.trim();
    
    if (message) {
        socket.emit('message', {
            room: currentRoom,
            message: message
        });
        
        messageInput.value = '';
        
        // Stop typing indicator
        if (isTyping) {
            isTyping = false;
            socket.emit('typing', { room: currentRoom, typing: false });
        }
    }
}

function sendFile() {
    const fileInput = document.getElementById('fileInput');
    const file = fileInput.files[0];
    
    if (!file) {
        showNotification('Nenhum arquivo selecionado', 'error');
        return;
    }
    
    // Validate file
    if (!validateFile(file)) {
        return;
    }
    
    // Convert file to base64
    const reader = new FileReader();
    reader.onload = function(e) {
        socket.emit('file_upload', {
            room: currentRoom,
            file_data: e.target.result,
            filename: file.name
        });
        
        // Clear file input and hide upload area
        fileInput.value = '';
        toggleFileUpload();
        
        showNotification('Arquivo enviado!', 'success');
    };
    
    reader.readAsDataURL(file);
}

function addMessageToChat(data) {
    const messagesContainer = document.getElementById('messagesContainer');
    if (!messagesContainer) return;
    
    const messageDiv = document.createElement('div');
    messageDiv.className = `flex ${data.is_own ? 'justify-end' : 'justify-start'}`;
    
    const messageContent = `
        <div class="max-w-xs lg:max-w-md">
            ${!data.is_own ? `
                <div class="flex items-center mb-1">
                    <div class="w-6 h-6 bg-primary rounded-full flex items-center justify-center mr-2">
                        <span class="text-xs font-medium text-white">${data.username[0].toUpperCase()}</span>
                    </div>
                    <span class="text-xs text-gray-500">${data.username}</span>
                    <span class="text-xs text-gray-400 ml-2">${data.timestamp}</span>
                </div>
            ` : ''}
            
            <div class="px-4 py-2 rounded-lg ${data.is_own ? 'bg-primary text-white' : 'bg-gray-100 text-gray-900'}">
                <p class="text-sm">${escapeHtml(data.message)}</p>
            </div>
            
            ${data.is_own ? `
                <div class="text-right mt-1">
                    <span class="text-xs text-gray-400">${data.timestamp}</span>
                </div>
            ` : ''}
        </div>
    `;
    
    messageDiv.innerHTML = messageContent;
    messagesContainer.appendChild(messageDiv);
}

function addFileMessageToChat(data) {
    const messagesContainer = document.getElementById('messagesContainer');
    if (!messagesContainer) return;
    
    const messageDiv = document.createElement('div');
    messageDiv.className = 'flex justify-start';
    
    const messageContent = `
        <div class="max-w-xs lg:max-w-md">
            <div class="flex items-center mb-1">
                <div class="w-6 h-6 bg-primary rounded-full flex items-center justify-center mr-2">
                    <span class="text-xs font-medium text-white">${data.username[0].toUpperCase()}</span>
                </div>
                <span class="text-xs text-gray-500">${data.username}</span>
                <span class="text-xs text-gray-400 ml-2">${data.timestamp}</span>
            </div>
            
            <div class="px-4 py-2 rounded-lg bg-gray-100 text-gray-900">
                <div class="flex items-center">
                    <i class="fas fa-file mr-2"></i>
                    <a href="${data.file_path}" class="text-blue-600 hover:text-blue-800 underline" target="_blank">
                        ${data.filename}
                    </a>
                </div>
            </div>
        </div>
    `;
    
    messageDiv.innerHTML = messageContent;
    messagesContainer.appendChild(messageDiv);
}

function showTypingIndicator(username, typing) {
    const indicator = document.getElementById('typingIndicator');
    if (!indicator) return;
    
    if (typing) {
        indicator.innerHTML = `
            <div class="typing-indicator">
                <span></span>
                <span></span>
                <span></span>
            </div>
            <span class="ml-2 text-xs">${username} está digitando...</span>
        `;
    } else {
        indicator.innerHTML = '';
    }
}

function showChatNotification(message) {
    const messagesContainer = document.getElementById('messagesContainer');
    if (!messagesContainer) return;
    
    const notificationDiv = document.createElement('div');
    notificationDiv.className = 'flex justify-center my-2';
    notificationDiv.innerHTML = `
        <div class="bg-gray-200 text-gray-600 text-xs px-3 py-1 rounded-full">
            ${escapeHtml(message)}
        </div>
    `;
    
    messagesContainer.appendChild(notificationDiv);
    scrollToBottom();
}

function updateConnectionStatus(connected) {
    const statusElements = document.querySelectorAll('[data-connection-status]');
    statusElements.forEach(element => {
        if (connected) {
            element.innerHTML = `
                <span class="w-2 h-2 bg-green-400 rounded-full mr-2"></span>
                <span class="text-sm text-gray-600">Conectado</span>
            `;
        } else {
            element.innerHTML = `
                <span class="w-2 h-2 bg-red-400 rounded-full mr-2"></span>
                <span class="text-sm text-gray-600">Desconectado</span>
            `;
        }
    });
}

function scrollToBottom() {
    const messagesContainer = document.getElementById('messagesContainer');
    if (messagesContainer) {
        messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }
}

function toggleFileUpload() {
    const fileUploadArea = document.getElementById('fileUploadArea');
    if (fileUploadArea) {
        fileUploadArea.classList.toggle('hidden');
    }
}

function validateFile(file) {
    const maxSize = 16 * 1024 * 1024; // 16MB
    const allowedTypes = [
        'application/pdf',
        'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'image/jpeg',
        'image/jpg',
        'image/png',
        'image/gif'
    ];
    
    if (file.size > maxSize) {
        showNotification('Arquivo muito grande. Máximo 16MB.', 'error');
        return false;
    }
    
    if (!allowedTypes.includes(file.type)) {
        showNotification('Tipo de arquivo não permitido.', 'error');
        return false;
    }
    
    return true;
}

function validateSelectedFile(file) {
    const fileUploadArea = document.getElementById('fileUploadArea');
    const sendButton = fileUploadArea?.querySelector('button[onclick="sendFile()"]');
    
    if (validateFile(file)) {
        if (sendButton) {
            sendButton.disabled = false;
            sendButton.classList.remove('opacity-50');
        }
        showNotification(`Arquivo selecionado: ${file.name}`, 'success');
    } else {
        if (sendButton) {
            sendButton.disabled = true;
            sendButton.classList.add('opacity-50');
        }
    }
}

function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    
    return text.replace(/[&<>"']/g, function(m) { return map[m]; });
}

// Auto-reconnection logic
function setupAutoReconnect() {
    socket.on('disconnect', function() {
        let attempts = 0;
        const maxAttempts = 5;
        
        const reconnectInterval = setInterval(() => {
            if (attempts < maxAttempts) {
                console.log(`Tentativa de reconexão ${attempts + 1}/${maxAttempts}`);
                socket.connect();
                attempts++;
            } else {
                clearInterval(reconnectInterval);
                showNotification('Não foi possível reconectar ao chat. Recarregue a página.', 'error');
            }
        }, 3000);
        
        socket.on('connect', function() {
            clearInterval(reconnectInterval);
            showNotification('Reconectado ao chat!', 'success');
            socket.emit('join', { room: currentRoom });
        });
    });
}

// Cleanup on page unload
window.addEventListener('beforeunload', function() {
    if (socket) {
        socket.emit('leave', { room: currentRoom });
        socket.disconnect();
    }
});

// Export functions
window.initializeChat = initializeChat;
window.sendMessage = sendMessage;
window.sendFile = sendFile;
window.toggleFileUpload = toggleFileUpload;
