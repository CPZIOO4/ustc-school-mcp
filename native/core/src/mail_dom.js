(function(action, label) {
  const visible = e => !!(e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
  const unique = (scope, labels) => {
    const found = [...scope.querySelectorAll('*')].filter(e => visible(e) && labels.includes(e.textContent.trim()) &&
      ![...e.children].some(c => visible(c) && labels.includes(c.textContent.trim())));
    return found.length === 1 ? found[0] : null;
  };
  const form = () => {
    const button = unique(document, ['生成', 'Generate']);
    if (!button) return null;
    let p = button;
    for (let n = 0; n < 7 && (p = p.parentElement); n++) {
      if (['BODY', 'HTML'].includes(p.tagName)) return null;
      const inputs = [...p.querySelectorAll('input[type=text],input:not([type])')].filter(visible);
      if (inputs.length === 1 && ![...p.querySelectorAll('input[type=password]')].some(visible) &&
          /密码名称|Password name|Token name/i.test(p.innerText)) return {input: inputs[0], button};
    }
    return null;
  };
  if (action === 'open') {
    const button = unique(document, ['生成专用密码', 'Generate Specific Password']);
    if (!button || button.disabled) return false;
    button.click(); return true;
  }
  if (action === 'name' || action === 'generate') {
    const f = form(); if (!f || f.button.disabled) return false;
    if (action === 'name') {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(f.input, label);
      f.input.dispatchEvent(new Event('input', {bubbles:true}));
      f.input.dispatchEvent(new Event('change', {bubbles:true}));
      return f.input.value === label;
    }
    if (f.input.value !== label) return false;
    f.button.click(); return true;
  }
  if (action === 'capture') {
    const title = unique(document, ['专用密码已生成', '专用密码生成成功', '生成专用密码成功', 'Specific password generated']);
    let p = title; const texts = [];
    for (let n = 0; p && n < 7 && (p = p.parentElement); n++) {
      if (['BODY', 'HTML'].includes(p.tagName)) break;
      if (p.innerText.length <= 12000) texts.push(p.innerText);
    }
    return texts;
  }
  return false;
})
