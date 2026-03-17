const addTextarea = document.getElementById("codes-input");
const addSummary = document.getElementById("codes-summary");

const correctTextarea = document.getElementById("correct-codes-input");
const correctSummary = document.getElementById("correct-codes-summary");
const correctResult = document.getElementById("correct-result");

const manualResult = document.getElementById("manual-result");
const manualPreview = document.getElementById("manual-preview");
const manualPreviewBody = document.getElementById("manual-preview-body");

let manualLast = null;

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function normalizeLines(text) {
  return (text || "")
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
}

function updateSummary(textarea, summaryNode) {
  const lines = normalizeLines(textarea.value);
  summaryNode.innerHTML = `Liczba kodów: <strong>${lines.length}</strong>`;
}

async function saveCodes() {
  const value = parseInt(document.getElementById("nominal-select").value, 10);
  const codes = normalizeLines(addTextarea.value);

  if (!codes.length) {
    alert("Wpisz przynajmniej jeden kod.");
    return;
  }

  try {
    const res = await fetch("/admin/api/codes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value, codes }),
    });

    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Błąd zapisu");

    alert(`Zapisano ${data.inserted} kodów.`);
    addTextarea.value = "";
    updateSummary(addTextarea, addSummary);
    loadStats();
    maybeLoadCodes();
  } catch (e) {
    alert(e.message || "Błąd komunikacji z serwerem.");
  }
}

async function correctValue() {
  const newValue = parseInt(document.getElementById("correct-new-value").value, 10);
  const codes = normalizeLines(correctTextarea.value);

  if (!codes.length) {
    correctResult.innerHTML = '<span class="pill err">Błąd</span> Wklej przynajmniej jeden kod.';
    return;
  }

  correctResult.innerHTML = '<span class="pill">Przetwarzanie</span> Trwa korekta nominału...';

  try {
    const res = await fetch("/admin/api/codes/correct-value", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ newValue, codes }),
    });

    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Błąd korekty");

    correctResult.innerHTML =
      '<span class="pill ok">OK</span> ' +
      `Zmieniono: <strong>${data.updated}</strong>, ` +
      `pominięto przypisane: <strong>${data.skipped_assigned}</strong>, ` +
      `nie znaleziono: <strong>${data.not_found}</strong>.`;

    loadStats();
    maybeLoadCodes();
  } catch (e) {
    correctResult.innerHTML = '<span class="pill err">Błąd</span> ' + escapeHtml(e.message || String(e));
  }
}

async function loadStats() {
  const container = document.getElementById("stats-container");
  container.innerHTML = '<div class="muted">Ładowanie statystyk...</div>';

  try {
    const res = await fetch("/admin/api/stats");
    const data = await res.json();

    if (!res.ok) throw new Error(data.detail || "Błąd statystyk");

    if (!Array.isArray(data) || !data.length) {
      container.innerHTML = '<div class="muted">Brak danych statystycznych.</div>';
      return;
    }

    container.innerHTML = "";
    data.forEach((row) => {
      const card = document.createElement("div");
      card.className = "stat-card";
      card.innerHTML = `
        <h3>${escapeHtml(row.value)} zł</h3>
        <div class="stat-row">Łącznie: <strong>${row.total}</strong></div>
        <div class="stat-row">Nieużyte: <strong>${row.unused}</strong></div>
        <div class="stat-row">Użyte: <strong>${row.used}</strong></div>
      `;
      container.appendChild(card);
    });
  } catch (e) {
    container.innerHTML = '<div class="muted">Nie udało się pobrać statystyk.</div>';
  }
}

async function loadCodes() {
  const tbody = document.getElementById("codes-tbody");
  const emptyState = document.getElementById("codes-empty-state");
  const tableWrap = document.getElementById("codes-table-wrap");

  const filterValue = document.getElementById("filter-value").value;
  const filterUsed = document.getElementById("filter-used").value;
  const filterLimit = document.getElementById("filter-limit").value;

  if (!filterValue) {
    tableWrap.classList.add("hidden");
    emptyState.classList.remove("hidden");
    emptyState.textContent = "Wybierz nominał, aby zobaczyć listę kodów.";
    return;
  }

  emptyState.classList.add("hidden");
  tableWrap.classList.remove("hidden");

  tbody.innerHTML = '<tr><td colspan="5" class="muted center padded">Ładowanie danych...</td></tr>';

  const params = new URLSearchParams();
  params.set("value", filterValue);
  if (filterUsed) params.set("used", filterUsed);
  if (filterLimit) params.set("limit", filterLimit);

  try {
    const res = await fetch("/admin/api/codes?" + params.toString());
    const data = await res.json();

    if (!res.ok) throw new Error(data.detail || "Błąd pobierania");

    if (!Array.isArray(data) || !data.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted center padded">Brak rekordów dla wybranego filtra.</td></tr>';
      return;
    }

    tbody.innerHTML = "";
    data.forEach((row) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${row.id}</td>
        <td><strong>${escapeHtml(row.code)}</strong></td>
        <td>${row.value} zł</td>
        <td>${row.used ? '<span class="pill ok">Użyty</span>' : '<span class="pill">Nieużyty</span>'}</td>
        <td>${row.order_id ? escapeHtml(String(row.order_id)) : "—"}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="5" class="muted center padded">Nie udało się pobrać danych.</td></tr>';
  }
}

function maybeLoadCodes() {
  const value = document.getElementById("filter-value").value;
  if (value) loadCodes();
}

function exportCsv() {
  const filterValue = document.getElementById("filter-value").value;
  const filterUsed = document.getElementById("filter-used").value;

  if (!filterValue) {
    alert("Najpierw wybierz nominał do eksportu.");
    return;
  }

  const params = new URLSearchParams();
  params.set("value", filterValue);
  if (filterUsed) params.set("used", filterUsed);

  window.open("/admin/api/codes/export?" + params.toString(), "_blank");
}

async function loadLogs() {
  const tbody = document.getElementById("logs-tbody");
  tbody.innerHTML = '<tr><td colspan="5" class="muted center padded">Ładowanie logów...</td></tr>';

  try {
    const res = await fetch("/admin/api/logs");
    const data = await res.json();

    if (!res.ok) throw new Error(data.detail || "Błąd logów");

    if (!Array.isArray(data) || !data.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted center padded">Brak logów.</td></tr>';
      return;
    }

    tbody.innerHTML = "";
    data.forEach((row) => {
      const statusClass =
        row.status === "processed" ? "ok" :
        row.status === "error" ? "err" : "";

      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(row.created_at || "—")}</td>
        <td><span class="pill ${statusClass}">${escapeHtml(row.status || "—")}</span></td>
        <td>${escapeHtml(row.order_id || "—")}</td>
        <td>${escapeHtml(row.order_serial || "—")}</td>
        <td>${escapeHtml(row.message || "—")}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="5" class="muted center padded">Nie udało się pobrać logów.</td></tr>';
  }
}

function renderManualPreview(data) {
  if (!data || !Array.isArray(data.codes) || !data.codes.length) {
    manualPreview.classList.add("hidden");
    manualPreviewBody.innerHTML = "";
    return;
  }

  manualPreview.classList.remove("hidden");
  manualPreviewBody.innerHTML = data.codes
    .map(
      (item) => `
        <div class="preview-item">
          <div><strong>Kod:</strong> ${escapeHtml(item.code)}</div>
          <div><strong>Nominał:</strong> ${escapeHtml(item.value)} zł</div>
        </div>
      `
    )
    .join("");
}

async function manualLoad() {
  const orderSerialNumber = (document.getElementById("manual-order").value || "").trim();
  const out = manualResult;

  if (!orderSerialNumber) {
    out.innerHTML = '<span class="pill err">Błąd</span> Podaj numer zamówienia.';
    return;
  }

  out.innerHTML = '<span class="pill">Ładowanie</span> Pobieram przypisane karty...';

  try {
    const res = await fetch("/admin/api/manual/order?orderSerialNumber=" + encodeURIComponent(orderSerialNumber));
    const data = await res.json();

    if (!res.ok) throw new Error(data.detail || "Nie znaleziono zamówienia");

    if (data.email) {
      document.getElementById("manual-email").value = data.email;
    }

    renderManualPreview(data);

    out.innerHTML =
      '<span class="pill ok">OK</span> ' +
      `Znaleziono <strong>${data.codes.length}</strong> kart(y) dla zamówienia <strong>${escapeHtml(orderSerialNumber)}</strong>.`;
  } catch (e) {
    renderManualPreview(null);
    out.innerHTML = '<span class="pill err">Błąd</span> ' + escapeHtml(e.message || String(e));
  }
}

async function manualIssue() {
  const value = parseInt(document.getElementById("manual-value").value, 10);
  const orderSerialNumber = (document.getElementById("manual-order").value || "").trim();
  const email = (document.getElementById("manual-email").value || "").trim();
  const out = manualResult;

  if (!value || value <= 0) {
    out.innerHTML = '<span class="pill err">Błąd</span> Wybierz poprawny nominał.';
    return;
  }

  if (!orderSerialNumber) {
    out.innerHTML = '<span class="pill err">Błąd</span> Podaj numer zamówienia.';
    return;
  }

  if (!email) {
    out.innerHTML = '<span class="pill err">Błąd</span> Podaj adres e-mail.';
    return;
  }

  out.innerHTML = '<span class="pill">Przetwarzanie</span> Sprawdzam i przypisuję kod...';

  try {
    const res = await fetch("/admin/api/manual/issue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value, orderSerialNumber, email }),
    });

    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Błąd ręcznego przypisania");

    manualLast = data;

    const reused = data.reused ? "TAK (z bazy)" : "NIE (nowy kod)";
    const note = data.noteUpdated ? " Notatka w Idosell została zaktualizowana." : "";

    out.innerHTML =
      '<span class="pill ok">OK</span> ' +
      `Kod: <strong>${escapeHtml(data.code)}</strong> (${data.value} zł) • ` +
      `Zamówienie: <strong>${escapeHtml(data.orderSerialNumber)}</strong> • ` +
      `Reuse: <strong>${reused}</strong>.${note}`;

    await manualLoad();
  } catch (e) {
    out.innerHTML = '<span class="pill err">Błąd</span> ' + escapeHtml(e.message || String(e));
  }
}

function manualPdf() {
  const orderSerialNumber = (document.getElementById("manual-order").value || "").trim();
  if (!orderSerialNumber) {
    manualResult.innerHTML = '<span class="pill err">Błąd</span> Podaj numer zamówienia.';
    return;
  }

  window.location.href = "/admin/api/manual/pdf?orderSerialNumber=" + encodeURIComponent(orderSerialNumber);
}

async function manualSendEmail() {
  const orderSerialNumber = (document.getElementById("manual-order").value || "").trim();
  const email = (document.getElementById("manual-email").value || "").trim();
  const attachPdf = document.getElementById("manual-attach-pdf").checked;

  if (!orderSerialNumber || !email) {
    manualResult.innerHTML = '<span class="pill err">Błąd</span> Podaj numer zamówienia i adres e-mail.';
    return;
  }

  manualResult.innerHTML = '<span class="pill">E-mail</span> Wysyłam wiadomość...';

  try {
    const res = await fetch("/admin/api/manual/send-email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ orderSerialNumber, email, attachPdf }),
    });

    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Błąd wysyłki");

    manualResult.innerHTML =
      '<span class="pill ok">Wysłano</span> ' +
      `Na: <strong>${escapeHtml(data.sentTo)}</strong> • PDF: <strong>${data.attachPdf ? "tak" : "nie"}</strong>.`;
  } catch (e) {
    manualResult.innerHTML = '<span class="pill err">Błąd</span> ' + escapeHtml(e.message || String(e));
  }
}

document.addEventListener("DOMContentLoaded", () => {
  addTextarea.addEventListener("input", () => updateSummary(addTextarea, addSummary));
  correctTextarea.addEventListener("input", () => updateSummary(correctTextarea, correctSummary));

  document.getElementById("btn-save-codes").addEventListener("click", saveCodes);
  document.getElementById("btn-correct-value").addEventListener("click", correctValue);

  document.getElementById("btn-refresh-stats").addEventListener("click", loadStats);
  document.getElementById("btn-refresh-codes").addEventListener("click", loadCodes);
  document.getElementById("btn-apply-filters").addEventListener("click", loadCodes);
  document.getElementById("btn-export-csv").addEventListener("click", exportCsv);

  document.getElementById("btn-manual-load").addEventListener("click", manualLoad);
  document.getElementById("btn-manual-issue").addEventListener("click", manualIssue);
  document.getElementById("btn-manual-pdf").addEventListener("click", manualPdf);
  document.getElementById("btn-manual-email").addEventListener("click", manualSendEmail);

  document.getElementById("btn-refresh-logs").addEventListener("click", loadLogs);

  updateSummary(addTextarea, addSummary);
  updateSummary(correctTextarea, correctSummary);
  loadStats();
  loadLogs();
});
