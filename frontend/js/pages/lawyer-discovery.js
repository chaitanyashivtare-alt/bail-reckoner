/**
 * NyaySetu — Lawyer Discovery / Matching Page
 * Suggestions come from the backend matcher (/api/lawyers/match). If a case has been analysed,
 * the filters start from that case's type, court and region.
 */
import { navbar, icons, badge } from '../components.js';
import { getRole, getCurrentCase } from '../state.js';
import * as api from '../api.js';

function esc(v) {
    return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

// Case-category choices map to the matcher's acts / categories.
const CATEGORY_OPTIONS = {
    'Any criminal matter': { acts: [], categories: [] },
    'NDPS': { acts: ['NDPS'], categories: ['Narcotics'] },
    'PMLA': { acts: ['PMLA'], categories: [] },
    'POCSO / sexual offences': { acts: ['POCSO'], categories: ['Sexual Offense'] },
    'Cyber crime': { acts: ['IT_ACT'], categories: ['Cyber Crime'] },
    'Economic offences': { acts: [], categories: ['Fraud or Cheating'] },
    'Corruption': { acts: ['PC_ACT'], categories: [] },
    'Murder / serious offences': { acts: [], categories: ['Murder'] },
    'Dowry / matrimonial': { acts: ['DOWRY'], categories: ['Dowry Harassment'] },
};
const STATES = ['Any', 'Delhi', 'Maharashtra', 'Karnataka', 'Uttar Pradesh', 'Haryana', 'Punjab', 'Tamil Nadu', 'Bihar'];
const LANGS = ['Any', 'English', 'Hindi', 'Marathi', 'Kannada', 'Tamil', 'Punjabi', 'Urdu'];

export function render() {
    const role = getRole();
    const roleName = localStorage.getItem('br_role_name') || 'User';
    return `
    <div class="layout-app">
        ${navbar({ showLinks: false, showRole: true, roleName })}
        <div class="page-content animate-fade-in">
            <div class="page-header">
                <div>
                    <h1 class="page-title">Find a Lawyer for Your Case</h1>
                    <p class="page-subtitle">Discover lawyers matched to your case based on jurisdiction, expertise, and experience.</p>
                </div>
            </div>

            <div class="alert alert-info" style="margin-bottom: var(--space-6)">
                <span class="alert-icon">${icons.info}</span>
                <div class="alert-content">
                    Lawyer profiles are shown based on publicly available professional information. This does not guarantee outcomes or constitute an endorsement.
                </div>
            </div>

            <!-- Matching Criteria -->
            <div class="card" style="margin-bottom: var(--space-6)">
                <h3 style="margin-bottom: var(--space-4)">Case-Based Matching Criteria</h3>
                <div class="form-row">
                    <div class="form-group">
                        <label class="form-label">Court (optional)</label>
                        <input class="form-input" id="lf-court" placeholder="e.g., Bombay High Court">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Jurisdiction (state)</label>
                        <select class="form-select" id="lf-region">${STATES.map(x => `<option>${x}</option>`).join('')}</select>
                    </div>
                    <div class="form-group">
                        <label class="form-label">Case Category</label>
                        <select class="form-select" id="lf-category">${Object.keys(CATEGORY_OPTIONS).map(x => `<option>${x}</option>`).join('')}</select>
                    </div>
                </div>
                <div class="form-row" style="margin-top: var(--space-4)">
                    <div class="form-group">
                        <label class="form-label">Language</label>
                        <select class="form-select" id="lf-lang">${LANGS.map(x => `<option>${x}</option>`).join('')}</select>
                    </div>
                    <div class="form-group">
                        <label class="form-label">Experience</label>
                        <select class="form-select" id="lf-exp"><option value="0">Any</option><option value="5">5+ years</option><option value="10">10+ years</option><option value="15">15+ years</option></select>
                    </div>
                    <div class="form-group">
                        <label class="form-label">Availability</label>
                        <select class="form-select" id="lf-avail"><option value="">Any</option><option value="Available">Available Now</option><option value="This week">This Week</option></select>
                    </div>
                </div>
                <button class="btn btn-primary" id="lf-find" style="margin-top: var(--space-4)">${icons.search} Find Lawyers</button>
                <p id="lf-context" style="margin-top: var(--space-3); font-size: var(--text-xs); color: var(--text-tertiary)"></p>
            </div>

            <!-- Results -->
            <div id="lf-count" style="margin-bottom: var(--space-4); font-size: var(--text-sm); color: var(--text-secondary)">Loading…</div>
            <div id="lf-results" style="display: grid; gap: var(--space-4)"></div>
        </div>
    </div>`;
}

function lawyerCardFull(l) {
    return `
    <div class="lawyer-card">
        <div class="lawyer-avatar">${esc(l.initials)}</div>
        <div class="lawyer-info">
            <div class="lawyer-name">
                ${esc(l.name)} ${l.match_strength ? badge(esc(l.match_strength), l.match_strength === 'Strong fit' ? 'success' : 'primary') : ''}
                ${l.verified ? `<span style="color: var(--success-500)" title="Verified Professional">✅</span>` : ''}
            </div>
            <div class="lawyer-details">
                ${l.experience} years experience • ${l.location} • ${l.cases} cases handled
            </div>
            <div class="lawyer-tags">
                ${l.practiceAreas.map(a => badge(esc(a), 'primary')).join('')}
                ${l.languages.map(lang => badge(esc(lang), 'neutral')).join('')}
            </div>
            <div style="margin-top: var(--space-3); font-size: var(--text-sm)">
                <strong>Courts:</strong> ${esc(l.courts.join(', '))}
            </div>
            <div class="lawyer-match">
                <strong style="color: var(--text-primary)">Suggested because:</strong>
                ${(l.matchReasons || []).map(r => `<div class="match-reason">✓ ${esc(r)}</div>`).join('')}
            </div>
            <div style="display: flex; gap: var(--space-3); margin-top: var(--space-4)">
                <button class="btn btn-primary btn-sm" data-navigate="/lawyer-profile?id=${l.id}">View Profile</button>
                <button class="btn btn-outline btn-sm">Request Consultation</button>
            </div>
        </div>
        <div style="text-align: right">
            ${badge(l.availability, l.availability === 'Available' ? 'success' : 'warning')}
        </div>
    </div>`;
}

export function init() {
    const ctx = getCurrentCase() || {};
    const fromCase = Boolean(ctx.acts || ctx.categories || ctx.input);
    // Start from the analysed case's category and region when there is one.
    if (fromCase) {
        const acts = ctx.acts || [], cats = ctx.categories || [];
        const match = Object.entries(CATEGORY_OPTIONS).find(([, v]) => v.acts.some(a => acts.includes(a)) || v.categories.some(c => cats.includes(c)));
        if (match) document.getElementById('lf-category').value = match[0];
        const region = ctx.input?.region;
        if (region && STATES.includes(region)) document.getElementById('lf-region').value = region;
        if (ctx.input?.court) document.getElementById('lf-court').value = ctx.input.court;
        document.getElementById('lf-context').textContent = 'Filters were pre-set from your analysed case. Change them to explore other matches.';
    }

    const run = async () => {
        const cat = CATEGORY_OPTIONS[document.getElementById('lf-category').value] || { acts: [], categories: [] };
        const region = document.getElementById('lf-region').value;
        const lang = document.getElementById('lf-lang').value;
        const count = document.getElementById('lf-count');
        const box = document.getElementById('lf-results');
        count.textContent = 'Matching…';
        try {
            const res = await api.matchLawyers({
                acts: cat.acts, categories: cat.categories,
                court: document.getElementById('lf-court').value.trim(),
                region: region === 'Any' ? '' : region,
                languages: lang === 'Any' ? [] : [lang],
                min_experience: Number(document.getElementById('lf-exp').value) || 0,
                availability: document.getElementById('lf-avail').value,
                top_k: 12,
            });
            try { sessionStorage.setItem('ny_lawyers', JSON.stringify(res.lawyers)); } catch (e) { /* ignore */ }
            count.innerHTML = `Showing <strong>${res.lawyers.length}</strong> suggested lawyers <span style="color:var(--text-tertiary)">(sample profiles, ordered by fit to the criteria, not by quality)</span>`;
            box.innerHTML = res.lawyers.length ? res.lawyers.map(lawyerCardFull).join('') : '<div class="card" style="text-align:center">No lawyers match these filters. Try widening them.</div>';
            box.querySelectorAll('[data-navigate]').forEach(el => el.addEventListener('click', () => { window.location.hash = '#' + el.dataset.navigate; }));
        } catch (e) {
            count.textContent = 'Could not load lawyer suggestions: ' + e.message;
        }
    };
    document.getElementById('lf-find')?.addEventListener('click', run);
    run();
}
