/**
 * NyaySetu — Police / Law Enforcement Dashboard
 * Each sidebar tab (?tab=...) loads real data from the API.
 */
import { navbar, sidebar, icons, statCard, badge, dataTable, alert } from '../components.js';
import { policeSidebarItems } from '../data.js';
import * as api from '../api.js';

const TABS = {
    dashboard:    { title: 'Law Enforcement Dashboard', subtitle: 'Overview of your cases, FIRs, charge sheets and court dates.' },
    cases:        { title: 'Cases', subtitle: 'Cases visible to your station or assigned to you.' },
    firs:         { title: 'FIRs', subtitle: 'FIR numbers linked to your cases.' },
    chargesheets: { title: 'Charge Sheets', subtitle: 'Charge-sheet documents uploaded to your cases.' },
    court:        { title: 'Court Proceedings', subtitle: 'Recorded hearings and their next dates.' },
    actions:      { title: 'Pending Actions', subtitle: 'Upcoming court dates from recorded hearings.' },
    reports:      { title: 'Reports', subtitle: 'Summary reports for your cases.' },
};

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
let requestId = 0;

// ===== Helpers ==============================================================

function hasTab(tab) {
    return Object.prototype.hasOwnProperty.call(TABS, tab);
}

function currentTab() {
    const query = window.location.hash.split('?')[1] || '';
    const tab = new URLSearchParams(query).get('tab') || 'dashboard';
    return hasTab(tab) ? tab : 'dashboard';
}

function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));
}

/** Format an ISO date or datetime string as "27 Oct 2026". */
function day(value) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value ?? ''));
    if (!m) return '—';
    return `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}`;
}

/** Today's date in local time as YYYY-MM-DD (matches the format of next_date). */
function todayIso() {
    const d = new Date();
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function caseLink(caseId) {
    return `<a href="#/case-details/${encodeURIComponent(caseId)}"><strong>${esc(caseId)}</strong></a>`;
}

function groupByFir(cases) {
    const groups = new Map();
    for (const c of cases) {
        if (!c.fir_number) continue;
        if (!groups.has(c.fir_number)) {
            groups.set(c.fir_number, { fir: c.fir_number, cases: [], sections: c.sections || '', station: c.org_name || '' });
        }
        groups.get(c.fir_number).cases.push(c);
    }
    return [...groups.values()];
}

function upcomingHearings(proceedings) {
    const today = todayIso();
    return proceedings
        .filter(p => p.next_date && p.next_date >= today)
        .sort((a, b) => a.next_date.localeCompare(b.next_date));
}

function loadingState() {
    return `<div class="card" style="padding: var(--space-6); text-align: center; color: var(--text-secondary)">Loading…</div>`;
}

function errorState(err) {
    const msg = esc(err?.message || 'Something went wrong');
    return alert(`Could not load this section: ${msg}. If you were logged out, <a href="#/login">log in again</a>.`, 'danger');
}

function section(title, html) {
    return `
    <div class="section">
        <div class="section-header"><h3 class="section-title">${title}</h3></div>
        <div class="card" style="padding: 0; overflow: hidden">${html}</div>
    </div>`;
}

// ===== Shared data fetches ==================================================

async function fetchCases() {
    const { cases } = await api.listCases({ limit: 200 });
    return cases || [];
}

async function fetchProceedings() {
    const { proceedings } = await api.listCourtProceedings({ limit: 200 });
    return proceedings || [];
}

async function fetchChargeSheets() {
    const { documents } = await api.listDocuments({ doc_type: 'chargesheet', limit: 200 });
    return documents || [];
}

// ===== Page shell ===========================================================

export function render() {
    const tab = currentTab();
    const { title, subtitle } = TABS[tab];
    return `
    <div class="layout-dashboard">
        ${navbar({ showLinks: false, showRole: true, roleName: 'Law Enforcement' })}
        ${sidebar(policeSidebarItems, tab)}
        <div class="page-content animate-fade-in">
            <div style="display: flex; align-items: center; gap: var(--space-3); padding: var(--space-3) var(--space-4); background: var(--danger-50); border: 1px solid var(--danger-200); border-radius: var(--radius-md); margin-bottom: var(--space-6); font-size: var(--text-sm); color: var(--danger-700)">
                ${icons.lock} <strong>Confidential / Authorized Access Only</strong> — This dashboard contains sensitive law enforcement information.
            </div>

            <div class="page-header">
                <div>
                    <h1 class="page-title">${title}</h1>
                    <p class="page-subtitle">${subtitle}</p>
                </div>
                <div class="page-actions">
                    <button class="btn btn-primary btn-sm" data-navigate="/case-search">New Case</button>
                </div>
            </div>

            <div id="police-content">${loadingState()}</div>
        </div>
    </div>`;
}

export async function init() {
    const id = ++requestId;
    const el = document.getElementById('police-content');
    if (!el) return;
    const tab = currentTab();
    try {
        await LOADERS[tab](el, id);
    } catch (err) {
        if (id === requestId) el.innerHTML = errorState(err);
    }
}

// ===== Tabs ================================================================

async function loadDashboard(el, id) {
    const [stats, cases, proceedings, chargeSheets] = await Promise.all([
        api.getCaseStats(), fetchCases(), fetchProceedings(), fetchChargeSheets(),
    ]);
    if (id !== requestId) return;

    const upcoming = upcomingHearings(proceedings);
    const firCount = groupByFir(cases).length;

    el.innerHTML = `
        <div class="grid-cols-5" style="margin-bottom: var(--space-6)">
            ${statCard({ icon: '📁', value: String(stats.active_cases ?? 0), label: 'Active Cases', iconBg: 'var(--primary-50)' })}
            ${statCard({ icon: '📋', value: String(firCount), label: 'FIRs', iconBg: 'var(--info-50)' })}
            ${statCard({ icon: '📄', value: String(chargeSheets.length), label: 'Charge Sheets', iconBg: 'var(--warning-50)' })}
            ${statCard({ icon: '🏛️', value: String(upcoming.length), label: 'Upcoming Court Dates', iconBg: 'var(--danger-50)' })}
            ${statCard({ icon: '🗂️', value: String(stats.document_count ?? 0), label: 'Documents', iconBg: 'var(--success-50)' })}
        </div>
        ${section('Recent Cases', dataTable({ columns: caseColumns(), rows: cases.slice(0, 5), emptyText: 'No cases found for your account.' }))}
        <div style="height: var(--space-6)"></div>
        ${section('Upcoming Court Dates', dataTable({ columns: hearingColumns(), rows: upcoming.slice(0, 5), emptyText: 'No upcoming court dates.' }))}`;
}

function caseColumns() {
    return [
        { label: 'Case ID', render: r => caseLink(r.id) },
        { label: 'FIR No.', render: r => esc(r.fir_number || '—') },
        { label: 'Title', render: r => esc(r.title) },
        { label: 'Sections', render: r => esc(r.sections || '—') },
        { label: 'Station', render: r => esc(r.org_name || '—') },
        { label: 'Status', render: r => badge(esc(r.status || 'active'), 'primary') },
    ];
}

async function loadCases(el, id) {
    const cases = await fetchCases();
    if (id !== requestId) return;

    el.innerHTML = `
        <div style="margin-bottom: var(--space-4)">
            <input type="text" id="police-case-search" class="form-input" placeholder="Search by case ID, FIR number, title or sections" style="max-width: 420px">
        </div>
        <div class="card" style="padding: 0; overflow: hidden" id="police-case-table">
            ${dataTable({ columns: caseColumns(), rows: cases, emptyText: 'No cases found for your account.' })}
        </div>`;

    const input = el.querySelector('#police-case-search');
    input.addEventListener('input', () => {
        const q = input.value.trim().toLowerCase();
        const rows = q
            ? cases.filter(c => [c.id, c.fir_number, c.title, c.sections].some(v => String(v || '').toLowerCase().includes(q)))
            : cases;
        el.querySelector('#police-case-table').innerHTML =
            dataTable({ columns: caseColumns(), rows, emptyText: 'No cases match your search.' });
    });
}

async function loadFirs(el, id) {
    const cases = await fetchCases();
    if (id !== requestId) return;
    const firs = groupByFir(cases);

    el.innerHTML = section('FIRs', dataTable({
        columns: [
            { label: 'FIR No.', render: r => `<strong>${esc(r.fir)}</strong>` },
            { label: 'Cases', render: r => r.cases.map(c => caseLink(c.id)).join(', ') },
            { label: 'Title', render: r => esc(r.cases[0].title) + (r.cases.length > 1 ? ` (+${r.cases.length - 1} more)` : '') },
            { label: 'Sections', render: r => esc(r.sections || '—') },
            { label: 'Station', render: r => esc(r.station || '—') },
        ],
        rows: firs,
        emptyText: 'No FIRs recorded for your cases yet. Add an FIR number when creating a case.',
    }));
}

async function loadChargeSheets(el, id) {
    const docs = await fetchChargeSheets();
    if (id !== requestId) return;

    el.innerHTML = section('Charge Sheets', dataTable({
        columns: [
            { label: 'File', render: r => `<strong>${esc(r.original_name)}</strong>` },
            { label: 'Case', render: r => caseLink(r.case_id) },
            { label: 'Uploaded', render: r => day(r.created_at) },
            { label: 'Verification', render: r => badge(esc(r.verification_status || 'pending'), r.verification_status === 'verified' ? 'success' : 'warning') },
        ],
        rows: docs,
        emptyText: 'No charge sheets uploaded yet. Upload one from a case\'s Documents tab using the "Charge Sheet" type.',
    }));
}

function hearingColumns() {
    return [
        { label: 'Next Date', render: r => day(r.next_date) },
        { label: 'Case', render: r => caseLink(r.case_id) },
        { label: 'Hearing Type', render: r => esc(r.hearing_type || '—') },
        { label: 'Court', render: r => esc(r.court_name || '—') },
        { label: 'Last Summary', render: r => esc((r.summary || '').slice(0, 90)) || '—' },
    ];
}

async function loadCourt(el, id) {
    const proceedings = await fetchProceedings();
    if (id !== requestId) return;
    const rows = [...proceedings].sort((a, b) => String(b.hearing_date).localeCompare(String(a.hearing_date)));

    el.innerHTML = section('Court Proceedings', dataTable({
        columns: [
            { label: 'Hearing Date', render: r => day(r.hearing_date) },
            { label: 'Case', render: r => caseLink(r.case_id) },
            { label: 'Type', render: r => esc(r.hearing_type || '—') },
            { label: 'Court', render: r => esc(r.court_name || '—') },
            { label: 'Presiding Officer', render: r => esc(r.presiding_officer || '—') },
            { label: 'Next Date', render: r => day(r.next_date) },
        ],
        rows,
        emptyText: 'No court proceedings recorded yet.',
    }));
}

async function loadActions(el, id) {
    const proceedings = await fetchProceedings();
    if (id !== requestId) return;
    el.innerHTML = section('Upcoming Court Dates', dataTable({
        columns: hearingColumns(),
        rows: upcomingHearings(proceedings),
        emptyText: 'No upcoming court dates.',
    }));
}

async function loadReports(el, id) {
    if (id !== requestId) return;
    el.innerHTML = alert('Reports are not built yet. Use the Cases, FIRs, Charge Sheets and Court Proceedings tabs for now.', 'info');
}

const LOADERS = {
    dashboard: loadDashboard,
    cases: loadCases,
    firs: loadFirs,
    chargesheets: loadChargeSheets,
    court: loadCourt,
    actions: loadActions,
    reports: loadReports,
};
