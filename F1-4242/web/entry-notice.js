// Keep the entry notice responsive while the workbench module loads.
(() => {
 const notice = document.querySelector('#entry-notice');
 notice.addEventListener('cancel', event => event.preventDefault());
 notice.addEventListener('keydown', event => {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); }
 });
 document.querySelector('#enter-workbench').addEventListener('click', () => notice.close());
 notice.showModal();
 // A restored document counts as reopening; navigation inside it does not.
 window.addEventListener('pageshow', event => {
  if (event.persisted && !notice.open) notice.showModal();
 });
})();
