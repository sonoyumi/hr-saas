// Comfort only: the cabinet works without JavaScript.
"use strict";

document.addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-href]");
  if (row && !e.target.closest("a, button, input, select")) location.href = row.dataset.href;
});

document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});
