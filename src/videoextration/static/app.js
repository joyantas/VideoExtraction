"use strict";

const byId = (id) => document.getElementById(id);
let bearer = "";
let pollController = null;

byId("token").addEventListener("input", (event) => { bearer = event.target.value.trim(); });
byId("forget").addEventListener("click", () => {
  bearer = "";
  byId("token").value = "";
  if (pollController) pollController.abort();
  byId("status").textContent = "Access token forgotten. Enter it again to read a job.";
});

async function api(path, options = {}) {
  const headers = { Authorization: `Bearer ${bearer}`, ...(options.headers || {}) };
  const response = await fetch(path, { ...options, headers, credentials: "omit", cache: "no-store" });
  let payload;
  try { payload = await response.json(); }
  catch { throw new Error(`API returned invalid JSON (HTTP ${response.status}).`); }
  if (!response.ok) {
    const detail = typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return payload;
}

function renderReport(report) {
  byId("report-section").hidden = false;
  byId("summary").textContent = `${report.overall_state || "needs-review"} · ${
    report.ready ? "Ready under configured illustrative checks only" : "NOT READY — incomplete, failed or unverified checks remain"
  } · Generated ${report.generated_at || "unknown"}`;
  byId("findings").replaceChildren();
  for (const finding of report.findings || []) {
    const li = document.createElement("li");
    const badge = document.createElement("span");
    const state = finding.state || "not-evaluated";
    badge.className = `badge ${["pass", "fail", "needs-review", "not-evaluated"].includes(state) ? state : "not-evaluated"}`;
    badge.textContent = state;
    li.append(badge, document.createTextNode(`${finding.rule_id || finding.id}: ${finding.conclusion || finding.detail || ""}`));
    byId("findings").append(li);
  }
  byId("evidence").textContent = JSON.stringify({
    profile: report.profile, profile_definition: report.profile_definition, rules_version: report.rules_version,
    coverage: report.coverage, required_text: report.required_text, transcript: report.transcript,
    evidence: report.evidence,
    disclaimer: report.disclaimer, error_code: report.error_code,
  }, null, 2);
}

async function follow(jobId) {
  if (pollController) pollController.abort();
  pollController = new AbortController();
  const signal = pollController.signal;
  const path = `/api/jobs/${encodeURIComponent(jobId)}`;
  for (let attempt = 0; attempt < 300; attempt += 1) {
    if (signal.aborted) return;
    const job = await api(path, { signal });
    const state = job.status || job.state;
    byId("status").textContent = `Job ${jobId} · ${state}`;
    if (state === "completed" || state === "failed") {
      try { renderReport(await api(`${path}/report`, { signal })); }
      catch (error) {
        if (state === "failed") throw new Error(job.error || "Job failed before a report was available. Create a new job to retry.");
        throw error;
      }
      return;
    }
    if (state === "rejected") throw new Error(job.error || "The uploaded object was rejected.");
    if (state === "queue-pending") {
      byId("status").textContent += " · Retrying persisted queue delivery…";
      await api(`${path}/complete`, { method: "POST", signal });
    }
    if (state === "awaiting-upload" || state === "uploading" || state === "uploaded") {
      byId("status").textContent += " · Upload is not finalized; this browser cannot replay a consumed upload capability.";
      return;
    }
    await new Promise((resolve) => {
      const abort = () => { clearTimeout(timer); resolve(); };
      const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, 2000);
      signal.addEventListener("abort", abort, { once: true });
    });
  }
  byId("status").textContent = `Job ${jobId}: polling paused after 10 minutes. Use “Check status” to resume.`;
}

byId("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  bearer = byId("token").value.trim();
  byId("submit").disabled = true;
  byId("report-section").hidden = true;
  try {
    const file = byId("video").files[0];
    if (!file || file.size === 0) throw new Error("Choose a non-empty video.");
    const extensionTypes = { mp4: "video/mp4", mov: "video/quicktime", webm: "video/webm", mkv: "video/x-matroska", avi: "video/x-msvideo" };
    const contentType = file.type || extensionTypes[file.name.split(".").pop().toLowerCase()];
    if (!contentType) throw new Error("Choose an MP4, MOV, WebM, MKV or AVI file.");
    const requiredText = byId("required-text").value.split("\n").map((text) => text.trim()).filter(Boolean);
    byId("status").textContent = "Creating upload…";
    const job = await api("/api/jobs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, content_type: contentType, size_bytes: file.size, required_text: requiredText, profile: "illustrative-portrait-v1" }),
    });
    byId("job-id").value = job.id;
    byId("status").textContent = `Uploading · Save this job ID: ${job.id}`;
    const uploadURL = new URL(job.upload_url, window.location.origin);
    if (uploadURL.origin !== window.location.origin && uploadURL.protocol !== "https:") {
      throw new Error("Cross-origin uploads require HTTPS.");
    }
    // Upload credentials are capability-scoped; never attach the API bearer to a Blob URL.
    const uploaded = await fetch(uploadURL, {
      method: "PUT", headers: job.upload_headers, body: file,
      credentials: "omit", redirect: "error",
    });
    if (!uploaded.ok) throw new Error(`Upload rejected (HTTP ${uploaded.status}); create a new job to retry.`);
    await api(`/api/jobs/${encodeURIComponent(job.id)}/complete`, { method: "POST" });
    await follow(job.id);
  } catch (error) {
    if (error.name !== "AbortError") byId("status").textContent = error.message;
  } finally { byId("submit").disabled = false; }
});

byId("resume-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  bearer = byId("token").value.trim();
  try { await follow(byId("job-id").value.trim()); }
  catch (error) { if (error.name !== "AbortError") byId("status").textContent = error.message; }
});
