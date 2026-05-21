// Trace compare: load /trace_a.jsonl and /trace_b.jsonl, render
// each as a column of message events, and offer a sync-by-tick
// scroll mode. The host (`ensemble trace compare a b`) bakes both
// traces into the embedded server.

async function loadTrace(path) {
  const resp = await fetch(path);
  if (!resp.ok) return [];
  const text = await resp.text();
  return text
    .split('\n')
    .filter((l) => l.trim().length > 0)
    .map((l) => {
      try { return JSON.parse(l); } catch (e) { return null; }
    })
    .filter((e) => e !== null);
}

function summarizeEvent(ev) {
  const payload = ev.payload || {};
  const kind = payload.kind || 'event';
  if (kind === 'agent_message' || kind === 'user_message') {
    return { tick: ev.tick, actor: ev.actor, kind, text: payload.text || '' };
  }
  if (kind === 'tool_call') {
    return {
      tick: ev.tick,
      actor: ev.actor,
      kind,
      text: `tool_call: ${payload.name}(${JSON.stringify(payload.args || {})})`,
    };
  }
  if (kind === 'tool_result') {
    return {
      tick: ev.tick,
      actor: ev.actor,
      kind,
      text: `tool_result: ${payload.name} -> ${JSON.stringify(payload.result || {})}`,
    };
  }
  if (kind === 'system') {
    return { tick: ev.tick, actor: 'system', kind, text: payload.note || '' };
  }
  return { tick: ev.tick, actor: ev.actor || '?', kind, text: JSON.stringify(payload) };
}

function render(feed, events) {
  const rows = events.map(summarizeEvent);
  feed.innerHTML = '';
  for (const r of rows) {
    const div = document.createElement('div');
    div.className = `compare-row compare-${r.kind}`;
    div.dataset.tick = r.tick;
    div.style.marginBottom = '8px';
    div.style.fontSize = '13px';
    div.style.borderLeft = '3px solid #888';
    div.style.paddingLeft = '8px';
    const head = document.createElement('div');
    head.style.color = '#666';
    head.style.fontSize = '11px';
    head.textContent = `tick=${r.tick} ${r.actor} (${r.kind})`;
    const body = document.createElement('div');
    body.style.whiteSpace = 'pre-wrap';
    body.textContent = r.text;
    div.appendChild(head);
    div.appendChild(body);
    feed.appendChild(div);
  }
}

function setLabel(id, label, count) {
  document.getElementById(id).textContent = `${label}: ${count} events`;
}

function setupSyncScroll(colA, colB, getEnabled) {
  let aLast = 0;
  let bLast = 0;
  let suppress = false;
  function tickOfTopChild(col) {
    for (const child of col.querySelectorAll('.compare-row')) {
      const r = child.getBoundingClientRect();
      if (r.bottom >= col.getBoundingClientRect().top) {
        return Number(child.dataset.tick || 0);
      }
    }
    return 0;
  }
  function scrollOtherToTick(other, tick) {
    let target = null;
    for (const child of other.querySelectorAll('.compare-row')) {
      const t = Number(child.dataset.tick || 0);
      if (t >= tick) { target = child; break; }
    }
    if (target) {
      suppress = true;
      target.scrollIntoView({ block: 'start' });
      requestAnimationFrame(() => { suppress = false; });
    }
  }
  colA.addEventListener('scroll', () => {
    if (!getEnabled() || suppress) return;
    const t = tickOfTopChild(colA);
    if (t !== aLast) {
      aLast = t;
      scrollOtherToTick(colB, t);
    }
  });
  colB.addEventListener('scroll', () => {
    if (!getEnabled() || suppress) return;
    const t = tickOfTopChild(colB);
    if (t !== bLast) {
      bLast = t;
      scrollOtherToTick(colA, t);
    }
  });
}

(async function init() {
  const [a, b] = await Promise.all([
    loadTrace('/trace_a.jsonl'),
    loadTrace('/trace_b.jsonl'),
  ]);
  render(document.getElementById('feedA'), a);
  render(document.getElementById('feedB'), b);
  setLabel('aLabel', 'A', a.length);
  setLabel('bLabel', 'B', b.length);

  const sync = document.getElementById('syncScroll');
  setupSyncScroll(
    document.getElementById('colA'),
    document.getElementById('colB'),
    () => sync.checked,
  );
})();
