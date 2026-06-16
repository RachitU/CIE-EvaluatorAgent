/* ═══════════════════════════════════════════════════════════════
   OpportunityAI Chat UI — Frontend Logic
   ═══════════════════════════════════════════════════════════════
   Handles:
   • Session lifecycle (start / reset)
   • SSE event stream from Flask
   • Rendering agent messages with rich formatting (TIPSC/PSEA tables,
     verdict chips, question boxes, feedback blocks)
   • User input → POST /api/message
   • Phase tracker updates
   ═══════════════════════════════════════════════════════════════ */

'use strict';

// ── DOM refs ─────────────────────────────────────────────────
const startBtn         = document.getElementById('startBtn');
const resetBtn         = document.getElementById('resetBtn');
const sidebarToggle    = document.getElementById('sidebarToggle');
const sidebar          = document.getElementById('sidebar');
const welcomeHero      = document.getElementById('welcomeHero');
const messagesFeed     = document.getElementById('messagesFeed');
const messagesScroll   = document.getElementById('messagesScroll');
const typingBar        = document.getElementById('typingBar');
const typingText       = document.getElementById('typingText');
const inputDock        = document.getElementById('inputDock');
const inputBox         = document.getElementById('inputBox');
const msgInput         = document.getElementById('msgInput');
const sendBtn          = document.getElementById('sendBtn');
const inputHint        = document.getElementById('inputHint');
const topbarPhase      = document.getElementById('topbarPhase');

// ── State ─────────────────────────────────────────────────────
let sessionId     = null;
let eventSource   = null;
let awaitingInput = false;
let currentPhase  = '';
let hadError      = false;   // set when an 'error' event arrives, suppresses SSE onerror banner
let sessionComplete = false; // set when 'complete' event arrives

const PHASE_LABELS = {
  intro:              'Getting Started',
  problem_definition: 'Problem Definition',
  ethics:             'Ethics Pre-Screen',
  market_scan:        'Market Intelligence Scan',
  tips:               'TIPS Evaluation',
  report:             'Final Report',
};
const PHASE_ORDER = ['intro','problem_definition','ethics','market_scan','tips','report'];

const PHASE_ICONS = {
  intro:              '📋',
  problem_definition: '💡',
  ethics:             '🛡️',
  market_scan:        '🔭',
  tips:               '📊',
  report:             '🎯',
};

// ═══════════════════════════════════════════════════════════════
// Session management
// ═══════════════════════════════════════════════════════════════

async function startSession() {
  if (sessionId) return;
  sessionComplete = false;
  startBtn.disabled = true;
  startBtn.textContent = 'Initialising…';
  hadError = false;

  try {
    const res  = await fetch('/api/start', { method: 'POST' });
    const data = await res.json();
    sessionId  = data.session_id;
  } catch (err) {
    appendSystemMsg('Failed to connect to server. Is Flask running?', 'error');
    startBtn.disabled = false;
    startBtn.innerHTML = `<svg viewBox="0 0 20 20" fill="currentColor"><path d="M10 18a8 8 0 100-16 8 8 0 000 16zM9.555 7.168A1 1 0 008 8v4a1 1 0 001.555.832l3-2a1 1 0 000-1.664l-3-2z"/></svg> Start Validation Session`;
    return;
  }

  // Hide welcome hero with fade
  welcomeHero.style.transition = 'opacity 0.4s, transform 0.4s';
  welcomeHero.style.opacity = '0';
  welcomeHero.style.transform = 'scale(0.97)';
  setTimeout(() => welcomeHero.remove(), 400);

  resetBtn.disabled = false;
  setTyping(true, 'Initialising agents…');
  openStream();
}

async function resetSession() {
  if (eventSource) { eventSource.close(); eventSource = null; }
  hadError = false;

  try {
    const res  = await fetch('/api/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    });
    const data = await res.json();
    sessionId = data.session_id;
  } catch {
    appendSystemMsg('Reset failed — please refresh the page.', 'error');
    return;
  }

  // Clear chat
  messagesFeed.innerHTML = '';
  resetPhaseTracker();
  currentPhase  = '';
  awaitingInput = false;
  disableInput('Restarting…');
  setTyping(true, 'Initialising agents…');
  openStream();
}

// ═══════════════════════════════════════════════════════════════
// SSE Stream
// ═══════════════════════════════════════════════════════════════

function openStream() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/stream/${sessionId}`);

  eventSource.onmessage = (ev) => {
    try {
      handleEvent(JSON.parse(ev.data));
    } catch { /* ignore parse errors */ }
  };

    eventSource.onerror = () => {
      setTyping(false);
      // If we already received a proper error event or the session completed, don't pile on with a vague message.
      if (!hadError && !awaitingInput && !sessionComplete) {
        appendSystemMsg('Connection to server lost. The session may have ended — click \'New Validation\' to restart.', 'warning');
      }
      eventSource.close();
    };
}

function handleEvent(ev) {
  switch (ev.type) {

    case 'heartbeat':
      break;   // keep-alive, ignore

    case 'phase':
      setTyping(false);
      updatePhaseTracker(ev.phase);
      appendPhaseBar(ev.phase, ev.label || PHASE_LABELS[ev.phase] || ev.phase);
      topbarPhase.textContent = ev.label || PHASE_LABELS[ev.phase] || '';
      break;

    case 'system':
      setTyping(false);
      appendSystemMsg(ev.content, ev.style || 'info');
      break;

    case 'agent':
      setTyping(false);
      appendAgentMsg(ev.content, ev.agent || 'Agent');
      break;

    case 'input_needed':
      setTyping(false);
      enableInput(ev.prompt || 'Your response:');
      break;

    case 'ethics':
      setTyping(false);
      appendEthicsCard(ev.content);
      break;

    case 'complete':
      setTyping(false);
      sessionComplete = true;
      appendFinalReport(ev.summary);
      disableInput('Validation complete!');
      markPhase('report', 'done');
      break;

    case 'exit':
      setTyping(false);
      disableInput('Session ended.');
      break;

    case 'error':
      hadError = true;
      setTyping(false);
      appendErrorMsg(ev.content || 'An unknown error occurred.');
      disableInput('An error occurred. Click \'New Validation\' to restart.');
      break;
  }
}

// ═══════════════════════════════════════════════════════════════
// Input
// ═══════════════════════════════════════════════════════════════

function enableInput(prompt) {
  awaitingInput = true;
  msgInput.disabled = false;
  sendBtn.disabled  = false;
  inputHint.textContent = prompt;
  msgInput.placeholder  = 'Type your response…';
  msgInput.focus();
}

function disableInput(hint = 'Waiting for agent…') {
  awaitingInput = false;
  msgInput.disabled = true;
  sendBtn.disabled  = true;
  msgInput.placeholder = 'Start a session to begin…';
  inputHint.textContent = hint;
}

async function sendMessage() {
  const text = msgInput.value.trim();
  if (!text || !sessionId || !awaitingInput) return;

  disableInput();
  appendUserMsg(text);
  setTyping(true, 'Agent is thinking…');

  msgInput.value = '';
  autoResize();

  try {
    await fetch(`/api/message/${sessionId}`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ message: text }),
    });
  } catch {
    setTyping(false);
    appendSystemMsg('Message failed to send. Please try again.', 'error');
    enableInput('Your response:');
  }
}

// ── Keyboard & resize ─────────────────────────────────────────
msgInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
msgInput.addEventListener('input', autoResize);
sendBtn.addEventListener('click', sendMessage);

function autoResize() {
  msgInput.style.height = 'auto';
  msgInput.style.height = Math.min(msgInput.scrollHeight, 140) + 'px';
}

// ═══════════════════════════════════════════════════════════════
// Typing indicator
// ═══════════════════════════════════════════════════════════════

function setTyping(visible, label = 'Agent is thinking…') {
  typingBar.style.display = visible ? 'flex' : 'none';
  typingText.textContent  = label;
  if (visible) scrollToBottom();
}

// ═══════════════════════════════════════════════════════════════
// Phase tracker
// ═══════════════════════════════════════════════════════════════

function updatePhaseTracker(phase) {
  const idx = PHASE_ORDER.indexOf(phase);
  PHASE_ORDER.forEach((p, i) => {
    const el = document.getElementById(`ph-${p}`);
    if (!el) return;
    el.classList.remove('active', 'done');
    if (i < idx)       el.classList.add('done');
    else if (i === idx) el.classList.add('active');
  });
  currentPhase = phase;
}

function markPhase(phase, state) {
  const el = document.getElementById(`ph-${phase}`);
  if (!el) return;
  el.classList.remove('active', 'done');
  el.classList.add(state);
}

function resetPhaseTracker() {
  PHASE_ORDER.forEach(p => {
    const el = document.getElementById(`ph-${p}`);
    if (el) el.classList.remove('active', 'done');
  });
}

// ═══════════════════════════════════════════════════════════════
// Message rendering
// ═══════════════════════════════════════════════════════════════

function scrollToBottom(smooth = true) {
  messagesScroll.scrollTo({
    top:      messagesScroll.scrollHeight,
    behavior: smooth ? 'smooth' : 'instant',
  });
}

/* ── Phase banner ────────────────────────────────────────────── */
function appendPhaseBar(phase, label) {
  const el = document.createElement('div');
  el.className = 'msg-phase-banner';
  el.innerHTML = `
    <span class="phase-banner-icon">${PHASE_ICONS[phase] || '📌'}</span>
    <span class="phase-banner-label">${escHtml(label)}</span>
  `;
  messagesFeed.appendChild(el);
  scrollToBottom();
}

/* ── Error message card ─────────────────────────────────────── */
function appendErrorMsg(content) {
  const el = document.createElement('div');
  el.style.cssText = [
    'margin:12px 0',
    'padding:16px 18px',
    'background:rgba(239,68,68,0.1)',
    'border:1px solid rgba(239,68,68,0.35)',
    'border-left:4px solid #ef4444',
    'border-radius:4px 12px 12px 12px',
    'animation:slideIn 0.4s var(--ease)',
  ].join(';');
  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <span style="font-size:16px">⚠️</span>
      <span style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#fca5a5">Session Error</span>
    </div>
    <div style="font-size:13.5px;color:#fecaca;line-height:1.6">${escHtml(content)}</div>
    <div style="margin-top:10px;font-size:12px;color:var(--text-3)">
      Click <strong style="color:var(--text-2)">'New Validation'</strong> in the sidebar to start a fresh session.
    </div>
  `;
  messagesFeed.appendChild(el);
  scrollToBottom();
}

/* ── System message ──────────────────────────────────────────── */
function appendSystemMsg(content, style = 'info') {
  const el = document.createElement('div');
  el.className = `msg-system ${style}`;
  el.innerHTML = `<div class="msg-system-inner">${escHtml(content)}</div>`;
  messagesFeed.appendChild(el);
  scrollToBottom();
}

/* ── User message ────────────────────────────────────────────── */
function appendUserMsg(text) {
  const ts  = now();
  const el  = document.createElement('div');
  el.className = 'msg-user';
  el.innerHTML = `
    <div class="msg-user-avatar">👤</div>
    <div class="msg-user-content">
      <div class="msg-user-bubble">${escHtml(text)}</div>
      <span style="font-size:11px;color:var(--text-3);margin-top:4px;">${ts}</span>
    </div>
  `;
  messagesFeed.appendChild(el);
  scrollToBottom();
}

/* ── Agent message ───────────────────────────────────────────── */
function appendAgentMsg(content, agentName = 'Agent') {
  const ts  = now();
  const el  = document.createElement('div');
  el.className = 'msg-agent';
  el.innerHTML = `
    <div class="msg-agent-avatar">⚡</div>
    <div class="msg-agent-content">
      <div class="msg-agent-header">
        <span class="msg-agent-name">${escHtml(agentName)}</span>
        <span class="msg-agent-time">${ts}</span>
      </div>
      <div class="msg-agent-bubble">${renderAgentContent(content)}</div>
    </div>
  `;
  messagesFeed.appendChild(el);
  scrollToBottom();
}

/* ── Final report card ───────────────────────────────────────── */
function appendFinalReport(summary) {
  const el = document.createElement('div');
  el.className = 'report-card';

  const tips   = summary.tips_output   || {};
  const probDef= summary.problem_definition || {};
  const scores = tips.tips_scores || {};
  const metrics= tips.tips_validated_metrics || {};
  const verdict= tips.overall_verdict || '';
  const coaching = tips.coaching_notes || '';

  // ── TIPS scorecard rows
  const dims = [
    { key: 'T', label: 'T — Timely',      metric: metrics.timely_factor },
    { key: 'I', label: 'I — Important',   metric: metrics.importance_metric },
    { key: 'P', label: 'P — Profitable',  metric: metrics.profitability_pivot },
    { key: 'S', label: 'S — Solvable',    metric: metrics.solvability_constraint },
  ];

  const scoreRows = dims.map(d => {
    const sc  = (scores[d.key] || 'UNKNOWN').toUpperCase();
    const cls = sc === 'GREEN' ? 'chip-green' : sc === 'YELLOW' ? 'chip-yellow' : 'chip-red';
    const emoji = sc === 'GREEN' ? '🟢' : sc === 'YELLOW' ? '🟡' : '🔴';
    const reasonRaw = d.metric || '';
    // Strip leading score prefix (e.g. "GREEN — ") from the metric string if present
    const reason = reasonRaw.replace(/^(GREEN|YELLOW|RED)\s*[—\-\u2013]\s*/i, '');
    return `<div class="tips-row">
      <span class="tips-dim-label">${escHtml(d.label)}</span>
      <span class="chip-tl ${cls}">${emoji} ${sc}</span>
      <span class="tips-dim-reason">${escHtml(reason)}</span>
    </div>`;
  }).join('');

  // ── Verdict badge
  const verdictClass = verdict === 'PROCEED_TO_DFV' ? 'verdict-proceed'
                     : verdict === 'NOT_VIABLE'     ? 'verdict-noviable'
                     : 'verdict-refine';
  const verdictLabel = verdict === 'PROCEED_TO_DFV' ? '✅ PROCEED TO DFV'
                     : verdict === 'NOT_VIABLE'     ? '❌ NOT VIABLE'
                     : '⚠️ REFINE REQUIRED';

  // ── Problem Definition fields
  const pdRows = [
    { k: 'Customer',    v: probDef.customer_segment },
    { k: 'Problem',     v: probDef.qualified_problem },
    { k: 'Consequence', v: probDef.consequence },
    { k: 'Solution',    v: probDef.proposed_solution },
  ].filter(r => r.v)
   .map(r => `<div class="prob-def-row">
      <span class="prob-def-key">${escHtml(r.k)}</span>
      <span class="prob-def-val">${escHtml(r.v)}</span>
    </div>`).join('');

  const assumptionsList = (probDef.assumptions || []).length
    ? `<div class="prob-def-row">
         <span class="prob-def-key">Assumptions</span>
         <ul class="bubble-list" style="margin:0;padding-left:18px">${
           probDef.assumptions.map(a => `<li style="font-size:12.5px;color:var(--text-2)">${escHtml(a)}</li>`).join('')
         }</ul>
       </div>`
    : '';

  // ── Full JSON for copy
  const fullJson = JSON.stringify({ problem_definition: probDef, ...tips }, null, 2);

  el.innerHTML = `
    <div class="report-card-title">
      🎯 TIPS Validation Complete
    </div>

    <div class="tips-scorecard">
      <div class="tips-scorecard-title">🗺 TIPS Scorecard</div>
      ${scoreRows}
      <div class="tips-verdict-row">
        <span class="tips-verdict-label">Verdict</span>
        <span class="${verdictClass}">${verdictLabel}</span>
      </div>
      ${coaching ? `<div class="tips-coaching"><strong>Coaching note:</strong> ${escHtml(coaching)}</div>` : ''}
    </div>

    ${pdRows || assumptionsList ? `
    <div class="prob-def-card">
      <div class="prob-def-title">📋 Refined Problem Definition</div>
      ${pdRows}
      ${assumptionsList}
    </div>` : ''}

    ${summary.market_verdict ? `
    <div class="report-section">
      <div class="report-section-label">Market Intelligence</div>
      <div class="report-section-val">
        Scout verdict: ${verdictChip(summary.market_verdict)}
        ${summary.market_angle ? `<br><em>${escHtml(summary.market_angle)}</em>` : ''}
      </div>
    </div>` : ''}

    <div class="json-block-wrap">
      <div class="json-block-header">
        <span class="json-block-label">📋 Full JSON → DFV</span>
        <button class="btn-copy-json" id="copyJsonBtn">Copy JSON</button>
      </div>
      <pre class="json-block-pre" id="tipsJsonPre">${escHtml(fullJson)}</pre>
    </div>

    <div class="report-dfv-badge">
      ${verdictLabel === '✅ PROCEED TO DFV' ? '✅ Ready for DFV Evaluation' : '🚧 Review coaching notes before DFV'}
    </div>
  `;

  messagesFeed.appendChild(el);
  scrollToBottom();

  // Wire copy button
  const copyBtn = document.getElementById('copyJsonBtn');
  if (copyBtn) {
    copyBtn.addEventListener('click', () => {
      navigator.clipboard.writeText(fullJson).then(() => {
        copyBtn.textContent = 'Copied!';
        setTimeout(() => { copyBtn.textContent = 'Copy JSON'; }, 2000);
      });
    });
  }
}


// ═══════════════════════════════════════════════════════════════
// Agent content renderer
// Parses special structured text and turns it into rich HTML.
// ═══════════════════════════════════════════════════════════════

function renderAgentContent(raw) {
  if (!raw) return '';

  const lines = raw.split('\n');
  let html    = '';
  let i       = 0;

  while (i < lines.length) {
    const line = lines[i].trim();

    // ── TIPSC TRIAGE: ────────────────────────────────────────
    if (/^TIPSC TRIAGE:/i.test(line)) {
      html += `<div class="bubble-heading">TIPSC Triage</div>`;
      html += `<table class="eval-table"><thead><tr><th>Criterion</th><th>Assessment</th></tr></thead><tbody>`;
      i++;
      while (i < lines.length) {
        const tl = lines[i].trim();
        // Match "T — Timely: Strong — explanation" or similar
        const m = tl.match(/^([TIPSC])\s*[—\-–]\s*([^:]+):\s*(.+)$/i);
        if (!m) { if (tl === '' && lines[i+1] && /^STATUS:/i.test(lines[i+1].trim())) break; if (tl === '') { i++; continue; } else break; }
        const [, letter, name, rest] = m;
        const [verdict, ...expl] = rest.split(/\s*[—\-–]\s*/);
        html += `<tr>
          <td>${escHtml(letter)} — ${escHtml(name.trim())}</td>
          <td>${verdictChip(verdict.trim())} ${expl.length ? escHtml(expl.join(' — ')) : ''}</td>
        </tr>`;
        i++;
      }
      html += `</tbody></table>`;
      continue;
    }

    // ── PROBLEM_DEFINITION: ─────────────────────────────────── (skip inline, shown in report)
    if (/^PROBLEM_DEFINITION:/i.test(line)) {
      html += `<div class="bubble-heading">📋 Problem Definition Captured</div>`;
      // consume the JSON block
      i++;
      while (i < lines.length && lines[i].trim() !== '' && !lines[i].trim().startsWith('COACHING_NOTE:')) {
        i++;
      }
      continue;
    }

    // ── TIPS_OUTPUT: ────────────────────────────────────────── (skip inline, shown in report)
    if (/^TIPS_OUTPUT:/i.test(line)) {
      html += `<div class="bubble-heading">📊 TIPS Evaluation Complete</div>`;
      i++;
      while (i < lines.length && lines[i].trim() !== '') {
        i++;
      }
      continue;
    }

    // ── TIPS_TRIAGE: ─────────────────────────────────────────
    if (/^TIPS_TRIAGE:/i.test(line)) {
      html += `<div class="bubble-heading">TIPS Triage</div>`;
      html += `<table class="eval-table"><thead><tr><th>Dimension</th><th>Score</th><th>Reason</th></tr></thead><tbody>`;
      i++;
      while (i < lines.length) {
        const tl = lines[i].trim();
        // "T — Timely: GREEN — reason"
        const m = tl.match(/^([TIPS])\s*[—\-\u2013]\s*([^:]+):\s*(GREEN|YELLOW|RED)\s*[—\-\u2013]?\s*(.*)$/i);
        if (!m) { if (tl === '' || /^(DIM_IN_FOCUS|QUESTION|TIPS_OUTPUT)/i.test(tl)) break; i++; continue; }
        const [, letter, dimName, sc, reason] = m;
        const scU = sc.toUpperCase();
        const chipCls = scU === 'GREEN' ? 'chip-green' : scU === 'YELLOW' ? 'chip-yellow' : 'chip-red';
        const emoji   = scU === 'GREEN' ? '🟢' : scU === 'YELLOW' ? '🟡' : '🔴';
        html += `<tr>
          <td>${escHtml(letter)} — ${escHtml(dimName.trim())}</td>
          <td><span class="chip-tl ${chipCls}">${emoji} ${scU}</span></td>
          <td style="font-size:12px;color:var(--text-2)">${escHtml(reason)}</td>
        </tr>`;
        i++;
      }
      html += `</tbody></table>`;
      continue;
    }

    // ── COACHING_NOTE: ───────────────────────────────────────
    if (/^COACHING_NOTE:/i.test(line)) {
      let note = line.replace(/^COACHING_NOTE:\s*/i, '').trim();
      i++;
      while (i < lines.length && lines[i].trim() && !/^(MISSING_FIELDS:|QUESTION:|PROBLEM_DEFINITION:)/i.test(lines[i].trim())) {
        note += ' ' + lines[i].trim();
        i++;
      }
      html += `<div class="tips-coaching"><strong>Coach:</strong> ${escHtml(note)}</div>`;
      continue;
    }

    // ── MISSING_FIELDS: ──────────────────────────────────────
    if (/^MISSING_FIELDS:/i.test(line)) {
      const val = line.replace(/^MISSING_FIELDS:\s*/i, '').trim();
      html += `<div class="bubble-line" style="color:var(--text-3);font-size:12px;">Missing fields: <strong style="color:#fcd34d">${escHtml(val)}</strong></div>`;
      i++;
      continue;
    }

    // ── DIM_IN_FOCUS: ────────────────────────────────────────
    if (/^DIM_IN_FOCUS:/i.test(line)) {
      const val = line.replace(/^DIM_IN_FOCUS:\s*/i, '').trim();
      html += `<div class="bubble-line" style="color:var(--text-3);font-size:12px;">Focus dimension: <strong style="color:var(--violet-light)">${escHtml(val)}</strong></div>`;
      i++;
      continue;
    }

    // ── TIPSC TRIAGE: (legacy) ───────────────────────────────
    if (/^TIPSC TRIAGE:/i.test(line)) {
      html += `<div class="bubble-heading">TIPSC Triage</div>`;

      html += `<div class="bubble-heading">PSEA Evaluation</div>`;
      html += `<table class="eval-table"><thead><tr><th>Criterion</th><th>Assessment</th></tr></thead><tbody>`;
      i++;
      while (i < lines.length) {
        const pl = lines[i].trim();
        // Match "Problem-Solution Fit: Strong — explanation"
        const m = pl.match(/^([^:]+):\s+([A-Za-z\-\/]+)\s*[—\-–]\s*(.+)$/);
        if (!m) { if (pl === '' || /^(Key Assumptions|Initial Feasibility|VERDICT|ISSUES|NEXT STEP)/i.test(pl)) break; i++; continue; }
        const [, name, verdict, expl] = m;
        html += `<tr>
          <td>${escHtml(name.trim())}</td>
          <td>${verdictChip(verdict.trim())} ${escHtml(expl)}</td>
        </tr>`;
        i++;
      }
      html += `</tbody></table>`;
      continue;
    }

    // ── Key Assumptions ──────────────────────────────────────
    if (/^Key Assumptions:/i.test(line)) {
      html += `<div class="bubble-heading">Key Assumptions</div><ul class="bubble-list">`;
      i++;
      while (i < lines.length) {
        const al = lines[i].trim();
        if (!al || /^\d+\./.test(al) || al.startsWith('–') || al.startsWith('-') || al.startsWith('•')) {
          if (!al) { i++; break; }
          const text = al.replace(/^[\d\.–\-•]+\s*/, '');
          html += `<li>${escHtml(text)}</li>`;
          i++;
        } else break;
      }
      html += `</ul>`;
      continue;
    }

    // ── STATUS: lines ────────────────────────────────────────
    if (/^STATUS:\s+/i.test(line)) {
      const val = line.replace(/^STATUS:\s+/i, '').trim();
      if (/APPROVED/i.test(val)) {
        html += `<div><span class="status-badge status-approved">✓ Status: Approved</span></div>`;
      } else if (/NEEDS_MORE_INFO/i.test(val)) {
        html += `<div><span class="status-badge status-pending">⟳ Status: Needs More Info</span></div>`;
      } else {
        html += `<div><span class="status-badge status-pending">Status: ${escHtml(val)}</span></div>`;
      }
      i++;
      continue;
    }

    // ── VERDICT: lines ───────────────────────────────────────
    if (/^VERDICT:\s+/i.test(line)) {
      const val = line.replace(/^VERDICT:\s+/i, '').trim();
      if (/READY_FOR_DFV/i.test(val)) {
        html += `<div><span class="status-badge status-dfv">🎯 Verdict: Ready for DFV</span></div>`;
      } else if (/PROCEED/i.test(val)) {
        html += `<div><span class="status-badge status-approved">✓ Verdict: Proceed</span></div>`;
      } else if (/REJECT/i.test(val)) {
        html += `<div><span class="status-badge status-rejected">✗ Verdict: Reject</span></div>`;
      } else if (/NEEDS_REFINEMENT/i.test(val)) {
        html += `<div><span class="status-badge status-pending">⟳ Verdict: Needs Refinement</span></div>`;
      } else {
        html += `<div><span class="status-badge status-pending">Verdict: ${escHtml(val)}</span></div>`;
      }
      i++;
      continue;
    }

    // ── QUESTION: ────────────────────────────────────────────
    if (/^QUESTION:/i.test(line)) {
      let qText = line.replace(/^QUESTION:\s*/i, '').trim();
      i++;
      // May span multiple lines
      while (i < lines.length && lines[i].trim() && !/^(STATUS:|VERDICT:|FEEDBACK:|SUMMARY:|CRITERION|ISSUE IN|NEXT STEP)/i.test(lines[i].trim())) {
        qText += ' ' + lines[i].trim();
        i++;
      }
      html += `
        <div class="question-box">
          <span class="question-label">Agent is asking</span>
          ${escHtml(qText)}
        </div>
      `;
      continue;
    }

    // ── FEEDBACK: ────────────────────────────────────────────
    if (/^FEEDBACK:\s*/i.test(line)) {
      let fbText = line.replace(/^FEEDBACK:\s*/i, '').trim();
      i++;
      while (i < lines.length && lines[i].trim() && !/^(STATUS:|VERDICT:|QUESTION:|CRITERION|SUMMARY:|ISSUE IN|NEXT STEP)/i.test(lines[i].trim())) {
        fbText += ' ' + lines[i].trim();
        i++;
      }
      html += `
        <div class="feedback-box">
          <span class="feedback-label">Feedback</span>
          ${escHtml(fbText)}
        </div>
      `;
      continue;
    }

    // ── SUMMARY: / EVALUATION SUMMARY: ───────────────────────
    if (/^(EVALUATION )?SUMMARY:/i.test(line)) {
      const sumText = line.replace(/^(EVALUATION )?SUMMARY:\s*/i, '').trim();
      html += `<div class="bubble-heading">Summary</div>`;
      if (sumText) html += `<div class="bubble-line">${escHtml(sumText)}</div>`;
      i++;
      while (i < lines.length && lines[i].trim() && !/^(NEXT STEP|VERDICT:|STATUS:)/i.test(lines[i].trim())) {
        html += `<div class="bubble-line">${escHtml(lines[i].trim())}</div>`;
        i++;
      }
      continue;
    }

    // ── CRITERION IN FOCUS / ISSUE IN FOCUS ──────────────────
    if (/^(CRITERION IN FOCUS|ISSUE IN FOCUS):\s*/i.test(line)) {
      const val = line.replace(/^(CRITERION IN FOCUS|ISSUE IN FOCUS):\s*/i, '');
      html += `<div class="bubble-line" style="color:var(--text-3);font-size:12px;">Focus: <strong style="color:var(--violet-light)">${escHtml(val)}</strong></div>`;
      i++;
      continue;
    }

    // ── SEARCH FINDINGS / SEARCH CONTEXT ─────────────────────
    if (/^(SEARCH FINDINGS|WEB SEARCH CONTEXT|━+)/i.test(line)) {
      html += `<div class="bubble-heading">Search Findings</div>`;
      i++;
      continue;
    }

    // ── NEXT STEP: ───────────────────────────────────────────
    if (/^NEXT STEP:/i.test(line)) {
      const val = line.replace(/^NEXT STEP:\s*/i, '').trim();
      html += `<div class="bubble-line" style="margin-top:8px;color:var(--text-3);font-size:12px;">Next: ${escHtml(val)}</div>`;
      i++;
      continue;
    }

    // ── ISSUES: ──────────────────────────────────────────────
    if (/^ISSUES:/i.test(line)) {
      html += `<div class="bubble-heading">Issues Identified</div><ul class="bubble-list">`;
      i++;
      while (i < lines.length) {
        const il = lines[i].trim();
        if (!il) { i++; break; }
        if (/^(VERDICT:|STATUS:|QUESTION:)/i.test(il)) break;
        const text = il.replace(/^[\-–•\*\d\.]+\s*/, '');
        if (text) html += `<li>${escHtml(text)}</li>`;
        i++;
      }
      html += `</ul>`;
      continue;
    }

    // ── INITIAL FEASIBILITY ───────────────────────────────────
    if (/^Initial Feasibility:/i.test(line)) {
      const m = line.match(/^Initial Feasibility:\s+([A-Za-z\/]+)\s*[—\-–]\s*(.+)$/);
      if (m) {
        html += `<div class="bubble-line"><strong>Initial Feasibility:</strong> ${verdictChip(m[1])} ${escHtml(m[2])}</div>`;
      } else {
        html += `<div class="bubble-line">${escHtml(line)}</div>`;
      }
      i++;
      continue;
    }

    // ── Market-scout section headers ──────────────────────────
    if (/^(SEARCH FINDINGS:|MARKET LANDSCAPE|COMPETITIVE LANDSCAPE|MARKET ANALYSIS)/i.test(line)) {
      html += `<div class="bubble-heading">${escHtml(line.replace(/:$/, ''))}</div>`;
      i++;
      continue;
    }

    // ── Bullet items (•, -, –, *) ────────────────────────────
    if (/^[•\-–\*]\s+/.test(line)) {
      html += `<ul class="bubble-list">`;
      while (i < lines.length && /^[•\-–\*]\s+/.test(lines[i].trim())) {
        html += `<li>${escHtml(lines[i].trim().replace(/^[•\-–\*]\s+/, ''))}</li>`;
        i++;
      }
      html += `</ul>`;
      continue;
    }

    // ── Numbered items ───────────────────────────────────────
    if (/^\d+\.\s+/.test(line)) {
      html += `<ul class="bubble-list">`;
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
        html += `<li>${escHtml(lines[i].trim().replace(/^\d+\.\s+/, ''))}</li>`;
        i++;
      }
      html += `</ul>`;
      continue;
    }

    // ── Section-like ALL-CAPS heading (no colon needed) ───────
    if (line === line.toUpperCase() && line.length > 5 && line.length < 60 && /[A-Z]/.test(line)) {
      html += `<div class="bubble-heading">${escHtml(line)}</div>`;
      i++;
      continue;
    }

    // ── Empty line → spacer ───────────────────────────────────
    if (line === '') {
      i++;
      continue;
    }

    // ── Default plain line ────────────────────────────────────
    html += `<div class="bubble-line">${escHtml(line)}</div>`;
    i++;
  }

  return html;
}

// ── Verdict chip ──────────────────────────────────────────────
function verdictChip(text) {
  if (!text) return '';
  const t = text.trim().toLowerCase();
  let cls = 'chip';
  if (t.includes('strong'))    cls += ' chip-strong';
  else if (t.includes('good')) cls += ' chip-good';
  else if (t.includes('pass')) cls += ' chip-pass';
  else if (t.includes('viable')) cls += ' chip-viable';
  else if (t.includes('proceed')) cls += ' chip-proceed';
  else if (t.includes('weak'))   cls += ' chip-weak';
  else if (t.includes('fail'))   cls += ' chip-fail';
  else if (t.includes('reject')) cls += ' chip-reject';
  else if (t.includes('concern'))     cls += ' chip-concern';
  else if (t.includes('unclear'))     cls += ' chip-unclear';
  else if (t.includes('questionable')) cls += ' chip-unclear';
  else if (t.includes('over-engineer')) cls += ' chip-unclear';
  else cls += ' chip-unclear';
  return `<span class="${cls}">${escHtml(text.trim())}</span>`;
}

// ── Ethics Card ────────────────────────────────────────────────────────

function appendEthicsCard(data) {
  const el = document.createElement('div');
  el.className = 'msg agent-msg';

  let gateHtml = '';
  const gates = [
    { label: 'Harm Vector', score: data.harm_vector, reason: data.harm_reason },
    { label: 'Legal Risk', score: data.legal_risk, reason: data.legal_reason },
    { label: 'Problem-Solution Integrity', score: data.problem_solution_integrity, reason: data.integrity_reason }
  ];

  gates.forEach(g => {
    let icon = g.score === 'GREEN' ? '🟢' : (g.score === 'YELLOW' ? '🟡' : '🔴');
    gateHtml += `
      <div style="margin-bottom: 8px;">
        <strong>${g.label}:</strong> ${icon} ${g.score}
        <div style="font-size: 0.9em; color: var(--text-muted);">${g.reason}</div>
      </div>
    `;
  });

  let verdictHtml = '';
  if (!data.ethics_pass) {
    verdictHtml = `<div style="margin-top: 12px; padding: 12px; background: rgba(239, 68, 68, 0.1); color: #ef4444; border-radius: 6px;">
      <strong>❌ Failed Ethics Pre-Screen</strong><br>
      ${data.rejection_reason}
    </div>`;
  } else if (data.compliance_flag) {
    verdictHtml = `<div style="margin-top: 12px; padding: 12px; background: rgba(245, 158, 11, 0.1); color: #f59e0b; border-radius: 6px;">
      <strong>⚠️ Passed with Compliance Flag</strong><br>
      This idea operates in a regulated space and will require legal/compliance review.
    </div>`;
  } else {
    verdictHtml = `<div style="margin-top: 12px; padding: 12px; background: rgba(16, 185, 129, 0.1); color: #10b981; border-radius: 6px;">
      <strong>✅ Passed Ethics Pre-Screen</strong>
    </div>`;
  }

  el.innerHTML = `
    <div class="msg-avatar">🛡️</div>
    <div class="msg-content">
      <div class="msg-author">Ethics Pre-Screener</div>
      <div class="msg-bubble">
        <div style="font-family: var(--font-mono); font-size: 0.95em;">
          ${gateHtml}
          ${verdictHtml}
        </div>
      </div>
    </div>
  `;
  messagesFeed.appendChild(el);
  scrollToBottom();
}

// ── TIPS Rendering ───────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function now() {
  return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// ═══════════════════════════════════════════════════════════════
// Sidebar toggle
// ═══════════════════════════════════════════════════════════════

sidebarToggle.addEventListener('click', () => {
  sidebar.classList.toggle('collapsed');
});

// ═══════════════════════════════════════════════════════════════
// Button wiring
// ═══════════════════════════════════════════════════════════════

startBtn.addEventListener('click', startSession);

resetBtn.addEventListener('click', () => {
  if (confirm('Start a new validation session? This will reset the current session.')) {
    resetSession();
  }
});
