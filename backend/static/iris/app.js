/* Iris dashboard SPA.
 *
 * No build step, no framework. Vanilla JS with hash routing:
 *   #/             list view
 *   #/call/{id}    detail view
 *
 * The fetch() calls go to /iris/api/* (already auth-guarded by the
 * route layer — the browser's Basic Auth credentials are sent
 * automatically once the user has entered them).
 */
(function () {
  "use strict";

  // ----- utility ----------------------------------------------------

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "html") node.innerHTML = v;
        else if (k.startsWith("on") && typeof v === "function") {
          node.addEventListener(k.slice(2).toLowerCase(), v);
        } else {
          node.setAttribute(k, v);
        }
      }
    }
    for (const c of children) {
      if (c === null || c === undefined || c === false) continue;
      if (typeof c === "string" || typeof c === "number") {
        node.appendChild(document.createTextNode(String(c)));
      } else {
        node.appendChild(c);
      }
    }
    return node;
  }

  function fmtDuration(seconds) {
    if (!seconds || seconds <= 0) return "—";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return m > 0 ? `${m}m ${s.toString().padStart(2, "0")}s` : `${s}s`;
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    // Local time, short format. Show year only if not current year.
    const now = new Date();
    const opts = d.getFullYear() === now.getFullYear()
      ? { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
      : { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
    return d.toLocaleString(undefined, opts);
  }

  function fmtMoney(usd) {
    if (usd === null || usd === undefined) return "—";
    if (usd < 0.01) return "<$0.01";
    return "$" + usd.toFixed(2);
  }

  function fmtMoneyFine(usd) {
    if (usd === null || usd === undefined) return "—";
    return "$" + usd.toFixed(4);
  }

  function fmtPhone(p) {
    if (!p) return "—";
    // E.164 +1NXXNXXXXXX -> (NXX) NXX-XXXX for US numbers
    const m = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(p);
    if (m) return `(${m[1]}) ${m[2]}-${m[3]}`;
    return p;
  }

  function tagClass(tag) {
    if (tag.includes("completed") || tag === "card_captured") return "tag completed";
    if (tag.includes("failed") || tag === "silent_hangup") return "tag failed";
    if (tag.startsWith("transfer_")) return "tag transfer";
    return "tag";
  }

  async function fetchJSON(url, opts) {
    const resp = await fetch(url, opts);
    if (!resp.ok) {
      throw new Error(`${resp.status} ${resp.statusText}`);
    }
    return resp.json();
  }

  // ----- list view --------------------------------------------------

  async function renderList(root) {
    root.innerHTML = "";
    root.appendChild(el("div", { class: "loading" }, "Loading calls…"));
    let data;
    try {
      data = await fetchJSON("/iris/api/calls?limit=200");
    } catch (err) {
      root.innerHTML = "";
      root.appendChild(el("div", { class: "card" },
        el("p", null, "Could not load calls: " + err.message)));
      return;
    }
    const calls = data.calls || [];
    $("#call-count").textContent = `${calls.length} call${calls.length === 1 ? "" : "s"}`;

    root.innerHTML = "";
    if (calls.length === 0) {
      root.appendChild(el("div", { class: "card" },
        el("p", null, "No calls found in recordings dir.")));
      return;
    }

    const legend = el("div", { class: "list-legend muted" },
      el("span", null,
        el("span", { class: "has-flag summary" }), " summary cached  ",
        el("span", { class: "has-flag audio" }), " merged audio ready  ",
        el("span", { class: "has-flag off" }), " not yet generated",
      ),
    );
    root.appendChild(legend);

    const table = el("table", { class: "calls" },
      el("thead", null,
        el("tr", null,
          el("th", null, "Started"),
          el("th", null, "Caller"),
          el("th", { class: "duration" }, "Duration"),
          el("th", null, "Outcome"),
          el("th", null, "Summary"),
          el("th", { class: "cost" }, "Cost"),
          el("th", null, "Flags"),
        )
      ),
      el("tbody", null,
        ...calls.map(c =>
          el("tr", { onClick: () => { location.hash = `#/call/${c.call_id}`; } },
            el("td", { class: "time" }, fmtTime(c.started_at)),
            el("td", { class: "phone" }, fmtPhone(c.caller_phone)),
            el("td", { class: "duration" }, fmtDuration(c.duration_seconds)),
            el("td", null,
              c.outcome
                ? el("span", { class: tagClass(c.outcome) }, c.outcome)
                : el("span", { class: "muted" }, "—"),
            ),
            el("td", { class: "summary-cell" },
              c.summary_short
                ? c.summary_short
                : el("span", { class: "muted" }, c.has_summary ? "—" : "(rule-based)"),
            ),
            el("td", { class: "cost" },
              c.cost_total_usd && c.cost_total_usd > 0
                ? fmtMoney(c.cost_total_usd)
                : el("span", { class: "muted" }, "—"),
            ),
            el("td", null,
              el("span", {
                class: "has-flag " + (c.has_summary ? "summary" : "off"),
                title: c.has_summary ? "Summary cached" : "No summary yet (rule-based outcome only)",
              }),
              el("span", {
                class: "has-flag " + (c.has_merged_audio ? "audio" : "off"),
                title: c.has_merged_audio ? "Merged stereo OGG ready" : "Audio not merged yet",
              }),
            ),
          )
        )
      )
    );
    root.appendChild(table);
  }

  // ----- detail view ------------------------------------------------

  function renderTranscript(items) {
    // chat-style. Filter out empty messages and the blizzard frog warmup.
    const out = el("div", { class: "transcript" });
    for (const it of items) {
      if (it.type === "ChatMessage") {
        let content = it.content;
        if (Array.isArray(content)) content = content.join(" ");
        content = (content || "").toString().trim();
        if (!content) continue;
        if (content.toLowerCase() === "blizzard frog") continue;
        if (it.role === "assistant" && content.toLowerCase() === "hello") continue;
        const cls = "msg " + (it.role || "assistant") + (it.interrupted ? " interrupted" : "");
        out.appendChild(el("div", { class: cls },
          el("div", { class: "role" }, it.role || ""),
          el("div", null, content),
        ));
      } else if (it.type === "FunctionCall") {
        out.appendChild(el("div", { class: "msg tool" },
          `→ tool: ${it.name || "?"}`
        ));
      } else if (it.type === "FunctionCallOutput") {
        // Output preview, kept short.
        let preview = it.output;
        if (typeof preview === "object") preview = JSON.stringify(preview);
        preview = (preview || "").toString();
        if (preview.length > 120) preview = preview.slice(0, 117) + "…";
        out.appendChild(el("div", { class: "msg tool" },
          `← tool result: ${preview}`
        ));
      }
    }
    return out;
  }

  function renderCostCard(cost) {
    return el("div", { class: "card" },
      el("h2", null, "Cost"),
      el("table", { class: "cost-table" },
        el("tbody", null,
          el("tr", null,
            el("td", { class: "label" }, "LLM (Claude)"),
            el("td", { class: "amount" }, fmtMoneyFine(cost.llm_total_usd)),
          ),
          el("tr", null,
            el("td", { class: "label muted" }, "  cache write"),
            el("td", { class: "amount muted" }, fmtMoneyFine(cost.llm_cache_write_usd)),
          ),
          el("tr", null,
            el("td", { class: "label muted" }, "  cache read"),
            el("td", { class: "amount muted" }, fmtMoneyFine(cost.llm_cache_read_usd)),
          ),
          el("tr", null,
            el("td", { class: "label muted" }, "  uncached input"),
            el("td", { class: "amount muted" }, fmtMoneyFine(cost.llm_input_uncached_usd)),
          ),
          el("tr", null,
            el("td", { class: "label muted" }, "  output"),
            el("td", { class: "amount muted" }, fmtMoneyFine(cost.llm_output_usd)),
          ),
          el("tr", null,
            el("td", { class: "label" }, `STT (${cost.stt_seconds.toFixed(0)}s)`),
            el("td", { class: "amount" }, fmtMoneyFine(cost.stt_total_usd)),
          ),
          el("tr", null,
            el("td", { class: "label" }, `Twilio (${cost.twilio_minutes} min)`),
            el("td", { class: "amount" }, fmtMoneyFine(cost.twilio_minutes_usd)),
          ),
          cost.sms_count > 0 ? el("tr", null,
            el("td", { class: "label" }, `SMS (${cost.sms_count})`),
            el("td", { class: "amount" }, fmtMoneyFine(cost.sms_usd)),
          ) : null,
          el("tr", { class: "total" },
            el("td", { class: "label" }, "Total"),
            el("td", { class: "amount" }, fmtMoney(cost.total_usd)),
          ),
        )
      )
    );
  }

  function renderAudioCard(callId, tracks) {
    if (!tracks || tracks.length === 0) {
      return el("div", { class: "card" },
        el("h2", null, "Audio"),
        el("p", { class: "muted" }, "No recordings for this call."),
      );
    }
    // Track summary line: e.g. "Caller, Iris, Front Desk (3 tracks)"
    const labels = tracks.map(t => t.label).join(", ");
    const haveRoles = tracks.some(t => t.role !== "unknown");

    // Try merged stereo first; the per-track players below it always
    // get shown too so the user can isolate one side if they want.
    const audio = el("audio", { controls: true, preload: "metadata" });
    const swapBtn = el("button", { class: "secondary" }, "Swap L/R");
    const errEl = el("div", { class: "audio-error", style: "display:none" });

    let swap = 0;
    function loadMerged() {
      audio.src = `/iris/api/calls/${encodeURIComponent(callId)}/audio.ogg?swap=${swap}`;
    }

    audio.addEventListener("error", () => {
      errEl.textContent = "Merged audio failed; use the per-track players below.";
      errEl.style.display = "";
    });

    swapBtn.addEventListener("click", () => {
      swap = swap ? 0 : 1;
      loadMerged();
    });

    loadMerged();

    // Per-track players (always rendered, with proper labels)
    const perTrack = el("div", { class: "per-track" });
    tracks.forEach((t, i) => {
      const audioEl = el("audio", {
        controls: true,
        preload: "metadata",
        src: `/iris/api/calls/${encodeURIComponent(callId)}/track/${i}.ogg`,
        style: "width: 100%;",
      });
      perTrack.appendChild(el("div", { class: "track-row" },
        el("div", { class: "track-label" },
          el("strong", null, t.label),
          t.role !== "unknown"
            ? el("span", { class: "muted track-identity" }, ` (${t.role}${t.identity ? ", " + t.identity : ""})`)
            : (t.identity ? el("span", { class: "muted track-identity" }, ` (${t.identity})`) : null),
        ),
        audioEl,
      ));
    });

    const channelHint = haveRoles
      ? "Caller=Left, AI+Answerer=Right."
      : "Channel mapping is a guess (old recording format).";

    return el("div", { class: "card" },
      el("h2", null, `Audio · ${labels}`),
      el("div", { class: "audio-controls" },
        el("div", null,
          el("strong", null, "Merged stereo "),
          el("span", { class: "muted" }, channelHint),
          " ", swapBtn,
        ),
        audio,
        errEl,
        el("div", { class: "muted", style: "margin-top:12px;" }, "Individual tracks:"),
        perTrack,
      ),
    );
  }

  function renderCategoriesCard(categories) {
    return el("div", { class: "card" },
      el("h2", null, "Categories"),
      categories.length === 0
        ? el("span", { class: "muted" }, "(none)")
        : el("div", null,
          ...categories.map(t => el("span", { class: tagClass(t) }, t)),
        ),
    );
  }

  function renderSummaryCard(callId, summary, onRegen, regenInFlight) {
    const card = el("div", { class: "card summary-block" });
    const header = el("h2", null, "Summary",
      el("button", {
        class: "secondary",
        style: "float:right;",
        disabled: regenInFlight,
        onClick: onRegen,
      }, regenInFlight ? "Generating…" : (summary ? "Regenerate" : "Generate")),
    );
    card.appendChild(header);
    if (!summary) {
      card.appendChild(el("p", { class: "muted" }, "No summary yet. Click Generate."));
      return card;
    }
    card.appendChild(el("p", { class: "summary-text" }, summary.summary || "(empty)"));
    if (summary.outcome) {
      card.appendChild(el("p", null,
        el("strong", null, "Outcome: "),
        el("span", { class: tagClass(summary.outcome) }, summary.outcome),
      ));
    }
    if (summary.issues_observed && summary.issues_observed.length > 0) {
      card.appendChild(el("strong", null, "Issues noted by reviewer:"));
      card.appendChild(el("ul", { class: "issues" },
        ...summary.issues_observed.map(i => el("li", null, i)),
      ));
    }
    if (summary.attached_to_reservations && summary.attached_to_reservations.length > 0) {
      card.appendChild(el("p", { class: "summary-meta" },
        el("strong", null, "Posted as Cloudbeds note on: "),
        summary.attached_to_reservations.join(", "),
      ));
    }
    if (summary.generated_at) {
      card.appendChild(el("div", { class: "summary-meta" },
        `Generated ${fmtTime(summary.generated_at)} (${summary.generator_version || "?"})`,
      ));
    }
    return card;
  }

  async function renderDetail(root, callId) {
    root.innerHTML = "";
    root.appendChild(el("div", { class: "loading" }, "Loading call…"));
    let data;
    try {
      data = await fetchJSON(`/iris/api/calls/${encodeURIComponent(callId)}`);
    } catch (err) {
      root.innerHTML = "";
      root.appendChild(el("div", { class: "card" },
        el("a", { class: "detail-back", href: "#/" }, "← Back to list"),
        el("p", null, "Could not load call: " + err.message)));
      return;
    }
    $("#call-count").textContent = `${fmtPhone(data.caller_phone)} · ${fmtTime(data.started_at)}`;

    let regenInFlight = false;
    let currentSummary = data.summary;

    async function onRegen() {
      regenInFlight = true;
      rerender();
      try {
        const r = await fetchJSON(
          `/iris/api/calls/${encodeURIComponent(callId)}/regen`,
          { method: "POST" },
        );
        currentSummary = r.summary;
      } catch (err) {
        alert("Regen failed: " + err.message);
      }
      regenInFlight = false;
      rerender();
    }

    function rerender() {
      root.innerHTML = "";
      root.appendChild(el("a", { class: "detail-back", href: "#/" }, "← Back to list"));

      const left = el("div", null,
        renderSummaryCard(callId, currentSummary, onRegen, regenInFlight),
        renderAudioCard(callId, data.tracks || []),
        el("div", { class: "card" },
          el("h2", null, "Transcript"),
          renderTranscript(data.items || []),
        ),
        el("details", null,
          el("summary", null, "Raw events (debug)"),
          el("pre", null, JSON.stringify(data.events, null, 2)),
        ),
      );
      const right = el("div", null,
        renderCategoriesCard(data.categories || []),
        renderCostCard(data.cost || {}),
        el("div", { class: "card" },
          el("h2", null, "Stats"),
          el("table", { class: "cost-table" }, el("tbody", null,
            el("tr", null,
              el("td", { class: "label" }, "Duration"),
              el("td", { class: "amount" }, fmtDuration(data.duration_seconds)),
            ),
            el("tr", null,
              el("td", { class: "label" }, "Chat items"),
              el("td", { class: "amount" }, data.item_count),
            ),
            el("tr", null,
              el("td", { class: "label" }, "Events logged"),
              el("td", { class: "amount" }, data.event_count),
            ),
            data.tts_cache_stats ? el("tr", null,
              el("td", { class: "label" }, "TTS cache hits"),
              el("td", { class: "amount" },
                `${data.tts_cache_stats.hits}/${data.tts_cache_stats.hits + data.tts_cache_stats.misses}`),
            ) : null,
          )),
        ),
      );
      const grid = el("div", { class: "detail-grid" }, left, right);
      root.appendChild(grid);
    }
    rerender();
  }

  // ----- routing ----------------------------------------------------

  function route() {
    const root = $("#root");
    const hash = location.hash || "#/";
    const callMatch = hash.match(/^#\/call\/(.+)$/);
    if (callMatch) {
      renderDetail(root, decodeURIComponent(callMatch[1]));
    } else {
      renderList(root);
    }
  }

  // ----- password change ------------------------------------------

  async function openPasswordModal() {
    const modal = $("#password-modal");
    const title = $("#password-modal-title");
    const msg = $("#password-message");

    // Reset fields each time so a previous attempt's values don't linger.
    $("#current-password").value = "";
    $("#new-password").value = "";
    $("#confirm-password").value = "";
    msg.textContent = "";
    msg.className = "modal-message";

    // Fetch auth status to set the title (set vs change) and the min length.
    try {
      const status = await fetchJSON("/iris/api/auth/status");
      title.textContent = status.custom_password_set
        ? "Change password"
        : "Set custom password";
      $("#min-pw-len").textContent = status.min_password_length;
    } catch (err) {
      // If we can't reach the status endpoint (e.g. logged out), don't
      // open the modal at all -- something else is wrong.
      alert("Could not load auth status: " + err.message);
      return;
    }

    modal.style.display = "";
    $("#current-password").focus();
  }

  function closePasswordModal() {
    $("#password-modal").style.display = "none";
  }

  async function submitPasswordChange(ev) {
    ev.preventDefault();
    const msg = $("#password-message");
    const submitBtn = $("#password-submit");
    const current = $("#current-password").value;
    const next = $("#new-password").value;
    const confirm = $("#confirm-password").value;

    if (next !== confirm) {
      msg.textContent = "New password and confirmation don't match.";
      msg.className = "modal-message error";
      return;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = "Updating…";
    msg.textContent = "";
    msg.className = "modal-message";

    try {
      const resp = await fetch("/iris/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: current,
          new_password: next,
          confirm_password: confirm,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const detail = data.detail || `${resp.status} ${resp.statusText}`;
        msg.textContent = "Failed: " + detail;
        msg.className = "modal-message error";
      } else {
        msg.textContent = data.message || "Password updated.";
        msg.className = "modal-message success";
        // Don't auto-close; let the user read the "you'll be logged out"
        // hint and decide when to reload.
      }
    } catch (err) {
      msg.textContent = "Network error: " + err.message;
      msg.className = "modal-message error";
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Update password";
    }
  }

  // ----- wiring ----------------------------------------------------

  window.addEventListener("hashchange", route);
  window.addEventListener("DOMContentLoaded", () => {
    $("#refresh-btn").addEventListener("click", route);
    $("#change-password-btn").addEventListener("click", openPasswordModal);
    $("#password-cancel").addEventListener("click", closePasswordModal);
    $("#password-form").addEventListener("submit", submitPasswordChange);
    // Click outside the modal box to dismiss.
    $("#password-modal").addEventListener("click", (e) => {
      if (e.target.id === "password-modal") closePasswordModal();
    });
    route();
  });
})();
