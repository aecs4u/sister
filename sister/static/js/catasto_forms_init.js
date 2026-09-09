/**
 * Catasto Forms Page Initialization
 * This file handles page-specific setup for catasto/forms.html
 */

// Initialize translations from data attribute
(function initializeTranslations() {
    const translationsData = document.getElementById('translations-data');
    if (translationsData) {
        try {
            window.translations = JSON.parse(translationsData.textContent);
        } catch (e) {
            console.error('Failed to parse translations:', e);
            window.translations = {};
        }
    } else {
        window.translations = window.translations || {};
    }
})();

// Initialize the CatastoForms module when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    if (typeof CatastoForms !== 'undefined') {
        CatastoForms.init();
    }
});
