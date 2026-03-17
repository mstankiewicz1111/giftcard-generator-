const textarea = document.getElementById("codes-input");
const summary = document.getElementById("codes-summary");
const manualResult = document.getElementById("manual-result");

let manualLast = null;

function updateSummary() {
  const text = textarea.value.trim();
  if (!text) {
    summary.innerHTML = 'Liczba kodów: <strong>0</strong>';
    return;
  }

  const lines = text
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  summary.innerHTML = 'Liczba kodów: <strong>' + lines.length + '</strong>';
}

async function saveCodes() {
  const nominalSelect = document.getElementById("nominal-select");
  const value = parseInt(nominalSelect.value, 10);
  const text = textarea.value.trim();

  if (!text) {
    alert("Wpisz przynajmniej jeden kod.");
    return;
  }

  const lines = text
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  if (lines.length === 0) {
    alert("Brak poprawnych linii z kodami.");
    return;
  }

  try {
    const res = await fetch("/admin/api/codes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        value: value,
        codes: lines
      }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert("Błąd podczas zapisywania kodów: " + (err.detail || res.status));
      return;
    }

    const data = await res.json();
    alert("Zapisano " + data.inserted + " kodów.");
    textarea.value = "";
    updateSummary();
    loadStats();
    loadCodes();
  } catch (e) {
    console.error(e);
    alert("Wystąpił błąd przy komunikacji z serwerem.");
  }
}

async function loadStats() {
  const statsEl = document.getElementById("stats-container");
  statsEl.innerHTML = '<span class="muted">Ładowanie statystyk...</span>';

  try {
    const res = await fetch("/admin/api/stats");
    if (!res.ok) {
      statsEl.innerHTML = '<span class="muted">Błąd przy pobieraniu statystyk.</span>';
      return;
    }

    const data = await res.json();
    if (!data || data.length === 0) {
      statsEl.innerHTML = '<span class="muted">Brak danych statystycznych.</span>';
      return;
    }

    const labels = {
      100: "100 zł",
      200: "200 zł",
      300: "300 zł",
      400: "400 zł",
      500: "500 zł",
    };

    statsEl.innerHTML = "";
    data.forEach((row) => {
      const div = document.createElement("div");
      div.className = "chip";
      const label = labels[row.value] || row.value + " zł";
      div.innerHTML =
        "<strong>" +
        label +
        "</strong>&nbsp;&nbsp;Łącznie: " +
        row.total +
        " &nbsp;•&nbsp; Nieużyte: " +
        row.unused +
        " &nbsp;•&nbsp; Użyte: " +
        row.used;
      statsEl.appendChild(div);
    });
  } catch (e) {
    console.error(e);
    statsEl.innerHTML = '<span class="muted">Błąd przy pobieraniu statystyk.</span>';
  }
}

async function loadCodes() {
  const tbody = document.getElementById("codes-tbody");
  tbody.innerHTML =
    '<tr><td colspan="5" class="muted center padded">Ładowanie danych...</td></tr>';

  const filterValue = document.getElementById("filter-value").value;
  const filterUsed = document.getElementById("filter-used").value;
  const filterLimit = document.getElementById("filter-limit").value;

  const params = new URLSearchParams();
  if (filterValue) params.set("value", filterValue);
  if (filterUsed) params.set("used", filterUsed);
  if (filterLimit) params.set("limit", filterLimit);

  try {
    const res = await fetch("/admin/api/codes?" + params.toString());
    if (!res.ok) {
      tbody.innerHTML =
        '<tr><td colspan="5" class="muted center padded">Błąd przy pobieraniu danych.</td></tr>';
      return;
    }

    const data = await res.json();
    if (!Array.isArray(data) || data.length === 0) {
      tbody.innerHTML =
        '<tr><td colspan="5" class="muted center padded">Brak rekordów.</td></tr>';
      return;
    }

    tbody.innerHTML = "";
    data.forEach((row) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + row.id + "</td>" +
        "<td><strong>" + escapeHtml(row.code) + "</strong></td>" +
        "<td>" + row.value + " zł</td>" +
        "<td>" + (row.used ? '<span class="pill ok">Użyty</span>' : '<span class="pill">Nieużyty</span>') + "</td>" +
        "<td>" + (row.order_id ? escapeHtml(String(row.order_id)) : "—") + "</td>";
      tbody.appendChild(tr);
    });
  } catch (e) {
    console.error(e);
    tbody.innerHTML =
      '<tr><td colspan="5" class="muted center padded">Błąd przy pobieraniu danych.</td></tr>';
  }
}

async function loadLogs() {
  const tbody = document.getElementById("logs-tbody");
  tbody.innerHTML =
    '<tr><td colspan="5" class="muted center padded">Ładowanie logów...</td></tr>';

  try {
    const res = await fetch("/admin/api/logs");
    if (!res.ok) {
      tbody.innerHTML =
        '<tr><td colspan="5" class="muted center padded">Błąd przy pobieraniu logów.</td></tr>';
      return;
    }

    const data = await res.json();
    if (!Array.isArray(data) || data.length === 0) {
      tbody.innerHTML =
        '<tr><td colspan="5" class="muted center padded">Brak logów.</td></tr>';
      return;
    }

    tbody.innerHTML = "";
    data.forEach((row) => {
      const tr = document.createElement("tr");
      const statusClass = row.status === "processed"
        ? "ok"
        : row.status === "error"
          ? "err"
          : "";

      tr.innerHTML =
        "<td>" + escapeHtml(row.created_at || "—") + "</td>" +
        '<td><span class="pill ' + statusClass + '">' + escapeHtml(row.status || "—") + "</span></td>" +
        "<td>" + escapeHtml(row.order_id || "—") + "</td>" +
        "<td>" + escapeHtml(row.order_serial || "—") + "</td>" +
        "<td>" + escapeHtml(row.message || "—") + "</td>";

      tbody.appendChild(tr);
    });
  } catch (e) {
    console.error(e);
    tbody.innerHTML =
      '<tr><td colspan="5" class="muted center padded">Błąd przy pobieraniu logów.</td></tr>';
  }
}

async function manualLoad() {
  const out = manualResult;
  const orderSerialNumber = (document.getElementById("manual-order").value || "").trim();

  if (!orderSerialNumber) return;

  out.innerHTML = '<span class="pill">Ładowanie</span> Pobieram dane zamówienia...';

  try {
    const res = await fetch(
      "/admin/api/manual/order?orderSerialNumber=" + encodeURIComponent(orderSerialNumber)
    );
    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.detail || "Nie udało się pobrać danych");
    }

    manualLast = data;

    if (data.email) {
      document.getElementById("manual-email").value = data.email;
    }

    out.innerHTML =
      '<span class="pill ok">OK</span> ' +
      'Znaleziono <strong>' + (data.codes ? data.codes.length : 0) + '</strong> kod(y) dla zamówienia ' +
      '<strong>' + escapeHtml(orderSerialNumber) + '</strong>.';
  } catch (e) {
    out.innerHTML = '<span class="pill err">Błąd</span> ' + escapeHtml(e.message || String(e));
  }
}

function manualPdf() {
  const out = manualResult;
  const orderSerialNumber = (document.getElementById("manual-order").value || "").trim();

  if (!orderSerialNumber) return;

  out.innerHTML = '<span class="pill">PDF</span> Generuję plik...';

  const url = "/admin/api/manual/pdf?orderSerialNumber=" + encodeURIComponent(orderSerialNumber);
  window.location.href = url;

  setTimeout(() => {
    out.innerHTML =
      '<span class="pill ok">OK</span> PDF wygenerowany dla zamówienia <strong>' +
      escapeHtml(orderSerialNumber) +
      "</strong>.";
  }, 400);
}

async function manualSendEmail() {
  const out = manualResult;
  const orderSerialNumber = (document.getElementById("manual-order").value || "").trim();
  const email = (document.getElementById("manual-email").value || "").trim();
  const attachPdf = document.getElementById("manual-attach-pdf").checked;

  if (!orderSerialNumber || !email) return;

  out.innerHTML = '<span class="pill">E-mail</span> Wysyłam wiadomość...';

  try {
    const res = await fetch("/admin/api/manual/send-email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ orderSerialNumber, email, attachPdf })
    });

    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Błąd wysyłki");
    }

    out.innerHTML =
      '<span class="pill ok">Wysłano</span> Na: <strong>' +
      escapeHtml(data.sentTo) +
      '</strong> • PDF: <strong>' +
      (data.attachPdf ? "tak" : "nie") +
      "</strong>.";
  } catch (e) {
    out.innerHTML = '<span class="pill err">Błąd</span> ' + escapeHtml(e.message || String(e));
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

document.addEventListener("DOMContentLoaded", () => {
  textarea.addEventListener("input", updateSummary);

  document.getElementById("btn-save-codes").addEventListener("click", saveCodes);
  document.getElementById("btn-refresh-stats").addEventListener("click", loadStats);
  document.getElementById("btn-refresh-codes").addEventListener("click", loadCodes);
  document.getElementById("btn-refresh-logs").addEventListener("click", loadLogs);
  document.getElementById("btn-apply-filters").addEventListener("click", loadCodes);

  document.getElementById("btn-manual-load").addEventListener("click", manualLoad);
  document.getElementById("btn-manual-pdf").addEventListener("click", manualPdf);
  document.getElementById("btn-manual-email").addEventListener("click", manualSendEmail);

  updateSummary();
  loadStats();
  loadCodes();
  loadLogs();
});
