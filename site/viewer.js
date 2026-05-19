// Trace viewer. Loads ./trace.jsonl, parses one event per line, and
// renders three panels (actors / tools / state changes) up to the
// timeline slider's current position. No framework.

(function () {
  const els = {
    slider:    document.getElementById('slider'),
    playBtn:   document.getElementById('playBtn'),
    prevBtn:   document.getElementById('prevBtn'),
    nextBtn:   document.getElementById('nextBtn'),
    tickLabel: document.getElementById('tickLabel'),
    messages:  document.getElementById('messagesPanel'),
    tools:     document.getElementById('toolsPanel'),
    diff:      document.getElementById('diffPanel'),
  };

  let events = [];
  let actors = [];
  let position = 0;
  let playTimer = null;

  async function load() {
    let text = '';
    try {
      const resp = await fetch('trace.jsonl');
      if (!resp.ok) throw new Error('http ' + resp.status);
      text = await resp.text();
    } catch (err) {
      showError('could not load trace.jsonl: ' + err.message);
      return;
    }
    events = text
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean)
      .map((s) => {
        try {
          return JSON.parse(s);
        } catch (e) {
          return null;
        }
      })
      .filter(Boolean);
    actors = collectActors(events);
    els.slider.max = String(Math.max(events.length - 1, 0));
    els.slider.value = '0';
    setPosition(events.length > 0 ? events.length - 1 : 0);
  }

  function collectActors(events) {
    const seen = new Set();
    const order = [];
    for (const e of events) {
      if (e.actor && !seen.has(e.actor)) {
        seen.add(e.actor);
        order.push(e.actor);
      }
    }
    return order;
  }

  function setPosition(p) {
    position = clamp(p, 0, Math.max(events.length - 1, 0));
    els.slider.value = String(position);
    els.tickLabel.textContent = `${position + 1} / ${events.length}`;
    render();
  }

  function clamp(v, lo, hi) {
    return v < lo ? lo : v > hi ? hi : v;
  }

  function render() {
    const upTo = events.slice(0, position + 1);
    renderMessages(upTo);
    renderTools(upTo);
    renderDiff(upTo);
  }

  function renderMessages(slice) {
    const cols = {};
    for (const a of actors) cols[a] = [];
    for (const e of slice) {
      const k = e.payload && e.payload.kind;
      if (k !== 'user_message' && k !== 'agent_message') continue;
      const who = e.actor || '?';
      (cols[who] = cols[who] || []).push({
        kind: k,
        text: e.payload.text || '',
        tick: e.tick,
      });
    }
    const html = ['<h4>actors</h4>'];
    for (const a of actors) {
      const msgs = cols[a] || [];
      html.push(`<div class="actor-col">`);
      html.push(`<div class="actor-msg" style="border-color:#bbb"><div class="who">${escape(a)}</div></div>`);
      for (const m of msgs.slice(-5)) {
        html.push(`<div class="actor-msg"><div class="who">${escape(a)} &middot; tick ${m.tick}</div><div class="text">${escape(m.text)}</div></div>`);
      }
      html.push(`</div>`);
    }
    els.messages.innerHTML = html.join('');
  }

  function renderTools(slice) {
    const html = ['<h4>tool calls</h4>'];
    for (const e of slice) {
      const k = e.payload && e.payload.kind;
      if (k === 'tool_call') {
        html.push(`<div class="tool-line"><div><span class="name">${escape(e.payload.name)}</span> <span class="who">&middot; ${escape(e.actor || '?')} &middot; tick ${e.tick}</span></div><div class="args">${escape(JSON.stringify(e.payload.args))}</div></div>`);
      } else if (k === 'tool_result') {
        html.push(`<div class="tool-line"><div><span class="name">${escape(e.payload.name)}</span> <span class="who">&rarr; result</span></div><div class="res">${escape(JSON.stringify(e.payload.result).slice(0, 240))}</div></div>`);
      }
    }
    if (html.length === 1) html.push(`<div class="who">no tool activity yet</div>`);
    els.tools.innerHTML = html.join('');
  }

  function renderDiff(slice) {
    // Synthesize a small "what changed" table from state_diff + tool_result
    // payloads. Mirrors the boring table the spec asks for.
    const rows = [];
    for (const e of slice) {
      const k = e.payload && e.payload.kind;
      if (k === 'state_diff' && e.payload.diff) {
        const d = e.payload.diff;
        rows.push({
          who: e.actor || '?',
          table: d.table || (d.field || ''),
          field: d.field || '',
          old: stringify(d.old),
          new: stringify(d.new),
        });
      } else if (k === 'tool_result' && e.payload.result) {
        const r = e.payload.result;
        if (r && r.data && typeof r.data === 'object') {
          for (const [k2, v2] of Object.entries(r.data)) {
            rows.push({
              who: e.actor || '?',
              table: e.payload.name,
              field: k2,
              old: '',
              new: stringify(v2),
            });
          }
        }
      }
    }
    const html = ['<h4>state changes</h4>'];
    if (rows.length === 0) {
      html.push(`<div class="who">no state changes yet</div>`);
    } else {
      html.push('<table class="diff">');
      html.push('<thead><tr><th>actor</th><th>table</th><th>field</th><th>old</th><th>new</th></tr></thead><tbody>');
      for (const r of rows.slice(-12)) {
        html.push(`<tr><td>${escape(r.who)}</td><td>${escape(r.table)}</td><td>${escape(r.field)}</td><td>${escape(r.old)}</td><td>${escape(r.new)}</td></tr>`);
      }
      html.push('</tbody></table>');
    }
    els.diff.innerHTML = html.join('');
  }

  function stringify(v) {
    if (v == null) return '';
    if (typeof v === 'string') return v;
    try { return JSON.stringify(v); } catch (e) { return String(v); }
  }

  function escape(s) {
    return String(s)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;');
  }

  els.slider.addEventListener('input', () => setPosition(Number(els.slider.value)));
  els.prevBtn.addEventListener('click', () => setPosition(position - 1));
  els.nextBtn.addEventListener('click', () => setPosition(position + 1));
  els.playBtn.addEventListener('click', () => {
    if (playTimer) {
      clearInterval(playTimer);
      playTimer = null;
      els.playBtn.textContent = 'play';
      return;
    }
    els.playBtn.textContent = 'pause';
    if (position >= events.length - 1) setPosition(0);
    playTimer = setInterval(() => {
      if (position >= events.length - 1) {
        clearInterval(playTimer);
        playTimer = null;
        els.playBtn.textContent = 'play';
        return;
      }
      setPosition(position + 1);
    }, 1000);
  });

  function showError(msg) {
    els.messages.innerHTML = '<h4>actors</h4><div class="who">' + escape(msg) + '</div>';
    els.tools.innerHTML = '<h4>tool calls</h4>';
    els.diff.innerHTML = '<h4>state changes</h4>';
  }

  load();
})();
