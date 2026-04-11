const provinceSelect1 = document.getElementById('provincia1');
const townSelect1 = document.getElementById('comune1');
const townOptions1 = townSelect1.getElementsByTagName('option');
let lastSelectedProvince1 = '';

// console.log('Initial setup:', {
//     provinceSelect1,
//     townSelect1,
//     townOptionsCount1: townOptions1.length
// });

const provinceSelect2 = document.getElementById('provincia2');
const townSelect2 = document.getElementById('comune2');
const townOptions2 = townSelect2.getElementsByTagName('option');
let lastSelectedProvince2 = '';

// console.log('Initial setup:', {
//     provinceSelect2,
//     townSelect2,
//     townOptionsCount2: townOptions2.length
// });

function filterTowns1() {
    const selectedProvince = provinceSelect1.value;
    // console.log('Selected county:', selectedProvince);

    for (let option of townOptions1) {
        if (option.dataset.province_code === "") continue; // Skip the default "Select a town" option
        if (selectedProvince === '' || option.dataset.province_code === selectedProvince) {
            option.style.display = '';
        } else {
            option.style.display = 'none';
        }
    }

    // Reset town selection
    townSelect1.value = '';
}

function filterTowns2() {
    const selectedProvince = provinceSelect2.value;
    // console.log('Selected county:', selectedProvince);               

    for (let option of townOptions2) {
        if (option.dataset.province_code === "") continue; // Skip the default "Select a town" option
        if (selectedProvince === '' || option.dataset.province_code === selectedProvince) {
            option.style.display = '';          
        } else {
            option.style.display = 'none';
        }
    }

    // Reset town selection
    townSelect2.value = '';
}


// const townOptions1 = Array.from(townSelect1.options).slice(1); // Exclude the first option
// const townOptions2 = Array.from(townSelect2.options).slice(1); // Exclude the first option

// function filterTowns1() {
//     const selectedProvince = provinceSelect1.value;
    
//     if (selectedProvince === lastSelectedProvince1) return; // No change, exit early
    
//     console.time('Filtering towns');
    
//     const fragment = document.createDocumentFragment();
//     let visibleTowns = 0;

//     townOptions1.forEach(option => {
//         const shouldDisplay = selectedProvince === '' || option.dataset.province === selectedProvince;
//         option.classList.toggle('hidden', !shouldDisplay);
//         if (shouldDisplay) visibleTowns++;
//         fragment.appendChild(option);
//     });

//     // Append all options at once
//     townSelect1.appendChild(fragment);

//     // Reset town selection
//     townSelect1.selectedIndex = 0;

//     lastSelectedProvince1 = selectedProvince;
    
//     console.timeEnd('Filtering towns');
//     console.log(`Filtered towns: ${visibleTowns} visible out of ${townOptions1.length} total`);
// }

// function filterTowns2() {
//     const selectedProvince = provinceSelect2.value;
    
//     if (selectedProvince === lastSelectedProvince2) return; // No change, exit early
    
//     console.time('Filtering towns');
    
//     const fragment = document.createDocumentFragment();
//     let visibleTowns = 0;

//     townOptions2.forEach(option => {
//         const shouldDisplay = selectedProvince === '' || option.dataset.province === selectedProvince;
//         option.classList.toggle('hidden', !shouldDisplay);
//         if (shouldDisplay) visibleTowns++;
//         fragment.appendChild(option);
//     });

//     // Append all options at once
//     townSelect2.appendChild(fragment);

//     // Reset town selection
//     townSelect2.selectedIndex = 0;

//     lastSelectedProvince2 = selectedProvince;
    
//     console.timeEnd('Filtering towns');
//     console.log(`Filtered towns: ${visibleTowns} visible out of ${townOptions2.length} total`);
// }

provinceSelect1.addEventListener('change', filterTowns1);
provinceSelect2.addEventListener('change', filterTowns2);

