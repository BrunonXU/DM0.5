// The robot console is also served over LAN HTTP, without the Clipboard API.
export async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return true; } catch {}
  }
  const focused = document.activeElement;
  const selection = window.getSelection();
  const ranges = selection ? Array.from({length: selection.rangeCount}, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
  const field = document.createElement('textarea');
  field.value = text;
  field.readOnly = true;
  field.style.cssText = 'position:fixed;left:-9999px;top:0;font-size:16px';
  // Elements outside an open modal are inert, including a fallback textarea.
  const host=focused?.closest('dialog[open]')||[...document.querySelectorAll('dialog[open]')].at(-1)||document.body;
  host.append(field);
  let copied = false;
  try {
    field.focus({preventScroll:true});
    field.select();
    copied = document.execCommand('copy');
  } catch {} finally {
    field.remove();
    if (focused?.isConnected) focused.focus({preventScroll:true});
    if (selection) {
      selection.removeAllRanges();
      ranges.forEach(range => selection.addRange(range));
    }
  }
  return copied;
}
