// Turns the current page into compact, LLM-friendly text.
//
// * Visible text is kept in reading order (headings as "#", table rows as "| a | b |").
// * Every interactive element (link, button, input, select, textarea) is replaced
//   by an inline token like  [12:button "Save bill"]  and tagged in the DOM with
//   data-agent-ref="12" so the browser tools can act on it by number.
// * Hidden elements, scripts and styles are skipped.
//
// Why text instead of screenshots? It is ~10x cheaper in tokens, exact (no OCR
// errors on amounts/dates), and element refs make actions unambiguous.
// Screenshots are still taken for evidence and as an optional vision fallback.
(maxChars) => {
  document.querySelectorAll('[data-agent-ref]').forEach(e => e.removeAttribute('data-agent-ref'));
  let nextRef = 1;
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'SVG', 'META', 'LINK']);
  const BLOCK = new Set(['DIV', 'P', 'SECTION', 'ARTICLE', 'HEADER', 'FOOTER', 'MAIN', 'NAV', 'ASIDE', 'FORM',
    'TABLE', 'THEAD', 'TBODY', 'TFOOT', 'UL', 'OL', 'DL', 'DT', 'DD', 'FIELDSET', 'FIGURE', 'BLOCKQUOTE', 'BR', 'HR']);
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const q = s => '"' + clean(s).slice(0, 80).replace(/"/g, "'") + '"';

  const visible = el => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
    if (el.tagName === 'OPTION') return true;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0 || st.display === 'contents';
  };

  const labelOf = el => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return l.innerText;
    }
    const parentLabel = el.closest('label');
    if (parentLabel) return parentLabel.innerText;
    return el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '';
  };

  const isInteractive = el => {
    const t = el.tagName;
    if (t === 'A') return el.hasAttribute('href');
    if (t === 'BUTTON' || t === 'SELECT' || t === 'TEXTAREA') return true;
    if (t === 'INPUT') return (el.type || 'text') !== 'hidden';
    const role = el.getAttribute('role');
    return role === 'button' || role === 'link' || el.hasAttribute('onclick');
  };

  const describe = (el, ref) => {
    const t = el.tagName;
    el.setAttribute('data-agent-ref', String(ref));
    if (t === 'A') {
      let href = el.getAttribute('href') || '';
      return `[${ref}:link ${q(el.innerText || el.title || href)} -> ${href}]`;
    }
    if (t === 'BUTTON' || el.getAttribute('role') === 'button') {
      return `[${ref}:button ${q(el.innerText || el.value || labelOf(el))}${el.disabled ? ' disabled' : ''}]`;
    }
    if (t === 'SELECT') {
      const opts = [...el.options].map(o => clean(o.text)).slice(0, 25);
      const sel = el.selectedOptions[0] ? clean(el.selectedOptions[0].text) : '';
      return `[${ref}:select ${q(labelOf(el))} selected=${q(sel)} options=[${opts.map(o => q(o)).join(', ')}]]`;
    }
    if (t === 'TEXTAREA') return `[${ref}:textarea ${q(labelOf(el))} value=${q(el.value)}]`;
    if (t === 'INPUT') {
      const type = (el.type || 'text').toLowerCase();
      if (type === 'submit' || type === 'button') return `[${ref}:button ${q(el.value || labelOf(el))}]`;
      if (type === 'checkbox' || type === 'radio') return `[${ref}:${type} ${q(labelOf(el))} checked=${el.checked}]`;
      const val = type === 'password' ? (el.value ? '********' : '') : el.value;
      return `[${ref}:input type=${type} ${q(labelOf(el))} value=${q(val)}]`;
    }
    return `[${ref}:clickable ${q(el.innerText)}]`;
  };

  const render = node => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent.replace(/\s+/g, ' ');
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const el = node, t = el.tagName;
    if (SKIP.has(t) || !visible(el)) return '';
    if (t === 'LABEL' && el.htmlFor && document.getElementById(el.htmlFor)) return ''; // shown on the input
    if (isInteractive(el)) return ' ' + describe(el, nextRef++) + ' ';
    if (t === 'PRE') return '\n' + el.innerText + '\n';
    if (t === 'TR') {
      const cells = [...el.children].map(c => clean([...c.childNodes].map(render).join('')));
      return '\n| ' + cells.join(' | ') + ' |';
    }
    const inner = [...el.childNodes].map(render).join('');
    if (/^H[1-6]$/.test(t)) return '\n' + '#'.repeat(+t[1]) + ' ' + clean(inner) + '\n';
    if (t === 'LI') return '\n- ' + clean(inner);
    if (BLOCK.has(t)) return '\n' + inner + '\n';
    return inner;
  };

  let text = render(document.body)
    .split('\n').map(l => l.replace(/[ \t]+/g, ' ').trim()).filter((l, i, a) => l || (a[i - 1] && a[i - 1].trim()))
    .join('\n').replace(/\n{3,}/g, '\n\n').trim();
  let truncated = false;
  if (text.length > maxChars) { text = text.slice(0, maxChars); truncated = true; }
  return {
    url: location.href,
    title: document.title,
    text,
    truncated,
    refs: nextRef - 1,
    hasPasswordField: !!document.querySelector('input[type=password]'),
  };
}
