const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let lastMarkdown = "";
let lastResult = null;

let masterEnabled = false;
fetch("/api/health").then((r) => r.json()).then((h) => {
  $("provider").textContent = `LLM: ${h.provider}`;
  masterEnabled = !!h.master_cv;
  $("useMaster").hidden = $("saveMaster").hidden = !masterEnabled;
  if (h.provider === "mock") $("provider").title = "Rule-based demo mode. Set LLM_PROVIDER and an API key for full results.";
}).catch(() => ($("provider").textContent = "offline"));

function showError(msg) { $("error").textContent = msg; $("error").hidden = !msg; }

$("sample").onclick = async () => {
  const [cv, jd] = await Promise.all([fetch("/static/samples/sample_cv.txt"), fetch("/static/samples/sample_jd.txt")]);
  $("cv").value = await cv.text();
  $("jd").value = await jd.text();
};

$("cvFile").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  showError("");
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/extract", { method: "POST", body: fd });
  const data = await r.json();
  if (!r.ok) return showError(data.detail || "Could not read file");
  $("cv").value = data.text;
  e.target.value = "";
};

let stepTimer;
function startSteps() {
  const items = [...$("steps").children];
  let i = 0;
  items.forEach((li) => li.classList.remove("on"));
  items[0].classList.add("on");
  stepTimer = setInterval(() => {
    if (i < items.length - 1) { items[i].classList.remove("on"); items[++i].classList.add("on"); }
  }, 4000);
}

$("go").onclick = async () => {
  const cv_text = $("cv").value.trim(), jd_text = $("jd").value.trim();
  if (cv_text.length < 50 || jd_text.length < 50) return showError("Please paste both your CV and the job description.");
  showError("");
  $("go").disabled = true;
  $("results").hidden = true;
  $("loading").hidden = false;
  startSteps();
  try {
    const r = await fetch("/api/analyze", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cv_text, jd_text }) });
    const data = await r.json();
    if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Request failed");
    render(data);
  } catch (err) {
    showError(err.message);
  } finally {
    clearInterval(stepTimer);
    $("loading").hidden = true;
    $("go").disabled = false;
  }
};

function render(d) {
  $("before").textContent = Math.round(d.ats_before.score);
  $("after").textContent = Math.round(d.ats_after.score);
  const hitsOf = (a) => [...a.must_have_hit, ...a.nice_to_have_hit];
  const beforeHits = new Set(hitsOf(d.ats_before));
  const gained = hitsOf(d.ats_after).filter((k) => !beforeHits.has(k));
  const missing = [...d.ats_after.must_have_missing, ...d.ats_after.nice_to_have_missing];
  $("gained").innerHTML = gained.map((k) => `<span class="chip hit">+ ${esc(k)}</span>`).join("") ||
    "<span class='muted small'>None: your CV already used the employer's wording for every skill it supports.</span>";
  $("missing").innerHTML = missing.map((k) => `<span class="chip miss ${masterEnabled ? "can" : ""}" data-skill="${esc(k)}">${esc(k)}</span>`).join("") || "<span class='muted small'>None</span>";
  $("haveHint").hidden = !masterEnabled || missing.length === 0;
  $("evForm").hidden = $("evDone").hidden = true;
  if (masterEnabled) [...$("missing").querySelectorAll(".chip.can")].forEach((c) => (c.onclick = () => openEvidence(c)));
  $("role").textContent = `· ${d.job.title}`;
  $("fit").textContent = d.gaps.fit_summary;
  $("strengths").innerHTML = d.gaps.transferable_strengths.map((s) => `<li>${esc(s)}</li>`).join("") || "<li class='muted'>None listed</li>";
  $("gaps").innerHTML = d.gaps.honest_gaps.map((s) => `<li>${esc(s)}</li>`).join("") || "<li class='muted'>No major gaps</li>";

  $("skills").innerHTML = d.gaps.skills.map((s) =>
    `<tr><td>${esc(s.skill)}</td><td><span class="pill ${s.status}">${s.status}</span></td><td class="ev">${esc(s.evidence || "–")}</td></tr>`).join("");

  renderReview(d);
  $("skipped").hidden = !d.unconfirmed_lines_skipped;
  $("skipped").textContent = `Skipped ${d.unconfirmed_lines_skipped} CV line(s) marked [CONFIRM] / [VERIFY]: confirm them in your master CV to use them.`;

  const flags = d.fact_flags;
  $("flagsCard").hidden = flags.length === 0;
  $("flags").innerHTML = flags.map((f) =>
    `<li>${f.bullet_index >= 0 ? `Bullet ${f.bullet_index + 1}: ` : f.bullet_index === -1 ? "Skills list: " : "Headline/summary: "}<code>${esc(f.detail)}</code></li>`).join("");

  $("headline").textContent = d.tailored.headline;
  $("summary").textContent = d.tailored.summary;
  const hits = new Set([...d.ats_after.must_have_hit, ...d.ats_after.nice_to_have_hit].map((s) => s.toLowerCase()));
  $("skillChips").innerHTML = d.tailored.skills_ordered.map((s) => `<span class="chip ${hits.has(s.toLowerCase()) ? "hit" : ""}">${esc(s)}</span>`).join("");

  $("bullets").innerHTML = d.tailored.bullets.map((b) => {
    const text = esc(b.rewritten).replace(/\[([^\]]+)\]/g, '<span class="ph">[$1]</span>');
    const tags = b.targets.map((t) => `<span class="tag">#${esc(t)}</span>`).join("");
    return `<li>${text}<span class="tags">${tags}</span><div class="orig">Original: ${esc(b.original)}</div></li>`;
  }).join("");
  [...$("bullets").children].forEach((li) => (li.onclick = () => li.classList.toggle("open")));

  lastMarkdown = d.tailored_markdown;
  lastResult = d;
  $("exportReport").hidden = true;
  $("results").hidden = false;
  $("results").scrollIntoView({ behavior: "smooth" });
}

const REVISION_TEXT = {
  accepted: "Revised after review",
  not_needed: "The reviewer found nothing to fix",
  rejected: "Kept the first draft",
  failed: "Review unavailable",
};

function renderReview(d) {
  const r = d.review;
  $("reviewCard").hidden = !r && d.revision !== "failed";
  if ($("reviewCard").hidden) return;
  $("revScore").textContent = r ? `· ${r.score}/10 on the first draft` : "";
  $("revNote").textContent = d.revision_note || REVISION_TEXT[d.revision] || "";
  $("revNote").classList.toggle("warn", d.revision === "rejected" || d.revision === "failed");
  $("revStrengths").innerHTML = (r?.strengths || []).map((s) => `<li>${esc(s)}</li>`).join("") || "<li class='muted'>–</li>";
  $("revIssues").innerHTML = (r?.issues || []).map((i) =>
    `<li><span class="kind">${esc(i.kind.replace("_", " "))}</span><b>${esc(i.location)}:</b> ${esc(i.problem)}<span class="fix">Fix: ${esc(i.fix)}</span></li>`
  ).join("") || "<li class='muted'>None</li>";
}

$("copy").onclick = async () => {
  await navigator.clipboard.writeText(lastMarkdown);
  $("copy").textContent = "Copied ✓";
  setTimeout(() => ($("copy").textContent = "Copy markdown"), 1500);
};

$("dl").onclick = () => {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lastMarkdown], { type: "text/markdown" }));
  a.download = "tailored-cv.md";
  a.click();
  URL.revokeObjectURL(a.href);
};

// ---------- master CV ----------
$("useMaster").onclick = async () => {
  const d = await (await fetch("/api/master")).json();
  if (!d.exists) return showError("No master CV yet. Paste or upload your CV, then click 'Save as master CV'.");
  showError("");
  $("cv").value = d.text;
};

$("saveMaster").onclick = async () => {
  const text = $("cv").value.trim();
  if (text.length < 50) return showError("Paste your CV first.");
  if (!confirm("Replace your saved master CV with the text in the CV box?")) return;
  const r = await fetch("/api/master", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
  const d = await r.json();
  if (!r.ok) return showError(d.detail || "Could not save");
  showError("");
  $("saveMaster").textContent = "Saved ✓";
  setTimeout(() => ($("saveMaster").textContent = "Save as master CV"), 1500);
};

let evChip = null;
function openEvidence(chip) {
  evChip = chip;
  $("evSkill").textContent = chip.dataset.skill;
  $("evText").value = "";
  $("evDone").hidden = true;
  $("evForm").hidden = false;
  $("evText").focus();
}
$("evCancel").onclick = () => ($("evForm").hidden = true);
$("evForm").onsubmit = async (e) => {
  e.preventDefault();
  const skill = evChip.dataset.skill, evidence = $("evText").value.trim();
  if (evidence.length < 10) return showError("Add a short sentence describing where you used it.");
  const r = await fetch("/api/master/evidence", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ skill, evidence }) });
  const d = await r.json();
  if (!r.ok) return showError(typeof d.detail === "string" ? d.detail : "Could not save");
  showError("");
  $("cv").value = d.text;
  evChip.classList.remove("can");
  evChip.classList.add("added");
  evChip.onclick = null;
  $("evForm").hidden = true;
  $("evDone").textContent = `Added "${skill}" to your master CV and loaded it into the CV box. Click "Tailor my CV" again to update the score.`;
  $("evDone").hidden = false;
};

// ---------- finished CV export ----------
const CHECK_LABEL = { name_readable: "name", email_readable: "email", phone_readable: "phone",
  sections_in_order: "section order", no_placeholders: "no placeholders" };

async function exportCV(format) {
  if (!lastResult) return;
  const btn = format === "pdf" ? $("dlPdf") : $("dlDocx");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Building…";
  try {
    const r = await fetch("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cv: lastResult.cv, job: lastResult.job, tailored: lastResult.tailored,
        format, pages: Number($("pages").value), style: $("cvStyle").value }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "Export failed");
    const name = /filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") || "")?.[1] || `cv.${format}`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
    showReport(JSON.parse(r.headers.get("X-Export-Report") || "{}"), name);
    showError("");
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

function showReport(rep, name) {
  const checks = Object.entries(rep.text_checks || {});
  const passed = checks.filter(([, ok]) => ok).map(([k]) => CHECK_LABEL[k] || k);
  const warn = (rep.warnings || []).length > 0;
  $("exportReport").classList.toggle("warn", warn);
  $("exportReport").innerHTML =
    `<b>${esc(name)}</b>: ${rep.pages} page(s), ${rep.bullets_kept} bullets` +
    (rep.bullets_added ? ` (${rep.bullets_added} added from your master CV to fill the page)` : "") +
    (rep.ats_score != null ? `, keyword match in the file ${Math.round(rep.ats_score)}` : "") +
    `. An ATS can read: ${esc(passed.join(", ") || "–")}.` +
    (warn ? `<ul>${rep.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "");
  $("exportReport").hidden = false;
}

// remember the last style and page choice
try {
  for (const id of ["cvStyle", "pages"]) {
    const v = localStorage.getItem(id);
    if (v) $(id).value = v;
    $(id).onchange = () => localStorage.setItem(id, $(id).value);
  }
} catch { /* storage unavailable: defaults are fine */ }

$("dlPdf").onclick = () => exportCV("pdf");
$("dlDocx").onclick = () => exportCV("docx");
