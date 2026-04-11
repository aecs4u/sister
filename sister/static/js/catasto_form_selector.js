        // Form toggle
        const form_prospetto_catastale_button = document.getElementById('form_prospetto_catastale_button');
        const form_elenco_immobili_button = document.getElementById('form_elenco_immobili_button');
        const form_ricerca_nazionale_button = document.getElementById('form_ricerca_nazionale_button');
        const form_ricerca_persona_button = document.getElementById('form_ricerca_persona_button');
        const form_indirizzo_button = document.getElementById('form_indirizzo_button');

        const form_prospetto_catastale = document.getElementById('form_prospetto_catastale');
        const form_elenco_immobili = document.getElementById('form_elenco_immobili');
        const form_ricerca_nazionale = document.getElementById('form_ricerca_nazionale');
        const form_ricerca_persona = document.getElementById('form_ricerca_persona');
        const form_indirizzo = document.getElementById('form_indirizzo');

        function select_form_prospetto_catastale() {
            form_prospetto_catastale.style.display = 'block';
            form_elenco_immobili.style.display = 'none';
            form_ricerca_nazionale.style.display = 'none';
            form_ricerca_persona.style.display = 'none';
            form_indirizzo.style.display = 'none';
        }
        function select_elenco_immobili() {
            form_prospetto_catastale.style.display = 'none';
            form_elenco_immobili.style.display = 'block';
            form_ricerca_nazionale.style.display = 'none';
            form_ricerca_persona.style.display = 'none';
            form_indirizzo.style.display = 'none';
        }
        function select_ricerca_nazionale() {
            form_prospetto_catastale.style.display = 'none';
            form_elenco_immobili.style.display = 'none';
            form_ricerca_nazionale.style.display = 'block';
            form_ricerca_persona.style.display = 'none';
            form_indirizzo.style.display = 'none';
        }
        function select_ricerca_persona() {
            form_prospetto_catastale.style.display = 'none';
            form_elenco_immobili.style.display = 'none';
            form_ricerca_nazionale.style.display = 'none';
            form_ricerca_persona.style.display = 'block';
            form_indirizzo.style.display = 'none';
        }
        function select_indirizzo() {
            form_prospetto_catastale.style.display = 'none';
            form_elenco_immobili.style.display = 'none';
            form_ricerca_nazionale.style.display = 'none';
            form_ricerca_persona.style.display = 'none';
            form_indirizzo.style.display = 'block';
        }

        form_prospetto_catastale_button.addEventListener('change', select_form_prospetto_catastale);
        form_elenco_immobili_button.addEventListener('change', select_elenco_immobili);
        form_ricerca_nazionale_button.addEventListener('change', select_ricerca_nazionale);
        form_ricerca_persona_button.addEventListener('change', select_ricerca_persona);
        form_indirizzo_button.addEventListener('change', select_indirizzo);
