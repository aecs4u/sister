// Catasto Forms JavaScript Module

// Initialize translations object
window.translations = window.translations || {};

const CatastoForms = {
    history: [],
    toastContainer: null,

    // Translation helper function
    t(key, fallback = key) {
        return window.translations[key] || fallback;
    },

    // Toast notification system
    initToasts() {
        // Create toast container if it doesn't exist
        if (!this.toastContainer) {
            this.toastContainer = document.createElement('div');
            this.toastContainer.id = 'catasto-toast-container';
            this.toastContainer.className = 'toast-container position-fixed bottom-0 end-0 p-3';
            this.toastContainer.style.zIndex = '1100';
            document.body.appendChild(this.toastContainer);
        }
    },

    showToast(message, type = 'info', duration = 5000) {
        this.initToasts();

        const toastId = 'toast-' + Date.now();
        const iconMap = {
            'success': 'fa-check-circle text-success',
            'error': 'fa-exclamation-circle text-danger',
            'warning': 'fa-exclamation-triangle text-warning',
            'info': 'fa-info-circle text-primary',
            'loading': 'fa-spinner fa-spin text-primary'
        };

        const bgMap = {
            'success': 'bg-success-subtle border-success',
            'error': 'bg-danger-subtle border-danger',
            'warning': 'bg-warning-subtle border-warning',
            'info': 'bg-info-subtle border-info',
            'loading': 'bg-light border-primary'
        };

        const toastHTML = `
            <div id="${toastId}" class="toast ${bgMap[type]} border" role="alert" aria-live="assertive" aria-atomic="true" data-bs-autohide="${type !== 'loading'}">
                <div class="toast-header ${bgMap[type]}">
                    <i class="fas ${iconMap[type]} me-2"></i>
                    <strong class="me-auto">${this.t(type.charAt(0).toUpperCase() + type.slice(1))}</strong>
                    <small class="text-muted">${this.t('now')}</small>
                    <button type="button" class="btn-close" data-bs-dismiss="toast" aria-label="Close"></button>
                </div>
                <div class="toast-body">
                    ${message}
                </div>
            </div>
        `;

        this.toastContainer.insertAdjacentHTML('beforeend', toastHTML);
        const toastElement = document.getElementById(toastId);
        const toast = new bootstrap.Toast(toastElement, {
            autohide: type !== 'loading',
            delay: duration
        });
        toast.show();

        // Remove from DOM after hidden
        toastElement.addEventListener('hidden.bs.toast', () => {
            toastElement.remove();
        });

        return toastId;
    },

    hideToast(toastId) {
        const toastElement = document.getElementById(toastId);
        if (toastElement) {
            const toast = bootstrap.Toast.getInstance(toastElement);
            if (toast) {
                toast.hide();
            }
        }
    },

    init() {
        this.loadHistory();
        this.setupFormHandlers();
        this.setupHistoryRefresh();
    },

    loadHistory() {
        const stored = localStorage.getItem('catasto_query_history');
        if (stored) {
            try {
                this.history = JSON.parse(stored);
                this.renderHistory();
            } catch (e) {
                console.error('Failed to load history:', e);
                this.history = [];
            }
        }
    },

    saveHistory() {
        // Keep only last 50 items
        if (this.history.length > 50) {
            this.history = this.history.slice(0, 50);
        }
        localStorage.setItem('catasto_query_history', JSON.stringify(this.history));
    },

    addToHistory(entry) {
        this.history.unshift({
            ...entry,
            timestamp: new Date().toISOString()
        });
        this.saveHistory();
        this.renderHistory();
    },

    renderHistory() {
        const historyContainer = document.getElementById('history-list');
        if (!historyContainer) return;

        if (this.history.length === 0) {
            historyContainer.innerHTML = `
                <div class="text-center py-5">
                    <i class="fas fa-history fa-3x text-muted mb-3"></i>
                    <p class="text-muted">${this.t('No queries yet')}</p>
                </div>
            `;
            return;
        }

        historyContainer.innerHTML = this.history.map(item => `
            <div class="history-item" data-form-group="${item.formGroup}">
                <div class="d-flex justify-content-between align-items-start mb-2">
                    <div class="history-item-endpoint">
                        <i class="fas fa-landmark me-2"></i>${item.endpoint}
                    </div>
                    <div class="history-item-timestamp">
                        <i class="fas fa-clock me-1"></i>${new Date(item.timestamp).toLocaleString()}
                    </div>
                </div>
                <div class="history-item-params">
                    ${Object.entries(item.params).map(([key, value]) =>
                        `<span class="badge bg-secondary me-1">${key}: ${value}</span>`
                    ).join('')}
                </div>
            </div>
        `).join('');

        // Add click handlers to history items
        document.querySelectorAll('.history-item').forEach((item, index) => {
            item.addEventListener('click', () => {
                this.loadHistoryItem(index);
            });
        });
    },

    loadHistoryItem(index) {
        const item = this.history[index];
        if (!item) return;

        // Switch to the correct tab
        const tabButton = document.querySelector(`[data-bs-target="#form-group-${item.formGroup}"]`);
        if (tabButton) {
            const tab = new bootstrap.Tab(tabButton);
            tab.show();

            // Wait for tab to show, then populate form
            setTimeout(() => {
                const form = document.getElementById(`form-${item.formGroup}-submit`);
                if (form) {
                    // Populate form fields
                    Object.entries(item.params).forEach(([key, value]) => {
                        const input = form.querySelector(`[name="${key}"]`);
                        if (input) {
                            input.value = value;
                        }
                    });

                    // Select the endpoint radio button
                    const endpointRadio = form.querySelector(`[value="${item.endpoint}"]`);
                    if (endpointRadio) {
                        endpointRadio.checked = true;
                    }
                }
            }, 100);
        }
    },

    setupHistoryRefresh() {
        const refreshBtn = document.getElementById('refresh-history');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', () => {
                this.renderHistory();
            });
        }
    },

    setupFormHandlers() {
        // Handle form submissions
        document.querySelectorAll('.catasto-form').forEach(form => {
            form.addEventListener('submit', (e) => {
                e.preventDefault();
                this.handleFormSubmit(form);
            });
        });

        // Handle "Use Example" buttons
        document.querySelectorAll('.btn-fill-example').forEach(btn => {
            btn.addEventListener('click', () => {
                const formGroup = btn.dataset.formGroup;
                this.fillExample(formGroup);
            });
        });

        // Handle copy buttons
        document.querySelectorAll('.btn-copy-response').forEach(btn => {
            btn.addEventListener('click', () => {
                const formGroup = btn.dataset.formGroup;
                this.copyResponse(formGroup);
            });
        });

        // Handle download buttons
        document.querySelectorAll('.btn-download-response').forEach(btn => {
            btn.addEventListener('click', () => {
                const formGroup = btn.dataset.formGroup;
                this.downloadResponse(formGroup);
            });
        });
    },

    async handleFormSubmit(form) {
        const formGroup = form.dataset.formGroup;
        const formData = new FormData(form);
        const submitBtn = form.querySelector('[type="submit"]');
        const originalBtnHTML = submitBtn ? submitBtn.innerHTML : '';

        // Get selected endpoint
        const endpointInput = form.querySelector(`[name="endpoint-${formGroup}"]:checked`) ||
                              form.querySelector(`[name="endpoint-${formGroup}"]`);
        const endpoint = endpointInput ? endpointInput.value : null;

        if (!endpoint) {
            this.showToast(this.t('Please select an endpoint'), 'error');
            this.showError(formGroup, this.t('Error'), this.t('Please select an endpoint'));
            return;
        }

        // Collect parameters
        const params = {};
        for (const [key, value] of formData.entries()) {
            if (!key.startsWith('endpoint-') && value) {
                params[key] = value;
            }
        }

        // Get sandbox mode from toggle (default to sandbox=true for safety)
        const sandboxToggle = document.getElementById('sandboxModeToggle');
        params.sandbox = sandboxToggle ? sandboxToggle.checked : true;

        // Log the mode being used
        const modeText = params.sandbox ? 'Sandbox' : 'Production';
        console.log(`Submitting ${endpoint} in ${modeText} mode with params:`, params);

        // Show loading state
        this.showLoading(formGroup);
        form.classList.add('loading');
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>' + this.t('Submitting') + '...';
        }

        // Show loading toast
        const loadingToastId = this.showToast(
            this.t('Submitting request to') + ' <strong>' + endpoint + '</strong>' +
            ' <span class="badge ' + (params.sandbox ? 'bg-warning' : 'bg-success') + ' ms-1">' +
            modeText + '</span>...',
            'loading'
        );

        try {
            // Make API request
            const response = await fetch(`/catasto/api/${endpoint}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(params)
            });

            const data = await response.json();

            // Hide loading toast
            this.hideToast(loadingToastId);

            // Show response
            this.showResponse(formGroup, data, data.success);

            // Show result toast
            if (data.success) {
                let successMsg = this.t('Request completed successfully');
                if (data.request_id) {
                    successMsg += `<br><small>${this.t('Request ID')}: ${data.request_id}</small>`;
                }
                if (data.status === 'pending') {
                    this.showToast(
                        this.t('Request submitted but still processing. Check back later.'),
                        'warning',
                        8000
                    );
                } else {
                    this.showToast(successMsg, 'success', 6000);
                }

                // Add to history
                this.addToHistory({
                    formGroup,
                    endpoint,
                    params,
                    success: true,
                    requestId: data.request_id
                });
            } else {
                const errorMsg = data.error || data.message || this.t('Unknown error');
                this.showToast(errorMsg, 'error', 8000);
            }
        } catch (error) {
            console.error('Request failed:', error);

            // Hide loading toast
            this.hideToast(loadingToastId);

            this.showError(formGroup, this.t('Error'), error.message);
            this.showToast(this.t('Request failed') + ': ' + error.message, 'error', 8000);
        } finally {
            form.classList.remove('loading');
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.innerHTML = originalBtnHTML;
            }
        }
    },

    showLoading(formGroup) {
        const responseArea = document.getElementById(`response-${formGroup}`);
        const statusDiv = document.getElementById(`response-status-${formGroup}`);
        const contentDiv = document.getElementById(`response-content-${formGroup}`);

        if (responseArea) responseArea.style.display = 'block';
        if (statusDiv) {
            statusDiv.innerHTML = `
                <div class="alert alert-info">
                    <i class="fas fa-spinner fa-spin me-2"></i>${this.t('Loading')}...
                </div>
            `;
        }
        if (contentDiv) contentDiv.textContent = '';
    },

    showResponse(formGroup, data, success) {
        const statusDiv = document.getElementById(`response-status-${formGroup}`);
        const contentDiv = document.getElementById(`response-content-${formGroup}`);

        if (statusDiv) {
            if (success && data.data) {
                // Build a rich summary card for successful responses
                statusDiv.innerHTML = this.buildResultSummary(data, formGroup);
            } else {
                // Show error state
                const errorMsg = data.error || data.message || this.t('Unknown error');
                statusDiv.innerHTML = `
                    <div class="alert alert-danger">
                        <div class="d-flex align-items-start">
                            <i class="fas fa-exclamation-circle fa-2x me-3 mt-1"></i>
                            <div>
                                <h5 class="alert-heading mb-1">${this.t('Request Failed')}</h5>
                                <p class="mb-2">${errorMsg}</p>
                                ${data.request_id ? `<small class="text-muted">${this.t('Request ID')}: ${data.request_id}</small>` : ''}
                            </div>
                        </div>
                    </div>
                    <div class="mt-3">
                        <h6><i class="fas fa-lightbulb me-2 text-warning"></i>${this.t('Suggestions')}:</h6>
                        <ul class="mb-0">
                            <li>${this.t('Check that all required fields are filled correctly')}</li>
                            <li>${this.t('Verify the tax code format (16 characters)')}</li>
                            <li>${this.t('Try using sandbox mode for testing')}</li>
                        </ul>
                    </div>
                `;
            }
        }

        if (contentDiv) {
            contentDiv.textContent = JSON.stringify(data, null, 2);
            // Store the response data for later use
            contentDiv.dataset.responseData = JSON.stringify(data);
        }
    },

    buildResultSummary(data, formGroup) {
        const responseData = data.data || {};
        const status = data.status || responseData.stato || 'unknown';
        const requestId = data.request_id || responseData.id || '';
        const endpoint = responseData.endpoint || '';
        const params = responseData.parametri || {};
        const result = responseData.risultato || {};

        // Status badge
        const statusBadge = this.getStatusBadge(status);

        // Build summary based on endpoint type
        let summaryContent = '';
        let actionButtons = '';

        if (endpoint.includes('ricerca_nazionale') || endpoint.includes('ricerca_persona')) {
            // Person/National search results
            const subjects = result.soggetti || [];
            const totalSubjects = subjects.length;
            let totalProperties = 0;
            let totalLand = 0;

            subjects.forEach(s => {
                (s.catasti || []).forEach(c => {
                    totalProperties += c.fabbricati || 0;
                    totalLand += c.terreni || 0;
                });
            });

            summaryContent = `
                <div class="row g-3 mt-2">
                    <div class="col-md-4">
                        <div class="card bg-primary bg-opacity-10 border-primary h-100">
                            <div class="card-body text-center py-3">
                                <i class="fas fa-user fa-2x text-primary mb-2"></i>
                                <h3 class="mb-0">${totalSubjects}</h3>
                                <small class="text-muted">${this.t('Subject(s) Found')}</small>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-4">
                        <div class="card bg-success bg-opacity-10 border-success h-100">
                            <div class="card-body text-center py-3">
                                <i class="fas fa-building fa-2x text-success mb-2"></i>
                                <h3 class="mb-0">${totalProperties}</h3>
                                <small class="text-muted">${this.t('Buildings')}</small>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-4">
                        <div class="card bg-warning bg-opacity-10 border-warning h-100">
                            <div class="card-body text-center py-3">
                                <i class="fas fa-mountain fa-2x text-warning mb-2"></i>
                                <h3 class="mb-0">${totalLand}</h3>
                                <small class="text-muted">${this.t('Land Plots')}</small>
                            </div>
                        </div>
                    </div>
                </div>
                ${this.buildSubjectsList(subjects)}
            `;

            if (totalSubjects > 0) {
                actionButtons = `
                    <a href="/catasto/national_legal_entities/${requestId}" class="btn btn-primary btn-sm">
                        <i class="fas fa-eye me-1"></i>${this.t('View Full Details')}
                    </a>
                `;
            }
        } else if (endpoint.includes('prospetto') || endpoint.includes('elenco_immobili')) {
            // Property results
            const properties = result.immobili || [];
            summaryContent = `
                <div class="row g-3 mt-2">
                    <div class="col-md-6">
                        <div class="card bg-info bg-opacity-10 border-info h-100">
                            <div class="card-body text-center py-3">
                                <i class="fas fa-home fa-2x text-info mb-2"></i>
                                <h3 class="mb-0">${properties.length}</h3>
                                <small class="text-muted">${this.t('Properties Found')}</small>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-6">
                        <div class="card bg-secondary bg-opacity-10 border-secondary h-100">
                            <div class="card-body text-center py-3">
                                <i class="fas fa-map-marker-alt fa-2x text-secondary mb-2"></i>
                                <p class="mb-0 fw-bold">${params.comune || ''}</p>
                                <small class="text-muted">${params.provincia || ''}</small>
                            </div>
                        </div>
                    </div>
                </div>
            `;
        } else {
            // Generic result
            summaryContent = `
                <div class="alert alert-info mt-2">
                    <i class="fas fa-info-circle me-2"></i>
                    ${this.t('Query completed successfully. See the raw response below for details.')}
                </div>
            `;
        }

        return `
            <div class="card border-success">
                <div class="card-header bg-success bg-opacity-10 d-flex justify-content-between align-items-center">
                    <div>
                        <h5 class="mb-0">
                            <i class="fas fa-check-circle text-success me-2"></i>
                            ${this.t('Request Completed Successfully')}
                        </h5>
                    </div>
                    ${statusBadge}
                </div>
                <div class="card-body">
                    <div class="row mb-3">
                        <div class="col-md-6">
                            <small class="text-muted">${this.t('Request ID')}:</small>
                            <div class="d-flex align-items-center">
                                <code class="me-2">${requestId}</code>
                                <button class="btn btn-sm btn-outline-secondary" onclick="navigator.clipboard.writeText('${requestId}'); this.innerHTML='<i class=\\'fas fa-check\\'></i>'; setTimeout(() => this.innerHTML='<i class=\\'fas fa-copy\\'></i>', 1500);">
                                    <i class="fas fa-copy"></i>
                                </button>
                            </div>
                        </div>
                        <div class="col-md-6">
                            <small class="text-muted">${this.t('Endpoint')}:</small>
                            <div><code>${endpoint}</code></div>
                        </div>
                    </div>
                    ${this.buildParamsDisplay(params)}
                    ${summaryContent}
                    <div class="mt-3 d-flex gap-2 flex-wrap">
                        ${actionButtons}
                        <button class="btn btn-outline-secondary btn-sm" onclick="document.getElementById('response-content-${formGroup}').scrollIntoView({behavior: 'smooth'})">
                            <i class="fas fa-code me-1"></i>${this.t('View Raw JSON')}
                        </button>
                    </div>
                </div>
            </div>
        `;
    },

    getStatusBadge(status) {
        const statusConfig = {
            'evasa': { class: 'bg-success', icon: 'fa-check', text: 'Completed' },
            'pending': { class: 'bg-warning', icon: 'fa-clock', text: 'Pending' },
            'in_corso': { class: 'bg-info', icon: 'fa-spinner fa-spin', text: 'Processing' },
            'errore': { class: 'bg-danger', icon: 'fa-times', text: 'Error' },
            'failed': { class: 'bg-danger', icon: 'fa-times', text: 'Failed' }
        };
        const config = statusConfig[status] || { class: 'bg-secondary', icon: 'fa-question', text: status };
        return `<span class="badge ${config.class}"><i class="fas ${config.icon} me-1"></i>${this.t(config.text)}</span>`;
    },

    buildParamsDisplay(params) {
        if (!params || Object.keys(params).length === 0) return '';

        const paramLabels = {
            'cf_piva': 'Tax Code',
            'tipo_catasto': 'Cadastre Type',
            'provincia': 'Province',
            'comune': 'Municipality',
            'foglio': 'Sheet',
            'particella': 'Parcel',
            'subalterno': 'Sub-unit'
        };

        const badges = Object.entries(params)
            .filter(([k, v]) => v && v !== 'NAZIONALE-IT')
            .map(([key, value]) => {
                const label = paramLabels[key] || key;
                return `<span class="badge bg-light text-dark border me-1 mb-1"><strong>${this.t(label)}:</strong> ${value}</span>`;
            }).join('');

        return `<div class="mb-3">${badges}</div>`;
    },

    buildSubjectsList(subjects) {
        if (!subjects || subjects.length === 0) return '';

        const subjectCards = subjects.slice(0, 3).map(s => {
            const provinces = (s.catasti || []).map(c => c.provincia).join(', ');
            return `
                <div class="col-md-4">
                    <div class="card h-100">
                        <div class="card-body py-2">
                            <h6 class="card-title mb-1">
                                <i class="fas fa-user-circle text-primary me-1"></i>
                                ${s.nome || ''} ${s.cognome || ''}
                            </h6>
                            <small class="text-muted d-block">${s.cf || ''}</small>
                            <small class="text-muted d-block">
                                <i class="fas fa-birthday-cake me-1"></i>${s.data_nascita || ''}
                            </small>
                            ${provinces ? `<small class="text-muted d-block"><i class="fas fa-map-marker-alt me-1"></i>${provinces}</small>` : ''}
                        </div>
                    </div>
                </div>
            `;
        }).join('');

        const moreCount = subjects.length - 3;
        const moreIndicator = moreCount > 0 ? `
            <div class="col-12 text-center mt-2">
                <small class="text-muted">+ ${moreCount} ${this.t('more subject(s)')}</small>
            </div>
        ` : '';

        return `
            <h6 class="mt-3 mb-2"><i class="fas fa-users me-2"></i>${this.t('Subjects Found')}:</h6>
            <div class="row g-2">
                ${subjectCards}
                ${moreIndicator}
            </div>
        `;
    },

    showError(formGroup, title, message) {
        const responseArea = document.getElementById(`response-${formGroup}`);
        const statusDiv = document.getElementById(`response-status-${formGroup}`);
        const contentDiv = document.getElementById(`response-content-${formGroup}`);

        if (responseArea) responseArea.style.display = 'block';
        if (statusDiv) {
            statusDiv.innerHTML = `
                <div class="alert alert-danger">
                    <i class="fas fa-exclamation-circle me-2"></i>${title}
                </div>
            `;
        }
        if (contentDiv) {
            contentDiv.textContent = message;
        }
    },

    fillExample(formGroup) {
        const form = document.getElementById(`form-${formGroup}-submit`);
        if (!form) return;

        // The form already has example values in the value attributes
        // Just trigger a visual feedback
        const inputs = form.querySelectorAll('input[value], select option[selected]');
        inputs.forEach(input => {
            if (input.tagName === 'OPTION') {
                input.parentElement.dispatchEvent(new Event('change'));
            } else {
                input.dispatchEvent(new Event('input'));
            }
        });

        // Show a brief success message
        const submitBtn = form.querySelector('[type="submit"]');
        if (submitBtn) {
            const originalText = submitBtn.innerHTML;
            submitBtn.innerHTML = '<i class="fas fa-check me-2"></i>Example Loaded';
            setTimeout(() => {
                submitBtn.innerHTML = originalText;
            }, 1500);
        }
    },

    async copyResponse(formGroup) {
        const contentDiv = document.getElementById(`response-content-${formGroup}`);
        if (!contentDiv) return;

        const responseData = contentDiv.dataset.responseData || contentDiv.textContent;

        try {
            await navigator.clipboard.writeText(responseData);
            const btn = document.querySelector(`[data-form-group="${formGroup}"].btn-copy-response`);
            if (btn) {
                const originalHTML = btn.innerHTML;
                btn.innerHTML = '<i class="fas fa-check me-1"></i>' + this.t('Copied');
                setTimeout(() => {
                    btn.innerHTML = originalHTML;
                }, 2000);
            }
        } catch (error) {
            console.error('Failed to copy:', error);
            alert(this.t('Failed to copy to clipboard'));
        }
    },

    downloadResponse(formGroup) {
        const contentDiv = document.getElementById(`response-content-${formGroup}`);
        if (!contentDiv) return;

        const responseData = contentDiv.dataset.responseData || contentDiv.textContent;

        try {
            const blob = new Blob([responseData], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `catasto-${formGroup}-${new Date().toISOString().slice(0, 10)}.json`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch (error) {
            console.error('Failed to download:', error);
            alert(this.t('Failed to download file'));
        }
    }
};

// Export for use in initialization script
window.CatastoForms = CatastoForms;
