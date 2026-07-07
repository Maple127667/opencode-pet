// opencode-pet — TUI plugin entry
//
// Forwards opencode session activity to the desktop pet overlay via stdin JSON.
//
// Architecture: TUI plugins receive events via api.event.on() (TUI-local bus).
// SSE streams (api.client.event.subscribe) open but never deliver to plugins.
// Supplementary client.session.status() polling catches busy/idle transitions.
//
// stdin protocol to pet.py:
//   {"type":"status","value":"idle|busy"}
//   {"type":"activity","value":"idle|thinking|speaking|tool"}
//   {"type":"flash","value":"success|fail|celebrate","duration":1500}
//   {"type":"alert","text":"..."}
//   {"type":"clear_alert"}
//   {"type":"bubble","text":"...","duration":3000}
//   {"type":"clear_bubble"}
//   {"type":"quit"}

import { appendFileSync } from "node:fs";

const PET_DIR = "D:\\Desktop\\code\\opencode-pet";
const PET_PY  = `${PET_DIR}\\pet.py`;
const LOG     = `${PET_DIR}\\probe.log`;

function log(kind, msg) {
  try { appendFileSync(LOG, `${new Date().toISOString()} [${kind}] ${msg}\n`); } catch {}
}

function findPython() {
  for (const c of ["pythonw", "pythonw.exe", "python", "py"]) {
    try {
      const path = globalThis.Bun.which(c);
      if (path) return { cmd: c, path };
    } catch {}
  }
  return null;
}

const tui = async (api) => {
  log("info", "opencode-pet tui() called");

  const py = findPython();
  if (!py) { log("error", "no python"); return; }

  const argv = py.cmd === "py" ? [py.cmd, "-3", PET_PY] : [py.cmd, PET_PY];

  let proc;
  try {
    proc = globalThis.Bun.spawn({
      cmd: argv, cwd: PET_DIR,
      stdout: "pipe", stderr: "pipe", stdin: "pipe",
    });
  } catch (e) { log("error", `spawn failed: ${e?.message}`); return; }
  log("success", `pet spawned pid=${proc.pid}`);

  // drain stderr to log
  (async () => {
    try {
      const r = proc.stderr.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await r.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i); buf = buf.slice(i + 1);
          if (line.trim()) log("pet", line);
        }
      }
    } catch {}
  })();

  const send = (obj) => {
    try {
      proc.stdin.write(JSON.stringify(obj) + "\n");
      try { proc.stdin.flush(); } catch {}
    } catch (e) { log("warn", `send failed: ${e?.message}`); }
  };

  send({ type: "status", value: "idle" });

  // ---- state helpers --------------------------------------------------
  let totalCost = 0;
  let totalTokens = { input: 0, output: 0, reasoning: 0 };
  try {
    totalCost = Number(api.kv.get("pet_total_cost", 0)) || 0;
    const t = api.kv.get("pet_total_tokens");
    if (t && typeof t === "object") totalTokens = t;
  } catch {}
  const saveTotals = () => {
    try {
      api.kv.set("pet_total_cost", totalCost);
      api.kv.set("pet_total_tokens", totalTokens);
    } catch {}
  };

  let busyTimer = null;
  let stallTimer = null;         // safety net: 60s no part → force activity idle
  let currentActivity = "idle";  // "idle" | "thinking" | "speaking" | "tool"
  let lastPollStatus = "idle";
  let pendingIdle = false;       // status:idle held back while activity != "idle"

  const STALL_TIMEOUT_MS = 60000;  // only a safety net, not the main timer

  // Map part.type → activity label
  const PART_TYPE_TO_ACTIVITY = {
    reasoning: "thinking",
    text:      "speaking",
    tool:      "tool",
  };

  // Centralized status sender.
  // While activity != "idle", idle is held back so the pet doesn't flicker
  // to waiting.gif during long reasoning/text pauses.
  const sendStatus = (v) => {
    if (v === "idle" && currentActivity !== "idle") {
      pendingIdle = true;
      return;
    }
    send({ type: "status", value: v });
  };

  const poke = (ms = 2500) => {
    if (busyTimer) clearTimeout(busyTimer);
    busyTimer = setTimeout(() => {
      sendStatus("idle");
      busyTimer = null;
    }, ms);
  };

  // Set activity channel. When returning to idle, flush any held-back status.
  const setActivity = (v) => {
    if (v === currentActivity) return;
    currentActivity = v;
    send({ type: "activity", value: v });
    if (v === "idle" && pendingIdle) {
      pendingIdle = false;
      sendStatus("idle");
    }
  };

  // ---- TUI-local event bus (the only working channel for plugins) ----
  const seen = new Set();
  const on = (name, handler) => {
    try {
      api.event.on(name, (e) => {
        if (!seen.has(name)) { seen.add(name); log("info", `event: ${name}`); }
        try { handler(e); } catch (err) { log("warn", `${name}: ${err?.message}`); }
      });
    } catch (err) { log("warn", `on(${name}) failed: ${err?.message}`); }
  };

  // =====================================================================
  // Session lifecycle — busy/idle drives the pet's base state
  // =====================================================================
  on("session.status", (e) => {
    const status = e?.properties?.status;
    const type = status?.type;
    log("info", `session.status.type=${type}`);
    if (type === "busy") {
      sendStatus("busy");
      poke(10000);
    } else if (type === "idle") {
      sendStatus("idle");
      if (busyTimer) { clearTimeout(busyTimer); busyTimer = null; }
    } else if (type === "retry") {
      send({ type: "flash", value: "fail", duration: 1200 });
      send({ type: "bubble", text: `🔁 retry #${status.attempt || 1}`, duration: 2000 });
    }
  });

  on("session.idle", () => {
    sendStatus("idle");
    if (busyTimer) { clearTimeout(busyTimer); busyTimer = null; }
  });

  on("session.error", () => {
    send({ type: "flash", value: "fail", duration: 2000 });
  });

  // =====================================================================
  // Polling fallback — sample session.status() every 500ms to catch
  // busy transitions the TUI bus might miss or deliver late.
  // =====================================================================
  const pollInterval = setInterval(async () => {
    try {
      if (typeof api.client?.session?.status !== "function") return;
      const result = await api.client.session.status();
      if (!result || typeof result !== "object") return;
      let anyBusy = false;
      for (const sid of Object.keys(result)) {
        const st = result[sid];
        if (st && st.type === "busy") { anyBusy = true; break; }
      }
      const now = anyBusy ? "busy" : "idle";
      if (now !== lastPollStatus) {
        lastPollStatus = now;
        log("info", `poll status → ${now}`);
        sendStatus(now);
        if (now === "idle" && busyTimer) { clearTimeout(busyTimer); busyTimer = null; }
      }
    } catch (e) {
      // silent — polling is best-effort
    }
  }, 500);

  // =====================================================================
  // T1.1 — ATTENTION REMINDERS (permission/question)
  // =====================================================================
  on("permission.asked", (e) => {
    const p = e?.properties || {};
    const patterns = (p.patterns || []).slice(0, 2).join(", ");
    send({ type: "alert", text: patterns ? `🔐 ${p.permission}: ${patterns}` : `🔐 ${p.permission || "permission"}` });
  });
  on("question.asked", (e) => {
    const q = e?.properties?.questions?.[0];
    const text = q?.message || q?.header || "agent asks";
    send({ type: "alert", text: `❓ ${String(text).slice(0, 50)}` });
  });
  ["permission.replied", "question.replied", "question.rejected"].forEach((n) => {
    on(n, () => send({ type: "clear_alert" }));
  });

  // =====================================================================
  // T2.5 — TODO PROGRESS
  // =====================================================================
  let lastTodoKey = "";
  on("todo.updated", (e) => {
    const todos = e?.properties?.todos || [];
    if (!todos.length) return;
    const done = todos.filter((t) => t.status === "completed").length;
    const total = todos.length;
    const key = `${done}/${total}`;
    if (key === lastTodoKey) return;
    lastTodoKey = key;
    if (done === total) {
      send({ type: "flash", value: "celebrate", duration: 2000 });
        send({ type: "bubble", text: `🎉 ${done}/${total} all done!`, duration: 10000 });
      } else {
        send({ type: "bubble", text: `📋 ${done}/${total}`, duration: 10000 });
    }
  });

  // =====================================================================
  // Activity detection — message.part.updated fires on every token/part delta
  // Classify by part.type:
  //   reasoning → thinking (review.gif)
  //   text      → speaking (running.gif)
  //   tool      → tool     (running.gif)
  // step-start/step-finish/file/snapshot/patch/agent/retry/compaction → ignored
  //
  // CRITICAL: activity does NOT auto-reset on a short timer. Long reasoning
  // or word-selection pauses are normal. Activity only returns to idle when:
  //   (a) message.updated fires with assistant.time.completed, OR
  //   (b) 60s stall safety-net triggers (agent truly stuck)
  // =====================================================================
  on("message.part.updated", (e) => {
    const part = e?.properties?.part || {};
    const activity = PART_TYPE_TO_ACTIVITY[part.type];
    if (!activity) return;  // untracked part type
    if (currentActivity !== activity) setActivity(activity);
    // reset stall safety-net — 60s of total silence means something is wrong
    if (stallTimer) clearTimeout(stallTimer);
    stallTimer = setTimeout(() => {
      log("warn", "stall: 60s no part.updated, forcing activity idle");
      setActivity("idle");
      stallTimer = null;
    }, STALL_TIMEOUT_MS);
  });

  // =====================================================================
  // Message completion — fires celebrate flash + clears activity channel
  // This is the ONLY place celebrate is triggered, so it never gets lost
  // due to status-hold or timer races.
  // =====================================================================
  on("message.updated", (e) => {
    const msg = e?.properties?.info || e?.properties;
    if (!msg) return;
    if (msg.role === "assistant" && msg.time?.completed) {
      // clear stall timer + activity channel
      if (stallTimer) { clearTimeout(stallTimer); stallTimer = null; }
      setActivity("idle");
      // celebrate flash — fires HERE, unconditionally on assistant completion
      send({ type: "flash", value: "celebrate", duration: 5000 });
      const cost = Number(msg.cost) || 0;
      const tok = msg.tokens || {};
      totalCost += cost;
      totalTokens.input     += Number(tok.input) || 0;
      totalTokens.output    += Number(tok.output) || 0;
      totalTokens.reasoning += Number(tok.reasoning) || 0;
      saveTotals();
      const sTok = (tok.input||0) + (tok.output||0) + (tok.reasoning||0);
      if (cost > 0 || sTok > 0) {
        send({ type: "bubble", text: `💰 $${cost.toFixed(4)} · today $${totalCost.toFixed(2)} · 🧠 ${(sTok/1000).toFixed(1)}k`, duration: 10000 });
      }
    }
  });

  // =====================================================================
  // lifecycle
  // =====================================================================
  api.lifecycle.onDispose(() => {
    log("info", "dispose");
    clearInterval(pollInterval);
    if (stallTimer) clearTimeout(stallTimer);
    if (busyTimer) clearTimeout(busyTimer);
    try {
      send({ type: "quit" });
      setTimeout(() => { try { proc.kill(); } catch {} }, 800);
    } catch {}
  });
};

export default { tui };
