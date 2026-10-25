// Theme toggle: the choice lives in a cookie so the server renders the right theme on the next page.
const THEME_COLORS = {dark: '#15120f', light: '#f7f3ee'};

document.querySelectorAll('.theme-toggle').forEach(button => button.addEventListener('click', () => {
    const theme = document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-bs-theme', theme);
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', THEME_COLORS[theme]);
    document.cookie = `theme=${theme}; path=/; max-age=31536000; SameSite=Lax`;
    document.querySelectorAll('.theme-toggle').forEach(b => {
        b.dataset.theme = theme;
        b.setAttribute('aria-label', theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
    });
}));

// Claude-backed actions take a while; show it and prevent double submits.
document.querySelectorAll('form.slow').forEach(form => form.addEventListener('submit', event => {
    form.querySelectorAll('button').forEach(b => { b.disabled = true; });
    const btn = event.submitter || form.querySelector('button[type=submit]');
    if (btn) btn.innerHTML = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span> Thinking…';
}));

// Forms that ask before submitting (e.g. regenerate the week).
document.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', event => {
    if (!confirm(form.dataset.confirm)) {
        event.preventDefault();
        event.stopImmediatePropagation();
    }
}, {capture: true}));

// Grocery list: keep the cart button's count in step with the checkboxes.
const cartCount = document.getElementById('cart-count');
if (cartCount) {
    const boxes = document.querySelectorAll('input[name="item"]');
    const update = () => { cartCount.textContent = [...boxes].filter(b => b.checked).length; };
    boxes.forEach(b => b.addEventListener('change', update));
    update();
}

// Recipe list filter.
const recipeFilter = document.getElementById('recipe-filter');
if (recipeFilter) {
    const rows = document.querySelectorAll('[data-recipe-search]');
    const empty = document.getElementById('recipe-filter-empty');
    recipeFilter.addEventListener('input', () => {
        const q = recipeFilter.value.trim().toLowerCase();
        let shown = 0;
        rows.forEach(row => {
            const match = !q || row.dataset.recipeSearch.includes(q);
            row.classList.toggle('hidden', !match);
            shown += match;
        });
        empty?.classList.toggle('hidden', shown > 0);
    });
}

// Copy buttons: data-copy="#selector" copies that element's text.
document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
    const text = document.querySelector(button.dataset.copy)?.textContent.trim();
    try {
        await navigator.clipboard.writeText(text);
        button.textContent = 'Copied';
    } catch {
        window.getSelection().selectAllChildren(document.querySelector(button.dataset.copy));
    }
}));

// Preferences meal schedule: quick picks fill the day grid; counts follow the checkboxes.
const mealSchedule = document.querySelector('[data-meal-schedule]');
if (mealSchedule) {
    const boxes = [...mealSchedule.querySelectorAll('input[data-meal-type]')];
    const presets = JSON.parse(document.getElementById('meal-presets').textContent);
    const update = () => {
        mealSchedule.querySelectorAll('.schedule-row').forEach(row => {
            row.querySelector('[data-meal-count]').textContent = row.querySelectorAll('input:checked').length;
        });
        mealSchedule.querySelector('[data-meal-total]').textContent = boxes.filter(b => b.checked).length;
    };
    boxes.forEach(b => b.addEventListener('change', update));
    mealSchedule.querySelectorAll('[data-preset]').forEach(button => button.addEventListener('click', () => {
        const preset = presets[button.dataset.preset];
        boxes.forEach(b => { b.checked = (preset[b.dataset.mealType] || []).includes(Number(b.value)); });
        update();
    }));
}
