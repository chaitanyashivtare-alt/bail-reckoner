/**
 * NyaySetu — AI Case Analysis Dashboard
 */
import { navbar, icons, badge, aiBadge, statCard, accordion, disclaimer, alert, caseCard } from '../components.js';
import { getCurrentCase } from '../state.js';

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[char]);
}


// ===== RAG brief rendering ====================================================

function citeChips(ids) {
    return (ids || []).map(id => `<button type="button" class="cite-chip" data-cite="${escapeHtml(id)}" title="Show source ${escapeHtml(id)}">${escapeHtml(id)}</button>`).join('');
}

function unverifiedTag(item) {
    return item.uncited ? ' <span class="badge badge-warning">No source: verify</span>' : '';
}

function renderBrief(brief, outcome) {
    const list = (items, fn) => items && items.length ? `<ul class="brief-list">${items.map(fn).join('')}</ul>` : '<p class="brief-empty">Nothing supported by the retrieved sources.</p>';
    const outcomeLine = outcome && outcome.retrieved
        ? `<p class="brief-note">Of the ${outcome.retrieved} judgments retrieved, bail was granted in ${outcome.granted} and rejected in ${outcome.rejected}. ${escapeHtml(outcome.note || '')}</p>` : '';
    const checks = brief.checks || {};
    const flagged = (checks.uncited_items || 0) + (checks.dropped_prediction || 0);
    return `
    <div class="section">
        <div class="section-header">
            <h3 class="section-title">Research Brief</h3>
            ${aiBadge()}
        </div>
        <div class="card brief-card">
            <p class="brief-summary">${escapeHtml(brief.summary || '')}</p>

            <h4>Applicable provisions</h4>
            ${list(brief.applicable_provisions, p => `<li><strong>${escapeHtml(p.provision)}</strong>${unverifiedTag(p)}<br>${escapeHtml(p.explanation)} ${citeChips(p.citations)}</li>`)}

            <h4>How similar cases were decided, and why</h4>
            ${list(brief.how_similar_cases_ended, p => `<li>${escapeHtml(p.pattern)}${unverifiedTag(p)} ${citeChips(p.citations)}</li>`)}
            ${outcomeLine}

            <h4>Procedural issues to discuss with your lawyer</h4>
            ${list(brief.procedural_options, p => `<li><strong>${escapeHtml(p.option)}</strong>${unverifiedTag(p)}<br><span class="brief-why">${escapeHtml(p.basis)}</span> ${citeChips(p.citations)}</li>`)}

            <h4>Questions to ask your lawyer</h4>
            ${list(brief.questions_for_lawyer, q => `<li><strong>${escapeHtml(q.question)}</strong><br><span class="brief-why">${escapeHtml(q.reason)}</span></li>`)}

            ${(brief.missing_information || []).length ? `<h4>Not stated in your documents</h4>${list(brief.missing_information, m => `<li>${escapeHtml(m)}</li>`)}` : ''}

            <p class="brief-foot">Written by an AI model using only the numbered sources below. ${flagged ? `${flagged} item(s) were removed or marked for missing a valid source. ` : ''}Not legal advice; it does not predict or recommend a bail outcome.</p>
        </div>
    </div>`;
}

function renderSources(brief) {
    const items = Object.values(brief?.sources || {});
    if (!items.length) return '';
    return `
    <div class="section">
        <details id="sources-details" class="card sources-card">
            <summary><strong>Sources used for this brief (${items.length})</strong> <span class="brief-why">Judgment excerpts, statute table rows, custody rules and landmark judgments</span></summary>
            ${items.map(s => `
                <div class="source-item" id="src-${escapeHtml(s.id)}">
                    <span class="cite-chip static">${escapeHtml(s.id)}</span>
                    <div>
                        <div><strong>${escapeHtml(s.title || '')}</strong> ${s.kind === 'judgment' ? badge(escapeHtml(s.outcome || ''), s.outcome === 'Granted' ? 'success' : 'danger') : badge(escapeHtml(s.kind), 'neutral')}</div>
                        ${s.court ? `<div class="brief-why">${escapeHtml(s.court)} • ${escapeHtml(String(s.year || ''))} • passage: ${escapeHtml(s.field || '')}</div>` : ''}
                        <div class="source-text">${escapeHtml(s.excerpt || '')}</div>
                    </div>
                </div>`).join('')}
        </details>
    </div>`;
}

function renderLawyers(lawyers) {
    if (!lawyers || !lawyers.length) return '';
    return `
    <div class="section">
        <div class="section-header">
            <h3 class="section-title">Suggested Lawyers</h3>
            <button class="btn btn-outline btn-sm" data-navigate="/lawyer-discovery">See all matches ${icons.arrow}</button>
        </div>
        <p style="font-size:var(--text-xs); color:var(--text-tertiary); margin-bottom:var(--space-3)">Sample profiles for this prototype. Suggestions are based only on the case type, court and region shown below. They are not rankings of quality and not an endorsement.</p>
        <div style="display:grid; gap:var(--space-3)">
            ${lawyers.slice(0, 3).map(l => `
            <div class="lawyer-card">
                <div class="lawyer-avatar">${escapeHtml(l.initials)}</div>
                <div class="lawyer-info">
                    <div class="lawyer-name">${escapeHtml(l.name)} ${badge(escapeHtml(l.match_strength || ''), l.match_strength === 'Strong fit' ? 'success' : 'primary')}</div>
                    <div class="lawyer-details">${l.experience} years • ${escapeHtml(l.location)} • ${escapeHtml((l.languages || []).join(', '))}</div>
                    <div class="lawyer-match"><strong style="color:var(--text-primary)">Suggested because:</strong>
                        ${(l.matchReasons || []).map(r => `<div class="match-reason">✓ ${escapeHtml(r)}</div>`).join('')}
                    </div>
                    <div style="margin-top:var(--space-3)"><button class="btn btn-primary btn-sm" data-navigate="/lawyer-profile?id=${l.id}">View Profile</button></div>
                </div>
            </div>`).join('')}
        </div>
    </div>`;
}

function briefToText(brief) {
    if (!brief) return [];
    const L = ['', 'RESEARCH BRIEF (AI-written from the cited sources; verify before relying on it)', brief.summary || ''];
    const block = (title, items, fn) => { if (items && items.length) { L.push('', title.toUpperCase()); items.forEach((x, i) => L.push(`${i + 1}. ${fn(x)}`)); } };
    const c = x => (x.citations && x.citations.length ? ` [${x.citations.join(', ')}]` : x.uncited ? ' [no source: verify]' : '');
    block('Applicable provisions', brief.applicable_provisions, p => `${p.provision}: ${p.explanation}${c(p)}`);
    block('How similar cases were decided', brief.how_similar_cases_ended, p => `${p.pattern}${c(p)}`);
    block('Procedural issues to discuss with your lawyer', brief.procedural_options, p => `${p.option} - ${p.basis}${c(p)}`);
    block('Questions to ask your lawyer', brief.questions_for_lawyer, q => `${q.question} (${q.reason})`);
    const src = Object.values(brief.sources || {});
    if (src.length) { L.push('', 'SOURCES'); src.forEach(s => L.push(`[${s.id}] ${s.title}${s.court ? ' | ' + s.court : ''}${s.year ? ' | ' + s.year : ''}: ${s.excerpt}`)); }
    return L;
}

export function render() {
    const roleName = localStorage.getItem('br_role_name') || 'User';
    const caseData = getCurrentCase();
    const input = caseData?.input || {};
    const assessment = caseData?.assessment || {};
    const custodyDays = caseData?.custody_days || 0;
    const similarCases = caseData?.similar_cases || [];
    const analysisError = caseData?.analysis_error || '';
    const brief = caseData?.brief || null;
    const briefStatus = caseData?.brief_status || '';
    const briefMessage = caseData?.brief_message || '';
    const lawyers = caseData?.lawyers || [];
    const submittedFacts = input.facts?.trim() || '';
    const sectionCount = input.sections ? input.sections.split(/[,;]+/).map(value => value.trim()).filter(Boolean).length : 0;

    return `
    <div class="layout-app">
        ${navbar({ showLinks: false, showRole: true, roleName })}
        <div class="page-content animate-fade-in">
            <div class="page-header">
                <div>
                    <div class="page-breadcrumb">
                        <span class="breadcrumb-link" data-navigate="/purpose">Home</span>
                        <span class="breadcrumb-sep">›</span>
                        <span class="breadcrumb-link" data-navigate="/case-upload">Case Upload</span>
                        <span class="breadcrumb-sep">›</span>
                        <span>Case Analysis</span>
                    </div>
                    <h1 class="page-title">Case Analysis ${aiBadge()}</h1>
                </div>
                <div class="page-actions">
                    <button class="btn btn-outline btn-sm" data-navigate="/bail-reckoner">${icons.scales} View Bail Assessment</button>
                    <button class="btn btn-secondary btn-sm" id="download-case-brief">${icons.download} Download Case Brief</button>
                    <button class="btn btn-primary btn-sm" data-navigate="/lawyer-discovery">${icons.lawyer} Find a Lawyer</button>
                </div>
            </div>

            ${disclaimer()}

            <!-- Case Header -->
            <div class="case-header" style="margin-top: var(--space-4)">
                <div>
                    <div class="case-id">NyaySetu case review</div>
                    <div class="case-court">Court details were not provided</div>
                </div>
                <div style="display:flex; gap: var(--space-2); align-items: center; flex-wrap: wrap">
                    ${badge('Based on submitted details', 'primary')}
                    ${badge(analysisError ? 'Review service unavailable' : 'Review complete', analysisError ? 'warning' : 'success')}
                    ${aiBadge()}
                </div>
            </div>

            ${analysisError ? alert(`Automated review could not be completed: ${escapeHtml(analysisError)}. The information below is what you submitted; no similar judgments or automated review result is available.`, 'warning', 'Review service unavailable') : ''}

            ${!brief && briefMessage ? alert(escapeHtml(briefMessage), briefStatus === 'llm_unavailable' ? 'info' : 'warning', 'Written brief not available') : ''}

            <!-- Summary Stats -->
            <div class="grid-cols-5" style="margin-bottom: var(--space-6)">
                ${statCard({ icon: '📋', value: escapeHtml(input.bailType || 'Not entered'), label: 'Bail type entered', iconBg: 'var(--primary-50)' })}
                ${statCard({ icon: '🕐', value: input.arrestDate ? `${custodyDays} days` : 'Not calculated', label: 'Custody duration', iconBg: 'var(--warning-50)' })}
                ${statCard({ icon: '⚖️', value: sectionCount.toString(), label: 'Section entries supplied', iconBg: 'var(--danger-50)' })}
                ${statCard({ icon: '📚', value: similarCases.length.toString(), label: 'Similar judgments found', iconBg: 'var(--info-50)' })}
                ${statCard({ icon: '✅', value: analysisError ? 'Unavailable' : 'Ready for review', label: 'Review status', iconBg: 'var(--success-50)' })}
            </div>

            ${brief ? renderBrief(brief, caseData?.outcome_summary) : ''}

            <!-- Submitted information and discussion prompts -->
            <div class="simple-language">
                <div class="sl-title">
                    ${icons.bulb} Information to review with your lawyer
                </div>
                ${accordion([
                    { title: `${icons.doc} Facts entered or extracted from your document`, content: `<p style="white-space:pre-wrap; overflow-wrap:anywhere">${escapeHtml(submittedFacts || 'No case facts were provided.')}</p>` },
                    { title: `${icons.scales} Sections and special laws you entered`, content: `<p>Sections: ${escapeHtml(input.sections || 'Not provided')}</p><p>Special-law sections: ${escapeHtml(input.specialLaws || 'Not provided')}</p><p>These entries have not been independently verified against the current law.</p>` },
                    { title: `${icons.lawyer} Questions to discuss with a lawyer`, content: brief && (brief.questions_for_lawyer || []).length ? `<ul>${brief.questions_for_lawyer.map(q => `<li><strong>${escapeHtml(q.question)}</strong><br>${escapeHtml(q.reason)}</li>`).join('')}</ul>` : '<ul><li>Which current statutory provisions apply to the facts and dates in the official record?</li><li>Are there special-statute requirements or previous court orders to consider?</li><li>Which documents and upcoming court dates should be confirmed?</li></ul>' },
                ])}
            </div>

            <!-- Case information supplied by the user -->
            <div class="section" style="margin-top: var(--space-6)">
                <div class="section-header">
                    <h3 class="section-title">Details supplied for review</h3>
                </div>
                <div class="card">
                    <div class="ac-row"><span class="ac-label">Bail type</span><span class="ac-value">${escapeHtml(input.bailType || 'Not provided')}</span></div>
                    <div class="ac-row"><span class="ac-label">Arrest date</span><span class="ac-value">${escapeHtml(input.arrestDate || 'Not provided')}</span></div>
                    <div class="ac-row"><span class="ac-label">Charge sheet filed</span><span class="ac-value">${input.chargeSheet ? 'Confirmed in submitted details' : 'Not confirmed'}</span></div>
                    <div class="ac-row"><span class="ac-label">First-time offender</span><span class="ac-value">${input.firstTime ? 'Reported in submitted details' : 'Not reported'}</span></div>
                    <p style="font-size:var(--text-xs); color:var(--text-tertiary); margin-top:var(--space-4)">These details are user-entered or extracted text. NyaySetu has not verified them against official court records.</p>
                </div>
            </div>

            <!-- Rule-Based Review Triggers -->
            <div class="section">
                <div class="section-header">
                    <h3 class="section-title">Rule-Based Review Triggers</h3>
                    ${aiBadge()}
                </div>
                <div class="card">
                    ${assessment.triggers && assessment.triggers.length > 0 ? 
                        assessment.triggers.map(t => `
                            <div style="display: flex; align-items: flex-start; gap: var(--space-3); padding: var(--space-3) 0; border-bottom: 1px solid var(--neutral-100)">
                                <span style="color: var(--warning-500); font-size: 18px; flex-shrink: 0">⚠</span>
                                <span style="font-size: var(--text-sm)">${escapeHtml(t)}</span>
                            </div>
                        `).join('') :
                        alert('No automated triggers were recorded. This is not a bail decision. All information requires legal verification.', 'info')
                    }
                    <div style="margin-top: var(--space-4); font-size: var(--text-xs); color: var(--text-tertiary)">
                        ${escapeHtml(assessment.disclaimer || 'Informational retrieval and issue spotting only. Does not predict, approve, or reject bail.')}
                    </div>
                </div>
            </div>

            <!-- Similar Judgments -->
            <div class="section">
                <div class="section-header">
                    <h3 class="section-title">Similar Judgments Found</h3>
                    <button class="btn btn-outline btn-sm" data-navigate="/case-law-search">View All ${icons.arrow}</button>
                </div>
                <p style="font-size:var(--text-xs); color:var(--text-tertiary); margin-bottom:var(--space-3)">Similarity is a search aid, not a measure of legal relevance or a prediction. Open and verify each judgment from an authoritative source.</p>
                ${similarCases.length > 0 ? `
                    <div style="display: grid; gap: var(--space-4)">
                        ${similarCases.map(c => `<div>${caseCard({
                            title: escapeHtml(c.case_title || c.case_name || 'Case'),
                            court: escapeHtml(c.court || ''),
                            date: escapeHtml(c.date || ''),
                            bailType: escapeHtml(c.bail_type || ''),
                            outcome: escapeHtml(c.bail_outcome || ''),
                            sections: escapeHtml(c.ipc_sections || ''),
                            similarity: c.similarity,
                        })}${(c.match_reasons || []).length ? `<div class="match-why">Why it was retrieved: ${c.match_reasons.map(escapeHtml).join(' • ')}</div>` : ''}</div>`).join('')}
                    </div>
                ` : `
                    <div class="card" style="text-align: center; padding: var(--space-8)">
                        <p style="color: var(--text-secondary)">No similar judgments found in the database. Try refining your case description or search separately.</p>
                        <button class="btn btn-outline" style="margin-top: var(--space-4)" data-navigate="/case-law-search">Search Case Law</button>
                    </div>
                `}
            </div>

            ${renderSources(brief)}

            ${renderLawyers(lawyers)}

            <!-- Action Buttons -->
            <div class="card" style="display: flex; gap: var(--space-4); flex-wrap: wrap; justify-content: center; padding: var(--space-8)">
                <button class="btn btn-primary btn-lg" data-navigate="/bail-reckoner">${icons.scales} View Bail Assessment</button>
                <button class="btn btn-secondary btn-lg" data-navigate="/lawyer-discovery">${icons.lawyer} Find a Lawyer</button>
                <button class="btn btn-secondary btn-lg" data-navigate="/legal-provisions">${icons.doc} View Legal Provisions</button>
                <button class="btn btn-outline btn-lg" data-navigate="/case-timeline">${icons.clock} Case Timeline</button>
            </div>
        </div>
    </div>`;
}

export function init() {
    // Open first accordion by default
    const firstAcc = document.querySelector('.accordion-item');
    if (firstAcc) firstAcc.classList.add('open');

    // Citation chips jump to the matching source
    document.querySelectorAll('.cite-chip[data-cite]').forEach(chip => chip.addEventListener('click', () => {
        const details = document.getElementById('sources-details');
        if (details) details.open = true;
        const target = document.getElementById(`src-${chip.dataset.cite}`);
        if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'center' });
            target.classList.add('flash');
            setTimeout(() => target.classList.remove('flash'), 1800);
        }
    }));

    document.getElementById('download-case-brief')?.addEventListener('click', () => {
        const current = getCurrentCase() || {};
        const caseInput = current.input || {};
        const triggers = current.assessment?.triggers || [];
        const judgments = current.similar_cases || [];
        const lines = [
            'NYAYSETU — CASE REVIEW BRIEF',
            `Generated: ${new Date().toLocaleString()}`,
            '',
            'CASE INFORMATION',
            `Sections: ${caseInput.sections || 'Not provided'}`,
            `Special laws: ${caseInput.specialLaws || 'Not provided'}`,
            `Bail type: ${caseInput.bailType || 'Not provided'}`,
            `Arrest date: ${caseInput.arrestDate || 'Not provided'}`,
            `Charge sheet filed: ${caseInput.chargeSheet ? 'Yes' : 'Not confirmed'}`,
            `Custody duration recorded by system: ${caseInput.arrestDate ? `${current.custody_days ?? 0} days` : 'Not calculated'}`,
            '',
            'CASE FACTS PROVIDED',
            caseInput.facts || 'No case facts provided.',
            '',
            'REVIEW TRIGGERS',
            ...(triggers.length ? triggers.map((trigger, index) => `${index + 1}. ${trigger}`) : ['No triggers recorded.']),
            '',
            'SIMILAR JUDGMENTS FOR HUMAN REVIEW',
            ...(judgments.length ? judgments.map((judgment, index) => `${index + 1}. ${judgment.case_title || judgment.case_name || 'Untitled case'} | ${judgment.court || 'Court not listed'} | ${judgment.date || 'Date not listed'} | similarity ${judgment.similarity ?? 'not scored'}`) : ['No similar judgments were returned.']),
            '',
            ...briefToText(current.brief),
            ...((current.lawyers || []).length ? ['', 'SUGGESTED LAWYERS (sample profiles)', ...current.lawyers.slice(0, 3).map((l, i) => `${i + 1}. ${l.name}, ${l.location} - ${(l.matchReasons || []).join('; ')}`)] : []),
            '',
            'IMPORTANT: NyaySetu provides legal information and research support only. This brief is not legal advice, a bail prediction, or a court decision. Check every fact, provision, and source against current authoritative records with a qualified lawyer.'
        ];
        const blob = new Blob([lines.join('\n')], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `nyaysetu-case-brief-${new Date().toISOString().slice(0, 10)}.txt`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
}
