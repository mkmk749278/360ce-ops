/* Click-to-sort for every .data-table — progressive enhancement, no deps.
 *
 * Click a header to sort by that column (asc), click again to flip. Numeric-
 * aware: a column sorts numerically when most of its cells parse as numbers
 * (handles "—", "%", "+", "," and blank cells gracefully — blanks sink to
 * the bottom in both directions). Rows keep their server-rendered order as
 * the tiebreak, so e.g. the Signals page's actives-first grouping survives
 * a sort on an equal-valued column.
 *
 * Also re-applies to HTMX-swapped content (htmx:afterSwap).
 */
(function () {
  "use strict";

  function cellText(row, idx) {
    var cell = row.cells[idx];
    if (!cell) return "";
    var t = (cell.getAttribute("data-sort") || cell.textContent || "").trim();
    return t;
  }

  function toNumber(text) {
    if (!text || text === "—" || text === "-") return null;
    var cleaned = text.replace(/[,%+\s]/g, "").replace(/[▲▼↕]/g, "");
    if (cleaned === "" || isNaN(cleaned)) return null;
    return parseFloat(cleaned);
  }

  function sortTable(table, idx, dir) {
    var tbody = table.tBodies[0];
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.rows);

    var numeric = 0, filled = 0;
    rows.forEach(function (r) {
      var t = cellText(r, idx);
      if (t !== "" && t !== "—") {
        filled++;
        if (toNumber(t) !== null) numeric++;
      }
    });
    var asNumbers = filled > 0 && numeric / filled >= 0.6;

    rows
      .map(function (row, i) { return { row: row, i: i, text: cellText(row, idx) }; })
      .sort(function (a, b) {
        var av, bv;
        if (asNumbers) {
          av = toNumber(a.text); bv = toNumber(b.text);
          if (av === null && bv === null) return a.i - b.i;
          if (av === null) return 1;   // blanks sink regardless of direction
          if (bv === null) return -1;
          return dir * (av - bv) || (a.i - b.i);
        }
        av = a.text.toLowerCase(); bv = b.text.toLowerCase();
        if (av === bv) return a.i - b.i;
        if (av === "" || av === "—") return 1;
        if (bv === "" || bv === "—") return -1;
        return dir * (av < bv ? -1 : 1);
      })
      .forEach(function (item) { tbody.appendChild(item.row); });
  }

  function enhance(root) {
    (root || document).querySelectorAll("table.data-table").forEach(function (table) {
      var head = table.tHead;
      if (!head || table.dataset.sortWired) return;
      table.dataset.sortWired = "1";
      Array.prototype.forEach.call(head.rows[0].cells, function (th, idx) {
        if (!th.textContent.trim()) return; // action/spacer columns stay inert
        th.classList.add("sortable");
        th.addEventListener("click", function () {
          var dir = th.classList.contains("sort-asc") ? -1 : 1;
          Array.prototype.forEach.call(head.rows[0].cells, function (h) {
            h.classList.remove("sort-asc", "sort-desc");
          });
          th.classList.add(dir === 1 ? "sort-asc" : "sort-desc");
          sortTable(table, idx, dir);
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () { enhance(document); });
  document.body && enhance(document);
  document.addEventListener("htmx:afterSwap", function (ev) { enhance(ev.target); });
})();
