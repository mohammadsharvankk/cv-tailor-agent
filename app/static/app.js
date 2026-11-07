const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let lastMarkdown = "";

fetch("/api/health").then((r) => r.json()).then((h) => {
  $("provider").textContent = `LLM: ${h.provider}`;
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
  $("missing").innerHTML = missing.map((k) => `<span class="chip miss">${esc(k)}</span>`).join("") || "<span class='muted small'>None</span>";
  $("role").textContent = `· ${d.job.title}`;
  $("fit").textContent = d.gaps.fit_summary;
  $("strengths").innerHTML = d.gaps.transferable_strengths.map((s) => `<li>${esc(s)}</li>`).join("") || "<li class='muted'>None listed</li>";
  $("gaps").innerHTML = d.gaps.honest_gaps.map((s) => `<li>${esc(s)}</li>`).join("") || "<li class='muted'>No major gaps</li>";

  $("skills").innerHTML = d.gaps.skills.map((s) =>
    `<tr><td>${esc(s.skill)}</td><td><span class="pill ${s.status}">${s.status}</span></td><td class="ev">${esc(s.evidence || "–")}</td></tr>`).join("");

  const flags = d.fact_flags;
  $("flagsCard").hidden = flags.length === 0;
  $("flags").innerHTML = flags.map((f) =>
    `<li>${f.bullet_index >= 0 ? `Bullet ${f.bullet_index + 1}: ` : "Skills list: "}<code>${esc(f.detail)}</code></li>`).join("");

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
  $("results").hidden = false;
  $("results").scrollIntoView({ behavior: "smooth" });
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
