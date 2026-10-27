/**
 * NyaySetu — Case Upload & Analysis Page
 */
import { navbar, icons, uploadZone, stepProgress, alert, badge, disclaimer, aiBadge, showToast } from '../components.js';
import { getRole, setCurrentCase } from '../state.js';
import * as api from '../api.js';

let analysisState = 'upload'; // 'upload' | 'form' | 'analyzing' | 'done'

export function render() {
    const role = getRole();
    const roleName = localStorage.getItem('br_role_name') || 'User';

    return `
    <div class="layout-app">
        ${navbar({ showLinks: false, showRole: true, roleName })}
        <div class="page-content animate-fade-in">
            <div class="page-header">
                <div>
                    <div class="page-breadcrumb">
                        <span class="breadcrumb-link" data-navigate="/purpose">Home</span>
                        <span class="breadcrumb-sep">›</span>
                        <span>Analyze Your Case</span>
                    </div>
                    <h1 class="page-title">Analyze Your Case</h1>
                    <p class="page-subtitle">Enter case details or upload documents for AI-assisted analysis.</p>
                </div>
            </div>

            ${disclaimer()}

            <!-- Upload Section -->
            <div class="upload-page" style="margin-top: var(--space-6)">
                <div class="upload-container">
                    ${uploadZone('case-file')}
                    <p style="font-size:var(--text-xs); color:var(--text-tertiary); margin-top:var(--space-3)">The selected PDF is sent to this NyaySetu server for text extraction. It is not added to the app’s case document library. Extracted text is included in your case review and kept in this browser tab’s session. Check and edit the text before continuing. Scanned PDFs are not readable yet. Nothing is sent to the Claude AI service until you press <strong>Auto-fill</strong> or <strong>Analyze</strong>; phone, ID, e-mail and labelled name/address lines are masked first.</p>
                    
                    <div class="divider-text" style="margin: var(--space-6) 0">Or enter case details manually</div>
                    
                    <!-- Manual Input Form -->
                    <div class="card" id="case-form-card">
                        <h3 style="margin-bottom: var(--space-5)">Case Details</h3>
                        <div class="form-row">
                            <div class="form-group">
                                <label class="form-label">IPC/BNS Sections</label>
                                <input type="text" class="form-input" id="inp-sections" placeholder="e.g., 302, 34 or 420, 468">
                                <span class="form-hint">Comma-separated section numbers</span>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Special-Law Sections</label>
                                <input type="text" class="form-input" id="inp-special" placeholder="e.g., POCSO 6; NDPS 20">
                                <span class="form-hint">Act name and section</span>
                            </div>
                        </div>
                        <div class="form-row" style="margin-top: var(--space-4)">
                            <div class="form-group">
                                <label class="form-label">Bail Type</label>
                                <select class="form-select" id="inp-bail-type">
                                    <option value="Regular">Regular Bail</option>
                                    <option value="Anticipatory">Anticipatory Bail</option>
                                    <option value="Default/statutory">Default / Statutory Bail</option>
                                    <option value="Interim">Interim Bail</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Arrest Date</label>
                                <input type="date" class="form-input" id="inp-arrest">
                            </div>
                        </div>
                        <div class="form-row" style="margin-top: var(--space-4)">
                            <div class="form-group">
                                <label class="form-checkbox-group">
                                    <input type="checkbox" class="form-checkbox" id="inp-chargesheet"> Charge sheet filed
                                </label>
                            </div>
                            <div class="form-group">
                                <label class="form-checkbox-group">
                                    <input type="checkbox" class="form-checkbox" id="inp-firsttime"> First-time offender (if confirmed)
                                </label>
                            </div>
                        </div>
                        <div style="margin-top: var(--space-4)">
                            <label class="form-label">Risk Factors (if recorded)</label>
                            <div style="display: flex; gap: var(--space-6); margin-top: var(--space-2)">
                                <label class="form-checkbox-group">
                                    <input type="checkbox" class="form-checkbox" id="inp-flight"> Flight risk
                                </label>
                                <label class="form-checkbox-group">
                                    <input type="checkbox" class="form-checkbox" id="inp-witness"> Witness risk
                                </label>
                                <label class="form-checkbox-group">
                                    <input type="checkbox" class="form-checkbox" id="inp-evidence"> Evidence risk
                                </label>
                            </div>
                        </div>
                        <div class="form-group" style="margin-top: var(--space-4)">
                            <label class="form-label">Case Facts and Legal Issues</label>
                            <textarea class="form-textarea" id="inp-facts" rows="6" placeholder="Enter relevant facts, or upload a text-based PDF above to extract its text. Review and remove anything you do not want to submit."></textarea>
                        </div>
                        <div class="form-group" style="margin-top: var(--space-4)">
                            <label class="form-label">Names or places to hide from the AI (optional, comma-separated)</label>
                            <input type="text" class="form-input" id="inp-redact" placeholder="e.g., Vikram Joshi, Neha Joshi, Wanowrie">
                            <p style="font-size:var(--text-xs); color:var(--text-tertiary); margin-top:var(--space-2)">Names inside sentences cannot be found automatically. List them here and they are replaced with [REDACTED] before text leaves this server.</p>
                        </div>
                        <button class="btn btn-outline w-full" style="margin-top: var(--space-4)" id="autofill-btn" type="button">✨ Auto-fill the form from this text (uses Claude AI)</button>
                        <button class="btn btn-primary btn-lg w-full" style="margin-top: var(--space-6)" id="analyze-btn">
                            ${icons.search} Start Analysis
                        </button>
                    </div>
                </div>
            </div>

            <!-- Analysis Progress -->
            <div id="analysis-progress" style="display:none">
                <div class="analysis-progress">
                    <div class="card" style="text-align: center; padding: var(--space-10)">
                        <div class="spinner spinner-lg" style="margin: 0 auto var(--space-6)"></div>
                        <h3 style="margin-bottom: var(--space-2)">Analyzing Your Case</h3>
                        <p style="color: var(--text-secondary); margin-bottom: var(--space-8)">Please wait while we process your case information...</p>
                        <div id="analysis-steps"></div>
                    </div>
                </div>
            </div>

            <!-- Results (shown after analysis) -->
            <div id="analysis-results" style="display:none"></div>
        </div>
    </div>`;
}

const analysisSteps = [
    'Reading the information you submitted',
    'Calculating custody duration when an arrest date is provided',
    'Checking rule-based review triggers',
    'Searching judgments by wording, sections and statute',
    'Writing a cited brief from the retrieved sources',
    'Matching lawyers to the case type and court',
];

// Details the AI read from the document; sent along with the analysis request.
let extracted = { retrieval_query: '', court: '', region: '' };

function redactTerms() {
    return (document.getElementById('inp-redact')?.value || '').split(',').map(t => t.trim()).filter(Boolean);
}

async function autofill() {
    const facts = document.getElementById('inp-facts')?.value || '';
    if (facts.trim().length < 40) {
        showToast('Upload a PDF or paste the chargesheet text first.', 'warning');
        return;
    }
    const btn = document.getElementById('autofill-btn');
    btn.disabled = true;
    btn.textContent = 'Reading the document…';
    try {
        const d = await api.ragExtractFields(facts, redactTerms());
        const set = (id, v) => { const el = document.getElementById(id); if (el && v !== undefined && v !== null) el.value = v; };
        const check = (id, v) => { const el = document.getElementById(id); if (el) el.checked = Boolean(v); };
        set('inp-sections', d.sections || '');
        set('inp-special', d.special_laws || '');
        set('inp-bail-type', d.bail_type || 'Regular');
        set('inp-arrest', d.arrest_date || '');
        check('inp-chargesheet', d.charge_sheet_filed);
        check('inp-firsttime', d.first_time_offender);
        check('inp-flight', d.flight_risk);
        check('inp-witness', d.witness_risk);
        check('inp-evidence', d.evidence_risk);
        extracted = { retrieval_query: d.retrieval_query || '', court: d.court || '', region: d.region || '' };
        const gaps = (d.missing_information || []).length ? ` Not stated in the document: ${d.missing_information.slice(0, 3).join('; ')}.` : '';
        showToast('Form filled from your document. Check every field before analysing.' + gaps, 'success');
    } catch (error) {
        showToast(error.message || 'Auto-fill is unavailable. Please fill the form manually.', 'warning');
    } finally {
        btn.disabled = false;
        btn.textContent = '✨ Auto-fill the form from this text (uses Claude AI)';
    }
}

export function init() {
    // Upload zone interactions
    const zone = document.getElementById('case-file-zone');
    const fileInput = document.getElementById('case-file');
    
    if (zone) {
        zone.addEventListener('click', () => fileInput.click());
        zone.addEventListener('dragover', e => {
            e.preventDefault();
            zone.classList.add('dragover');
        });
        zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
        zone.addEventListener('drop', e => {
            e.preventDefault();
            zone.classList.remove('dragover');
            if (e.dataTransfer.files.length > 0) handleFile(e.dataTransfer.files[0]);
        });
        fileInput.addEventListener('change', () => {
            if (fileInput.files.length > 0) handleFile(fileInput.files[0]);
        });
    }

    // Analyze button
    document.getElementById('analyze-btn')?.addEventListener('click', runAnalysis);
    document.getElementById('autofill-btn')?.addEventListener('click', autofill);
    extracted = { retrieval_query: '', court: '', region: '' };
}

async function handleFile(file) {
    const zone = document.getElementById('case-file-zone');
    if (!file.name.toLowerCase().endsWith('.pdf')) {
        showToast('Please choose a PDF. Other formats are not processed in this prototype.', 'warning');
        return;
    }
    if (file.size > 10 * 1024 * 1024) {
        showToast('PDF is larger than the 10 MB demo limit.', 'warning');
        return;
    }
    zone.querySelector('.upload-icon').textContent = '⏳';
    zone.querySelector('.upload-title').textContent = file.name;
    zone.querySelector('.upload-hint').textContent = 'Extracting selectable text…';
    zone.querySelector('.upload-formats').innerHTML = badge('Reading PDF', 'info');
    try {
        const result = await api.extractCaseText(file);
        const factsField = document.getElementById('inp-facts');
        factsField.value = result.text;
        const detail = result.truncated ? `First 30,000 characters extracted from ${result.pages_read} pages; the text was shortened.` : `Text extracted from ${result.pages_read} page${result.pages_read === 1 ? '' : 's'}.`;
        zone.querySelector('.upload-hint').textContent = `${(file.size / 1024).toFixed(1)} KB · ${detail}`;
        zone.querySelector('.upload-icon').textContent = '✓';
        zone.querySelector('.upload-formats').innerHTML = badge('Text ready', 'success');
        factsField.focus();
        showToast(`${detail} Review it, then press Auto-fill to pre-fill the form.`, result.truncated ? 'warning' : 'success');
    } catch (error) {
        zone.querySelector('.upload-hint').textContent = 'Text could not be extracted. You can still enter facts manually below.';
        zone.querySelector('.upload-icon').textContent = '⚠️';
        zone.querySelector('.upload-formats').innerHTML = badge('Try another PDF', 'warning');
        showToast(error.message, 'warning');
    }
}

async function runAnalysis() {
    const sections = document.getElementById('inp-sections')?.value || '';
    const specialLaws = document.getElementById('inp-special')?.value || '';
    const bailType = document.getElementById('inp-bail-type')?.value || 'Regular';
    const arrestDate = document.getElementById('inp-arrest')?.value || '';
    const chargeSheet = document.getElementById('inp-chargesheet')?.checked || false;
    const firstTime = Boolean(document.getElementById('inp-firsttime')?.checked);
    const flight = document.getElementById('inp-flight')?.checked || false;
    const witness = document.getElementById('inp-witness')?.checked || false;
    const evidence = document.getElementById('inp-evidence')?.checked || false;
    const facts = document.getElementById('inp-facts')?.value || '';

    if (!sections && !facts) {
        showToast('Please enter case sections or facts to analyze.', 'warning');
        return;
    }

    // Show progress
    document.querySelector('.upload-page').style.display = 'none';
    const progress = document.getElementById('analysis-progress');
    progress.style.display = 'block';
    progress.scrollIntoView({ behavior: 'smooth' });

    // Start the request first, then animate the steps while it runs
    const payload = {
        sections,
        special_laws: specialLaws,
        bail_type: bailType,
        arrest_date: arrestDate,
        charge_sheet_filed: chargeSheet,
        first_time_offender: firstTime,
        flight_risk: flight,
        witness_risk: witness,
        evidence_risk: evidence,
        case_facts: facts,
        top_k: 5,
        redact_terms: redactTerms(),
        retrieval_query: extracted.retrieval_query,
        court: extracted.court,
        region: extracted.region,
    };
    let finished = false;
    const request = api.ragAnalyze(payload).catch(async err => {
        // RAG service unavailable: fall back to the original review so the page still works.
        const basic = await api.reviewCase({ ...payload, top_k: 5 });
        return { ...basic, brief: null, brief_status: 'error', brief_message: `The AI brief is unavailable (${err.message}). Showing the standard review.`, lawyers: [] };
    }).finally(() => { finished = true; });

    const stepsContainer = document.getElementById('analysis-steps');
    const draw = (active) => {
        stepsContainer.innerHTML = analysisSteps.map((step, j) => `
            <div class="analysis-step ${j < active ? 'done' : j === active ? 'active' : 'pending'}">
                <div class="step-icon">${j < active ? '✓' : j === active ? '<div class="spinner"></div>' : (j + 1)}</div>
                <div class="step-text">${step}</div>
            </div>
        `).join('');
    };
    for (let i = 0; i < analysisSteps.length - 1 && !finished; i++) {
        draw(i);
        await delay(i < 3 ? 500 : 1800);
    }
    if (!finished) draw(analysisSteps.length - 1);

    try {
        const result = await request;

        setCurrentCase({
            ...result,
            input: { sections, specialLaws, bailType, arrestDate, chargeSheet, firstTime, flight, witness, evidence, facts, court: extracted.court, region: extracted.region },
        });

        draw(analysisSteps.length);
        await delay(400);
        window.location.hash = '#/case-analysis';

    } catch (err) {
        showToast('Analysis failed: ' + err.message + '. Showing with available data.', 'warning');
        // Store partial result
        setCurrentCase({
            custody_days: 0,
            assessment: { status: 'Review required', triggers: ['Unable to complete full analysis. Please verify input data.'], disclaimer: 'Informational only.' },
            similar_cases: [],
            input: { sections, specialLaws, bailType, arrestDate, chargeSheet, firstTime, flight, witness, evidence, facts },
            analysis_error: err.message,
        });
        await delay(500);
        window.location.hash = '#/case-analysis';
    }
}

function delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}
