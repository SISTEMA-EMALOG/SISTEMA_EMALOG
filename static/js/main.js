/**
 * EMALOG Main JavaScript Functions
 * Handles common functionality across the application
 */

// Global variables
let loadingOverlay;
let notificationContainer;

// Initialize application
document.addEventListener('DOMContentLoaded', function() {
    initializeApp();

    // Update current time
    function updateCurrentTime() {
        const now = new Date();
        const timeString = now.toLocaleString('pt-BR', {
            day: '2-digit',
            month: '2-digit',
            year: 'numeric',
            hour: '2-digit',
            minute: '2-digit'
        });
        const timeElement = document.getElementById('currentTime');
        if (timeElement) {
            timeElement.textContent = timeString;
        }
    }

    // Update time immediately and then every minute
    updateCurrentTime();
    setInterval(updateCurrentTime, 60000);
});

function initializeApp() {
    // Create loading overlay
    loadingOverlay = document.getElementById('loadingOverlay');

    // Create notification container
    createNotificationContainer();

    // Initialize tooltips
    initializeTooltips();

    // Initialize dropdowns
    initializeDropdowns();

    // Auto-hide flash messages
    autoHideFlashMessages();

    // Initialize sidebar toggle for mobile
    initializeSidebarToggle();

    // Initialize form validations
    initializeFormValidations();

    // Initialize date inputs with current date
    initializeDateInputs();
}

// Loading functions
function showLoading() {
    if (loadingOverlay) {
        loadingOverlay.classList.remove('hidden');
    }
}

function hideLoading() {
    if (loadingOverlay) {
        loadingOverlay.classList.add('hidden');
    }
}

// Notification system
function createNotificationContainer() {
    if (!document.getElementById('notificationContainer')) {
        notificationContainer = document.createElement('div');
        notificationContainer.id = 'notificationContainer';
        notificationContainer.className = 'fixed top-4 right-4 z-50 space-y-2';
        document.body.appendChild(notificationContainer);
    } else {
        notificationContainer = document.getElementById('notificationContainer');
    }
}

function showNotification(message, type = 'info', duration = 5000) {
    const notification = document.createElement('div');
    notification.className = `notification ${type} flex items-center justify-between p-4 rounded-lg shadow-lg`;

    const iconMap = {
        'success': 'fas fa-check-circle',
        'error': 'fas fa-times-circle',
        'warning': 'fas fa-exclamation-triangle',
        'info': 'fas fa-info-circle'
    };

    notification.innerHTML = `
        <div class="flex items-center">
            <i class="${iconMap[type]} mr-3"></i>
            <span>${message}</span>
        </div>
        <button onclick="this.parentElement.remove()" class="ml-4 text-current opacity-70 hover:opacity-100">
            <i class="fas fa-times"></i>
        </button>
    `;

    notificationContainer.appendChild(notification);

    // Auto remove after duration
    setTimeout(() => {
        if (notification.parentElement) {
            notification.remove();
        }
    }, duration);
}

// Sidebar toggle for mobile
function initializeSidebarToggle() {
    window.toggleSidebar = function() {
        const sidebar = document.getElementById('sidebar');
        if (sidebar) {
            sidebar.classList.toggle('-translate-x-full');
        }
    };
}

// Dropdown functionality
function initializeDropdowns() {
    window.toggleDropdown = function(dropdownId) {
        const dropdown = document.getElementById(dropdownId);
        if (dropdown) {
            dropdown.classList.toggle('hidden');
        }

        // Close other dropdowns
        document.querySelectorAll('[id$="Menu"], [id$="Dropdown"]').forEach(element => {
            if (element.id !== dropdownId && !element.classList.contains('hidden')) {
                element.classList.add('hidden');
            }
        });
    };

    // Close dropdowns when clicking outside
    document.addEventListener('click', function(event) {
        if (!event.target.closest('[onclick*="toggleDropdown"]')) {
            document.querySelectorAll('[id$="Menu"], [id$="Dropdown"]').forEach(element => {
                element.classList.add('hidden');
            });
        }
    });
}

// Auto-hide flash messages
function autoHideFlashMessages() {
    setTimeout(() => {
        document.querySelectorAll('.flash-message').forEach(message => {
            message.style.opacity = '0';
            setTimeout(() => message.remove(), 300);
        });
    }, 5000);
}

// Form validations
function initializeFormValidations() {
    // CPF validation
    document.querySelectorAll('input[name="cpf"]').forEach(input => {
        input.addEventListener('blur', function() {
            if (this.value && !validateCPF(this.value)) {
                showNotification('CPF inválido', 'error');
                this.focus();
            }
        });
    });

    // CNPJ validation
    document.querySelectorAll('input[name="cnpj"]').forEach(input => {
        input.addEventListener('blur', function() {
            if (this.value && !validateCNPJ(this.value)) {
                showNotification('CNPJ inválido', 'error');
                this.focus();
            }
        });
    });

    // Email validation
    document.querySelectorAll('input[type="email"]').forEach(input => {
        input.addEventListener('blur', function() {
            if (this.value && !validateEmail(this.value)) {
                showNotification('E-mail inválido', 'error');
                this.focus();
            }
        });
    });
}

// Validation functions
function validateCPF(cpf) {
    cpf = cpf.replace(/[^\d]+/g, '');

    if (cpf.length !== 11 || /^(\d)\1{10}$/.test(cpf)) {
        return false;
    }

    let sum = 0;
    for (let i = 0; i < 9; i++) {
        sum += parseInt(cpf.charAt(i)) * (10 - i);
    }

    let rev = 11 - (sum % 11);
    if (rev === 10 || rev === 11) rev = 0;
    if (rev !== parseInt(cpf.charAt(9))) return false;

    sum = 0;
    for (let i = 0; i < 10; i++) {
        sum += parseInt(cpf.charAt(i)) * (11 - i);
    }

    rev = 11 - (sum % 11);
    if (rev === 10 || rev === 11) rev = 0;
    if (rev !== parseInt(cpf.charAt(10))) return false;

    return true;
}

function validateCNPJ(cnpj) {
    cnpj = cnpj.replace(/[^\d]+/g, '');

    if (cnpj.length !== 14 || /^(\d)\1{13}$/.test(cnpj)) {
        return false;
    }

    let length = cnpj.length - 2;
    let numbers = cnpj.substring(0, length);
    let digits = cnpj.substring(length);
    let sum = 0;
    let pos = length - 7;

    for (let i = length; i >= 1; i--) {
        sum += numbers.charAt(length - i) * pos--;
        if (pos < 2) pos = 9;
    }

    let result = sum % 11 < 2 ? 0 : 11 - sum % 11;
    if (result !== parseInt(digits.charAt(0))) return false;

    length += 1;
    numbers = cnpj.substring(0, length);
    sum = 0;
    pos = length - 7;

    for (let i = length; i >= 1; i--) {
        sum += numbers.charAt(length - i) * pos--;
        if (pos < 2) pos = 9;
    }

    result = sum % 11 < 2 ? 0 : 11 - sum % 11;
    if (result !== parseInt(digits.charAt(1))) return false;

    return true;
}

function validateEmail(email) {
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    return emailRegex.test(email);
}

// Date input initialization
function initializeDateInputs() {
    const today = new Date().toISOString().split('T')[0];

    // Set minimum date for future dates
    document.querySelectorAll('input[type="date"][name*="expiry"], input[type="date"][name*="valid"]').forEach(input => {
        if (!input.value) {
            input.min = today;
        }
    });

    // Set default date for new records
    document.querySelectorAll('input[type="date"][name*="date"]').forEach(input => {
        if (!input.value && !input.hasAttribute('data-no-default')) {
            input.value = today;
        }
    });
}

// Tooltip initialization
function initializeTooltips() {
    // Simple tooltip implementation
    document.querySelectorAll('[data-tooltip]').forEach(element => {
        element.addEventListener('mouseenter', function() {
            const tooltipText = this.getAttribute('data-tooltip');
            const tooltip = document.createElement('div');
            tooltip.className = 'absolute bg-gray-900 text-white text-xs rounded py-1 px-2 z-50';
            tooltip.textContent = tooltipText;
            tooltip.id = 'tooltip';

            this.appendChild(tooltip);
        });

        element.addEventListener('mouseleave', function() {
            const tooltip = this.querySelector('#tooltip');
            if (tooltip) {
                tooltip.remove();
            }
        });
    });
}

// File upload helpers
function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';

    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));

    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function validateFileType(file, allowedTypes) {
    return allowedTypes.includes(file.type) || allowedTypes.some(type => file.name.toLowerCase().endsWith(type));
}

function validateFileSize(file, maxSizeBytes) {
    return file.size <= maxSizeBytes;
}

// Currency formatting
function formatCurrency(value) {
    return new Intl.NumberFormat('pt-BR', {
        style: 'currency',
        currency: 'BRL'
    }).format(value);
}

function parseCurrency(value) {
    return parseFloat(value.replace(/[^\d,]/g, '').replace(',', '.')) || 0;
}

// Date formatting
function formatDate(date, format = 'dd/MM/yyyy') {
    if (!date) return '';

    const d = new Date(date);
    const day = String(d.getDate()).padStart(2, '0');
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const year = d.getFullYear();
    const hours = String(d.getHours()).padStart(2, '0');
    const minutes = String(d.getMinutes()).padStart(2, '0');

    switch (format) {
        case 'dd/MM/yyyy':
            return `${day}/${month}/${year}`;
        case 'dd/MM/yyyy HH:mm':
            return `${day}/${month}/${year} ${hours}:${minutes}`;
        case 'yyyy-MM-dd':
            return `${year}-${month}-${day}`;
        default:
            return d.toLocaleDateString('pt-BR');
    }
}

// Debounce function for search inputs
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// Local storage helpers
function saveToLocalStorage(key, data) {
    try {
        localStorage.setItem(key, JSON.stringify(data));
    } catch (error) {
        console.error('Error saving to localStorage:', error);
    }
}

function loadFromLocalStorage(key, defaultValue = null) {
    try {
        const item = localStorage.getItem(key);
        return item ? JSON.parse(item) : defaultValue;
    } catch (error) {
        console.error('Error loading from localStorage:', error);
        return defaultValue;
    }
}

// Print functionality
function printPage() {
    window.print();
}

function printElement(elementId) {
    const element = document.getElementById(elementId);
    if (!element) return;

    const printWindow = window.open('', '_blank');
    printWindow.document.write(`
        <!DOCTYPE html>
        <html>
        <head>
            <title>Impressão - EMALOG</title>
            <link href="https://cdn.tailwindcss.com" rel="stylesheet">
            <style>
                @media print {
                    body { font-size: 12px; }
                    .no-print { display: none !important; }
                }
            </style>
        </head>
        <body class="p-4">
            ${element.innerHTML}
        </body>
        </html>
    `);

    printWindow.document.close();
    printWindow.focus();
    printWindow.print();
    printWindow.close();
}

// Table helpers
function sortTable(tableId, columnIndex, type = 'string') {
    const table = document.getElementById(tableId);
    if (!table) return;

    const tbody = table.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));

    rows.sort((a, b) => {
        const aValue = a.cells[columnIndex].textContent.trim();
        const bValue = b.cells[columnIndex].textContent.trim();

        if (type === 'number') {
            return parseFloat(aValue) - parseFloat(bValue);
        } else if (type === 'date') {
            return new Date(aValue) - new Date(bValue);
        } else {
            return aValue.localeCompare(bValue);
        }
    });

    rows.forEach(row => tbody.appendChild(row));
}

function filterTable(tableId, searchValue) {
    const table = document.getElementById(tableId);
    if (!table) return;

    const rows = table.querySelectorAll('tbody tr');
    const searchTerm = searchValue.toLowerCase();

    rows.forEach(row => {
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(searchTerm) ? '' : 'none';
    });
}

// Modal helpers
function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.remove('hidden');
        modal.classList.add('modal-enter');
    }
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.add('hidden');
        modal.classList.remove('modal-enter');
    }
}

// Export functions to global scope
window.showLoading = showLoading;
window.hideLoading = hideLoading;
window.showNotification = showNotification;
window.formatCurrency = formatCurrency;
window.formatDate = formatDate;
window.validateCPF = validateCPF;
window.validateCNPJ = validateCNPJ;
window.validateEmail = validateEmail;
window.debounce = debounce;
window.printPage = printPage;
window.printElement = printElement;
window.sortTable = sortTable;
window.filterTable = filterTable;
window.openModal = openModal;
window.closeModal = closeModal;
window.saveToLocalStorage = saveToLocalStorage;
window.loadFromLocalStorage = loadFromLocalStorage;