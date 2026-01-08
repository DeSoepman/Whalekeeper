let isRestarting = false;

async function fetchData(endpoint) {
    const response = await fetch(endpoint);
    if (response.status === 401) {
        // Don't redirect to login during restart
        if (!isRestarting) {
            window.location.href = '/login';
        }
        throw new Error('Unauthorized');
    }
    return await response.json();
}

function formatDate(dateString) {
    const date = new Date(dateString);
    const options = { day: 'numeric', month: 'short', year: 'numeric' };
    return date.toLocaleDateString('en-GB', options);
}

function formatDateTime(dateString) {
    const date = new Date(dateString);
    const dateOptions = { day: 'numeric', month: 'short', year: 'numeric' };
    const timeOptions = { hour: '2-digit', minute: '2-digit' };
    return date.toLocaleDateString('en-GB', dateOptions) + ', ' + date.toLocaleTimeString('en-GB', timeOptions);
}

function toggleMobileMenu() {
    const navMenu = document.querySelector('.nav-menu');
    const hamburger = document.querySelector('.hamburger');
    const isOpening = !navMenu.classList.contains('active');
    
    if (isOpening) {
        // Store current scroll position
        const scrollY = window.scrollY;
        document.body.style.top = `-${scrollY}px`;
        document.body.classList.add('menu-open');
    } else {
        // Restore scroll position
        const scrollY = document.body.style.top;
        document.body.classList.remove('menu-open');
        document.body.style.top = '';
        window.scrollTo(0, parseInt(scrollY || '0') * -1);
    }
    
    navMenu.classList.toggle('active');
    hamburger.classList.toggle('active');
}

async function loadContainers() {
    const data = await fetchData('/api/containers');
    const container = document.getElementById('containers');
    
    if (data.length === 0) {
        container.innerHTML = '<p>No containers being monitored</p>';
        return;
    }
    
    const monitored = data.filter(c => c.monitored);
    const notMonitored = data.filter(c => !c.monitored);
    
    const renderContainer = (c) => 
        '<div class="container-card" data-container="' + c.name + '">' +
            '<div class="container-name">' +
                '<span>' + c.name + '</span>' +
                '<div style="display: flex; align-items: center; gap: 8px;">' +
                    '<span class="container-status ' + c.status + '" title="Status: ' + c.status + '"></span>' +
                    '<span class="container-menu-icon" onclick="toggleContainerMenu(event, \'' + c.name + '\', ' + c.monitored + ')">⋮</span>' +
                '</div>' +
            '</div>' +
            '<div class="container-image">' + c.image + '</div>' +
            (c.version ? '<div class="container-id">Version: ' + c.version + '</div>' : '') +
            '<div class="checking-overlay">' +
                '<span class="loading-spinner"></span>' +
                '<span>Checking...</span>' +
            '</div>' +
            '<div class="container-menu" id="menu-' + c.name + '">' +
                '<div class="container-menu-item ' + (c.monitored ? 'danger' : 'success') + '" onclick="toggleMonitoring(\'' + c.name + '\', ' + !c.monitored + ')">' +
                    (c.monitored ? '✕ Disable Monitoring' : '✓ Enable Monitoring') +
                '</div>' +
                '<div class="container-menu-item" onclick="checkContainer(\'' + c.name + '\')">🔍 Check for Updates</div>' +
            '</div>' +
        '</div>';
    
    let html = '';
    
    if (monitored.length > 0) {
        html += '<div class="containers-section">' +
                '<div class="section-heading">Monitored</div>' +
                '<div class="container-grid">' + monitored.map(renderContainer).join('') + '</div>' +
                '</div>';
    }
    
    if (notMonitored.length > 0) {
        html += '<div class="containers-section">' +
                '<div class="section-heading">Not Monitored</div>' +
                '<div class="container-grid">' + notMonitored.map(renderContainer).join('') + '</div>' +
                '</div>';
    }
    
    container.innerHTML = html;
    
    // Update rollback dropdown with containers that have versions
    await loadRollbackContainers();
}

async function loadRollbackContainers() {
    try {
        const containers = await fetchData('/api/rollback-containers');
        const select = document.getElementById('rollback-container');
        
        if (containers.length === 0) {
            select.innerHTML = '<option value="">No containers with rollback versions</option>';
        } else {
            select.innerHTML = '<option value="">Select container...</option>' +
                containers.map(c => '<option value="' + c.name + '">' + c.name + ' (' + c.version_count + ' versions)</option>').join('');
        }
    } catch (error) {
        console.error('Failed to load rollback containers:', error);
    }
}

async function loadHistory() {
    const data = await fetchData('/api/history');
    const history = document.getElementById('history');
    
    if (data.length === 0) {
        history.innerHTML = '<p>No update history yet</p>';
        return;
    }
    
    history.innerHTML = data.map(h => {
        // Only show arrow and images if both old and new images exist
        const showImages = h.old_image && h.new_image;
        const imageDisplay = showImages 
            ? '<div class="history-tags">' + h.old_image + ' → ' + h.new_image + '</div>'
            : '';
        
        return '<div class="history-item ' + h.status + '">' +
            '<div class="history-badge"><span class="badge ' + h.status + '">' + h.status.toUpperCase() + '</span></div>' +
            '<div class="history-content">'+
            '<div class="history-title"><strong>' + h.container_name + '</strong> (' + formatDateTime(h.timestamp) + ')</div>' +
            imageDisplay +
            (h.message ? '<div class="history-message">' + h.message + '</div>' : '') +
            '</div>' +
        '</div>';
    }).join('');
}

async function checkNow() {
    try {
        // Get all monitored containers
        const containers = await fetchData('/api/containers');
        const monitoredContainers = containers.filter(c => c.monitored);
        
        if (monitoredContainers.length === 0) {
            showModal(
                'No Containers',
                'No containers are currently being monitored',
                'OK',
                () => closeModal(),
                false,
                true
            );
            return;
        }
        
        // Show checking modal
        showModal(
            'Checking for Updates',
            '<div><span class="loading-spinner"></span>Checking ' + monitoredContainers.length + ' container' + (monitoredContainers.length > 1 ? 's' : '') + ' for updates...</div>',
            null,
            null,
            true
        );
        
        // Trigger the check
        const response = await fetch('/api/check-now', { method: 'POST' });
        const result = await response.json();
        
        // Wait a bit then refresh
        setTimeout(async () => {
            await refreshData();
            
            // Get updated container data to check for updates
            const updatedContainers = await fetchData('/api/containers');
            const containersWithUpdates = updatedContainers.filter(c => c.has_update);
            
            let resultMessage;
            if (containersWithUpdates.length === 0) {
                resultMessage = 'All containers are up to date!';
            } else if (containersWithUpdates.length === 1) {
                resultMessage = `1 container has an update available: ${containersWithUpdates[0].name}`;
            } else {
                resultMessage = `${containersWithUpdates.length} containers have updates available`;
            }
            
            // Show result modal
            showModal(
                'Check Complete',
                resultMessage,
                'OK',
                () => closeModal(),
                false,
                true
            );
        }, 2000);
        
    } catch (error) {
        alert('Error triggering update check: ' + error);
    }
}

async function checkContainer(containerName) {
    // Step 1: Confirm check for updates
    showModal(
        'Check for Updates',
        `Do you want to check <strong>${containerName}</strong> for available updates?`,
        'Check',
        async () => {
            // Step 2: Show checking status
            showModal(
                'Checking for Updates',
                `<div><span class="loading-spinner"></span>Checking ${containerName} for updates...</div>`,
                null,
                null,
                true
            );
            
            try {
                const response = await fetch('/api/check-container', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ container_name: containerName, check_only: true })
                });
                
                const result = await response.json();
                
                if (result.update_available) {
                    // Step 3: Updates found - ask to update
                    showModal(
                        'Update Available',
                        `A new version is available for <strong>${containerName}</strong>.<br><br>` +
                        `Current: <code>${result.current_image}</code><br>` +
                        `New: <code>${result.new_image}</code><br><br>` +
                        `Do you want to update now?`,
                        'Update Now',
                        async () => {
                            // Step 4: Perform update
                            showModal(
                                'Updating Container',
                                `<div><span class="loading-spinner"></span>Updating ${containerName}...</div>`,
                                null,
                                null,
                                true
                            );
                            
                            const updateResponse = await fetch('/api/update-container', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ container_name: containerName })
                            });
                            
                            const updateResult = await updateResponse.json();
                            
                            // Step 5: Show result
                            showModal(
                                updateResult.success ? 'Update Successful' : 'Update Failed',
                                updateResult.message,
                                'OK',
                                () => {
                                    closeModal();
                                    refreshData();
                                },
                                false,
                                true  // Hide cancel button
                            );
                        }
                    );
                } else {
                    // Step 3: No updates found
                    showModal(
                        'No Updates Available',
                        `<strong>${containerName}</strong> is already running the latest version.<br><br>` +
                        `Current image: <code>${result.current_image}</code>`,
                        'OK',
                        () => closeModal(),
                        false,
                        true  // Hide cancel button
                    );
                }
            } catch (error) {
                showModal(
                    'Error',
                    `Failed to check for updates: ${error}`,
                    'OK',
                    () => closeModal(),
                    false,
                    true  // Hide cancel button
                );
            }
        }
    );
}

// Modal functions
let modalCallback = null;

function showModal(title, body, confirmText = 'OK', onConfirm = null, hideButtons = false, hideCancelButton = false) {
    const modal = document.getElementById('updateModal');
    const header = document.getElementById('modalHeader');
    const bodyEl = document.getElementById('modalBody');
    const footer = document.getElementById('modalFooter');
    const confirmBtn = document.getElementById('modalConfirmBtn');
    const cancelBtn = document.getElementById('modalCancelBtn');
    
    header.textContent = title;
    bodyEl.innerHTML = body;
    
    if (hideButtons) {
        footer.style.display = 'none';
    } else {
        footer.style.display = 'flex';
        confirmBtn.textContent = confirmText;
        modalCallback = onConfirm;
        
        // Show/hide cancel button
        if (hideCancelButton) {
            cancelBtn.style.display = 'none';
        } else {
            cancelBtn.style.display = 'inline-block';
        }
    }
    
    modal.classList.add('active');
}

function closeModal() {
    const modal = document.getElementById('updateModal');
    modal.classList.remove('active');
    modalCallback = null;
}

function confirmModalAction() {
    if (modalCallback) {
        modalCallback();
    } else {
        closeModal();
    }
}

// Close modal on outside click
document.addEventListener('click', function(event) {
    const modal = document.getElementById('updateModal');
    if (event.target === modal) {
        closeModal();
    }
});


async function refreshData() {
    await loadContainers();
}

async function refreshLogs() {
    await loadHistory();
}

document.getElementById('rollback-container').addEventListener('change', async function(e) {
    const containerName = e.target.value;
    const versionSelect = document.getElementById('rollback-version');
    
    if (!containerName) {
        versionSelect.innerHTML = '<option value="">Select version...</option>';
        return;
    }
    
    const versions = await fetchData('/api/versions/' + containerName);
    versionSelect.innerHTML = '<option value="">Select version...</option>' +
        versions.map(v => 
            '<option value="' + v.id + '">' +
                (v.display_tag || v.image_tag) + ' (' + formatDate(v.created_at) + ')' +
            '</option>'
        ).join('');
});

async function performRollback() {
    const container = document.getElementById('rollback-container').value;
    const version = document.getElementById('rollback-version').value;
    
    if (!container || !version) {
        showModal(
            'Selection Required',
            'Please select both container and version to rollback.',
            'OK',
            () => closeModal(),
            false,
            true
        );
        return;
    }
    
    // Step 1: Confirm rollback
    showModal(
        'Confirm Rollback',
        `Are you sure you want to rollback <strong>${container}</strong> to a previous version?<br><br>` +
        `This will stop the current container and recreate it with the selected image version.`,
        'Rollback',
        async () => {
            // Step 2: Show rolling back status
            showModal(
                'Rolling Back',
                `<div><span class="loading-spinner"></span>Rolling back ${container}...</div>`,
                null,
                null,
                true
            );
            
            try {
                const response = await fetch('/api/rollback', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ container_name: container, version_id: parseInt(version) })
                });
                
                const result = await response.json();
                
                // Step 3: Show result
                let message = result.message;
                
                // Add compose file instruction if this is a compose-managed container
                if (result.success && result.is_compose && result.image_name) {
                    message += '<br><br><strong>⚠️ Docker Compose Detected</strong><br>' +
                               'To keep this version permanently, update your docker-compose.yml:<br>' +
                               '<code style="display: block; margin-top: 10px; padding: 10px; background: #0a0a0a; border-radius: 4px; color: #00ff88;">' +
                               'image: ' + result.image_name +
                               '</code><br>' +
                               'Then run: <code>docker compose up -d</code>';
                }
                
                showModal(
                    result.success ? 'Rollback Successful' : 'Rollback Failed',
                    message,
                    'OK',
                    () => {
                        closeModal();
                        if (result.success) {
                            refreshData();
                            // Reset rollback selects
                            document.getElementById('rollback-container').value = '';
                            document.getElementById('rollback-version').innerHTML = '<option value="">Select version...</option>';
                        }
                    },
                    false,
                    true
                );
            } catch (error) {
                showModal(
                    'Error',
                    `Failed to perform rollback: ${error}`,
                    'OK',
                    () => closeModal(),
                    false,
                    true
                );
            }
        }
    );
}

// Restart container
async function restartContainer() {
    // Step 1: Confirm restart
    showModal(
        'Restart Container',
        'Are you sure you want to restart the Whalekeeper container?<br><br>This will reload the configuration and briefly interrupt service.',
        'Restart',
        async () => {
            // Set flag to prevent 401 redirects during restart
            isRestarting = true;
            
            // Step 2: Show restarting message
            showModal(
                'Restarting Server',
                '<div><span class="loading-spinner"></span>Restarting container...</div>',
                null,
                null,
                true
            );
            
            // Trigger restart - don't wait for response as connection will drop
            fetch('/api/restart', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            }).catch(() => {
                // Expected - connection will drop during restart
            });
            
            // Wait for server to come back online
            await waitForServer();
            
            // Reload page
            window.location.reload();
        }
    );
}

// Wait for server to come back online
async function waitForServer() {
    const maxAttempts = 30; // 30 seconds max
    const delayMs = 1000; // Check every second
    
    // Wait a bit before starting to check (give server time to restart)
    await new Promise(resolve => setTimeout(resolve, 2000));
    
    for (let i = 0; i < maxAttempts; i++) {
        try {
            const response = await fetch('/api/containers', {
                method: 'GET',
                cache: 'no-cache'
            });
            
            if (response.ok) {
                // Server is back online
                return;
            }
        } catch (error) {
            // Server still down, continue waiting
        }
        
        await new Promise(resolve => setTimeout(resolve, delayMs));
    }
    
    // If we get here, server didn't come back - reload anyway
}

// Cron schedule translator
function translateCron(cronExpression) {
    if (!cronExpression || !cronExpression.trim()) return null;
    
    const parts = cronExpression.trim().split(/\s+/);
    if (parts.length !== 5) return 'Invalid cron format (should be 5 parts: minute hour day month weekday)';
    
    const [minute, hour, day, month, weekday] = parts;
    
    const months = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
    const weekdays = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    
    let description = 'At ';
    
    // Time
    if (minute === '*' && hour === '*') {
        description = 'Every minute';
    } else if (minute.startsWith('*/') && hour === '*') {
        description = `Every ${minute.substring(2)} minutes`;
    } else if (hour.startsWith('*/') && minute === '*') {
        description = `Every ${hour.substring(2)} hours`;
    } else if (hour.startsWith('*/')) {
        const h = hour.substring(2);
        const m = minute === '*' ? '00' : minute.padStart(2, '0');
        description = `Every ${h} hours at minute ${m}`;
    } else {
        const h = hour === '*' ? 'every hour' : hour.padStart(2, '0');
        const m = minute === '*' ? 'every minute' : minute.padStart(2, '0');
        if (hour === '*') {
            description = `At minute ${m} of every hour`;
        } else {
            description += `${h}:${m}`;
        }
    }
    
    // Day of month
    if (day !== '*') {
        description += ` on day ${day}`;
    }
    
    // Month
    if (month !== '*') {
        const monthNum = parseInt(month) - 1;
        if (monthNum >= 0 && monthNum < 12) {
            description += ` in ${months[monthNum]}`;
        } else {
            description += ` in month ${month}`;
        }
    }
    
    // Day of week
    if (weekday !== '*') {
        const dayNum = parseInt(weekday);
        if (dayNum >= 0 && dayNum <= 6) {
            description += day === '*' && month === '*' ? ` on ${weekdays[dayNum]}` : ` (${weekdays[dayNum]})`;
        } else {
            description += ` on day ${weekday}`;
        }
    }
    
    return description;
}

function updateCronDescription() {
    const cronInput = document.getElementById('cron_schedule');
    const cronText = document.getElementById('cron_text');
    
    if (!cronInput || !cronText) return;
    
    const description = translateCron(cronInput.value);
    
    if (description) {
        cronText.textContent = description;
    } else {
        cronText.textContent = 'Enter a cron expression';
    }
}

// Add event listener for cron schedule input
setTimeout(() => {
    const cronInput = document.getElementById('cron_schedule');
    if (cronInput) {
        cronInput.addEventListener('input', updateCronDescription);
        cronInput.addEventListener('blur', updateCronDescription);
        // Update on initial load
        updateCronDescription();
    }
}, 100);

// Initial load
refreshData();

// Auto-refresh every 30 seconds only on dashboard
setInterval(function() {
    const dashboardTab = document.getElementById('dashboard');
    if (dashboardTab.classList.contains('active')) {
        refreshData();
    }
    const logsTab = document.getElementById('logs');
    if (logsTab.classList.contains('active')) {
        refreshLogs();
    }
}, 30000);

// Tab management
function showTab(evt, tabName) {
    // Hide all tabs
    document.querySelectorAll('.tab-content').forEach(function(tab) {
        tab.classList.remove('active');
    });
    document.querySelectorAll('.nav-button').forEach(function(btn) {
        btn.classList.remove('active');
    });
    
    // Show selected tab
    document.getElementById(tabName).classList.add('active');
    evt.target.classList.add('active');
    
    // Close mobile menu after tab selection
    const navMenu = document.querySelector('.nav-menu');
    const hamburger = document.querySelector('.hamburger');
    if (navMenu && navMenu.classList.contains('active')) {
        navMenu.classList.remove('active');
        hamburger.classList.remove('active');
        
        // Restore scroll position
        const scrollY = document.body.style.top;
        document.body.classList.remove('menu-open');
        document.body.style.top = '';
        window.scrollTo(0, parseInt(scrollY || '0') * -1);
    }
    
    // Load config if switching to config tab
    if (tabName === 'config') {
        loadConfig();
    }
    
    // Load logs if switching to logs tab
    if (tabName === 'logs') {
        refreshLogs();
    }
    
    // Load config if switching to notifications tab
    if (tabName === 'notifications') {
        loadConfig();
    }
}

// Notification tab management
function showNotificationTab(evt, tabName) {
    // Hide all notification tabs
    document.querySelectorAll('.notification-tab').forEach(function(tab) {
        tab.classList.remove('active');
    });
    
    // Remove active from all notification tab buttons within the notifications section
    const notificationSection = evt.target.closest('.section');
    notificationSection.querySelectorAll('.tab-button').forEach(function(btn) {
        btn.classList.remove('active');
    });
    
    // Show selected tab
    document.getElementById(tabName).classList.add('active');
    evt.target.classList.add('active');
}

// Load configuration
async function loadConfig() {
    const config = await fetchData('/api/config');
    
    // Basic settings
    document.getElementById('cron_schedule').value = config.cron_schedule || '';
    
    // Update cron description after loading the value
    updateCronDescription();
    
    // Monitoring
    document.getElementById('monitor_all').checked = config.monitoring?.monitor_all || false;
    document.getElementById('exclude_containers').value = (config.monitoring?.exclude_containers || []).join('\n');
    
    // Email
    document.getElementById('email_enabled').checked = config.notifications?.email?.enabled || false;
    document.getElementById('smtp_host').value = config.notifications?.email?.smtp_host || '';
    document.getElementById('smtp_port').value = config.notifications?.email?.smtp_port || 587;
    document.getElementById('use_tls').checked = config.notifications?.email?.use_tls || false;
    document.getElementById('email_username').value = config.notifications?.email?.username || '';
    document.getElementById('email_password').value = config.notifications?.email?.password || '';
    document.getElementById('from_address').value = config.notifications?.email?.from_address || '';
    document.getElementById('to_addresses').value = (config.notifications?.email?.to_addresses || []).join('\n');
    
    // Email preferences
    document.getElementById('notify_on_update_found').checked = config.notifications?.email?.notify_on_update_found !== false;
    document.getElementById('notify_on_no_updates').checked = config.notifications?.email?.notify_on_no_updates || false;
    document.getElementById('notify_on_success').checked = config.notifications?.email?.notify_on_success !== false;
    document.getElementById('notify_on_error').checked = config.notifications?.email?.notify_on_error !== false;
    
    // Discord
    document.getElementById('discord_enabled').checked = config.notifications?.discord?.enabled || false;
    document.getElementById('discord_webhook').value = config.notifications?.discord?.webhook_url || '';
    
    // Webhook
    document.getElementById('webhook_enabled').checked = config.notifications?.webhook?.enabled || false;
    document.getElementById('webhook_url').value = config.notifications?.webhook?.url || '';
    
    // Other
    document.getElementById('keep_versions').value = config.rollback?.keep_versions || 3;
    document.getElementById('web_port').value = config.web?.port || 5454;
}

// Save configuration
document.getElementById('config-form').addEventListener('submit', async function(e) {
    e.preventDefault();
    
    const formData = {
        cron_schedule: document.getElementById('cron_schedule').value,
        monitoring: {
            monitor_all: document.getElementById('monitor_all').checked,
            exclude_containers: document.getElementById('exclude_containers').value
                .split('\n').filter(x => x.trim())
        },
        notifications: {
            email: {
                enabled: document.getElementById('email_enabled').checked,
                smtp_host: document.getElementById('smtp_host').value,
                smtp_port: parseInt(document.getElementById('smtp_port').value) || 587,
                use_tls: document.getElementById('use_tls').checked,
                username: document.getElementById('email_username').value,
                password: document.getElementById('email_password').value,
                from_address: document.getElementById('from_address').value,
                to_addresses: document.getElementById('to_addresses').value
                    .split('\n').filter(x => x.trim()),
                notify_on_update_found: document.getElementById('notify_on_update_found').checked,
                notify_on_no_updates: document.getElementById('notify_on_no_updates').checked,
                notify_on_success: document.getElementById('notify_on_success').checked,
                notify_on_error: document.getElementById('notify_on_error').checked
            },
            discord: {
                enabled: document.getElementById('discord_enabled').checked,
                webhook_url: document.getElementById('discord_webhook').value
            },
            webhook: {
                enabled: document.getElementById('webhook_enabled').checked,
                url: document.getElementById('webhook_url').value
            }
        },
        rollback: {
            keep_versions: parseInt(document.getElementById('keep_versions').value) || 3
        },
        web: {
            port: parseInt(document.getElementById('web_port').value) || 5454
        }
    };
    
    const response = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formData)
    });
    
    const result = await response.json();
    alert(result.message);
});

// Save notification configuration
document.getElementById('notification-form').addEventListener('submit', async function(e) {
    e.preventDefault();
    
    // Get current config first
    const config = await fetchData('/api/config');
    
    // Get password value - only include if it's not the masked placeholder
    const passwordValue = document.getElementById('email_password').value;
    const password = (passwordValue && passwordValue !== '********') ? passwordValue : '';
    
    // Update only notification settings
    config.notifications = {
        email: {
            enabled: document.getElementById('email_enabled').checked,
            smtp_host: document.getElementById('smtp_host').value,
            smtp_port: parseInt(document.getElementById('smtp_port').value) || 587,
            use_tls: document.getElementById('use_tls').checked,
            username: document.getElementById('email_username').value,
            password: password,
            from_address: document.getElementById('from_address').value,
            to_addresses: document.getElementById('to_addresses').value
                .split('\n').filter(x => x.trim()),
            notify_on_update_found: document.getElementById('notify_on_update_found').checked,
            notify_on_no_updates: document.getElementById('notify_on_no_updates').checked,
            notify_on_success: document.getElementById('notify_on_success').checked,
            notify_on_error: document.getElementById('notify_on_error').checked
        },
        discord: {
            enabled: document.getElementById('discord_enabled').checked,
            webhook_url: document.getElementById('discord_webhook').value
        },
        webhook: {
            enabled: document.getElementById('webhook_enabled').checked,
            url: document.getElementById('webhook_url').value
        }
    };
    
    const response = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config)
    });
    
    const result = await response.json();
    showModal(
        result.success ? 'Settings Saved' : 'Save Failed',
        result.message,
        'OK',
        () => closeModal(),
        false,
        true
    );
});

// Send test email
async function sendTestEmail() {
    const passwordValue = document.getElementById('email_password').value;
    
    const emailData = {
        smtp_host: document.getElementById('smtp_host').value,
        smtp_port: parseInt(document.getElementById('smtp_port').value) || 587,
        use_tls: document.getElementById('use_tls').checked,
        username: document.getElementById('email_username').value,
        password: passwordValue === '********' ? '' : passwordValue,
        from_address: document.getElementById('from_address').value,
        to_addresses: document.getElementById('to_addresses').value
            .split('\n').filter(x => x.trim())
    };
    
    // Validate required fields
    if (!emailData.smtp_host) {
        alert('Please enter SMTP Host');
        return;
    }
    if (!emailData.from_address) {
        alert('Please enter From Address');
        return;
    }
    if (emailData.to_addresses.length === 0) {
        alert('Please enter at least one To Address');
        return;
    }
    
    // Show loading modal
    showModal(
        'Sending Test Email',
        '<div><span class=\"loading-spinner\"></span>Sending test email...</div>',
        null,
        null,
        true
    );
    
    try {
        const response = await fetch('/api/test-email', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(emailData)
        });
        
        const result = await response.json();
        
        // Show result
        showModal(
            result.success ? 'Test Email Sent' : 'Test Email Failed',
            result.message,
            'OK',
            () => closeModal(),
            false,
            true
        );
    } catch (error) {
        showModal(
            'Error',
            `Failed to send test email: ${error}`,
            'OK',
            () => closeModal(),
            false,
            true
        );
    }
}

async function toggleMonitoring(containerName, enabled) {
    // Close the menu
    closeAllMenus();
    
    try {
        const response = await fetch(`/api/toggle-monitoring/${containerName}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: enabled })
        });
        
        const result = await response.json();
        
        if (result.success) {
            // Refresh container list to update UI
            await loadContainers();
        } else {
            alert('Failed to toggle monitoring: ' + result.message);
            // Refresh to revert checkbox state
            await loadContainers();
        }
    } catch (error) {
        alert('Error toggling monitoring: ' + error);
        await loadContainers();
    }
}

function toggleContainerMenu(event, containerName, monitored) {
    event.stopPropagation();
    
    // Close all other menus
    closeAllMenus();
    
    // Toggle this menu
    const menu = document.getElementById('menu-' + containerName);
    menu.classList.toggle('active');
}

function closeAllMenus() {
    document.querySelectorAll('.container-menu').forEach(menu => {
        menu.classList.remove('active');
    });
}

// Close menus when clicking outside
document.addEventListener('click', function(event) {
    if (!event.target.closest('.container-menu') && !event.target.closest('.container-menu-icon')) {
        closeAllMenus();
    }
});

// Initialize on page load
document.addEventListener('DOMContentLoaded', async function() {
    await loadContainers();
    await loadHistory();
    await loadVersion();
});

async function loadVersion() {
    try {
        const data = await fetchData('/api/version');
        const versionEl = document.querySelector('.version');
        if (versionEl && data.version) {
            versionEl.textContent = data.version;
        }
    } catch (error) {
        console.error('Failed to load version:', error);
    }
}

async function logout() {
    try {
        await fetch('/api/logout', { method: 'POST' });
        window.location.href = '/login';
    } catch (error) {
        console.error('Logout failed:', error);
        window.location.href = '/login';
    }
}
