# opencode-pet

A desktop pet overlay ([月薪喵](https://github.com/Lumi-arta/desktop_cat) sprites, MIT) that reacts to [opencode](https://github.com/sst/opencode) session activity in real time.

The pet runs as a transparent always-on-top tkinter window spawned by a TUI plugin. The plugin listens to opencode's TUI-local event bus and forwards activity signals to the pet over stdin.

## Features

- **7 unique GIF sprites**: idle / waiting / running / running-left / running-right / review / jumping / waving / failed
- **Three-tier frequency separation**: 120 Hz physics, 60 Hz render, 12 Hz GIF
- **Drag-to-throw physics** with gravity, bounce, friction, air-drag
- **Activity-aware states**:
  - `idle` → `waiting.gif` with random `running-right.gif` walks every 20–120 s
  - `busy` entrance sequence → `running-right.gif` (1.5 s) → `review.gif` (1.5 s) → work
  - `thinking` (reasoning parts) → `review.gif`
  - `speaking` (text parts) → `running.gif`
  - `tool` (tool parts) → `running.gif`
  - completion → 5 s `waving.gif` celebrate flash + cost bubble
  - retry / error → `failed.gif`
- **Permission / question alerts** with right-click dismiss
- **Cost / token tracking** persisted via opencode KV
- **WS_EX_NOACTIVATE** window — never steals keyboard focus from the terminal

## Architecture

```
opencode TUI process                 pet process (pythonw)
─────────────────────                ──────────────────────
index.js (TUI plugin)                pet.py (tkinter window)
  ├─ api.event.on(...)                 ├─ stdin_loop (thread)
  │   • session.status                 │   parses JSON lines
  │   • message.part.updated           │   dispatches via root.after()
  │   • message.updated                ├─ _effective_state()
  │   • permission/question            │   priority: alert > flash >
  │   • todo.updated                   │            moving > busy-seq > idle-seq
  ├─ client.session.status() poll      ├─ physics step @120 Hz
  │   every 500 ms (busy/idle guard)   ├─ render @60 Hz
  └─ stdin JSON ───────────────────►   └─ GIF advance @12 Hz
    {type:"status"|"activity"|"flash"|
     "bubble"|"alert"|...}
```

## Install

### 1. Clone and link the plugin

```bash
git clone https://github.com/Maple127667/opencode-pet.git
```

Register it in `~/.config/opencode/tui.json`:

```json
["oh-my-openagent@latest", "file:/path/to/opencode-pet"]
```

### 2. Preprocess white-background GIFs (one-time)

Two source GIFs ship with white backgrounds. Run the preprocessor once to
flood-fill key them out and re-save as transparent GIFs:

```bash
python preprocess_gifs.py
```

Originals are backed up as `*-orig.gif`. Re-running restores from backup
before re-processing, so it's safe to tweak `WHITE_TOL` and re-run.

### 3. Restart opencode

The pet spawns at the bottom-right corner. Send a message — the cat should
walk in, review, run while speaking, and wave on completion.

## Configuration

All tunable constants live at the top of `pet.py`:

| Constant | Default | Purpose |
|---|---|---|
| `IDLE_WALK_MIN_DELAY` | 20.0 s | Min interval between idle walks |
| `IDLE_WALK_MAX_DELAY` | 120.0 s | Max interval between idle walks |
| `IDLE_WALK_DURATION` | 1.5 s | How long each walk lasts |
| `BUSY_RUN_DURATION` | 1.5 s | busy_run phase length |
| `BUSY_REVIEW_DURATION` | 1.5 s | busy_review phase length |
| `GRAVITY` | 4.0 | Drag-to-throw gravity |
| `AIR_DRAG` | 0.045 | Per-step velocity damping |
| `BOUNCE` | 0.30 | Wall collision energy retention |
| `FRICTION` | 0.55 | Ground friction |

In `index.js`:

| Constant | Default | Purpose |
|---|---|---|
| `STALL_TIMEOUT_MS` | 60000 | Safety net: force activity idle if no part.updated |
| Polling interval | 500 ms | `client.session.status()` fallback cadence |

## Asset credits

[月薪喵 / desktop_cat](https://github.com/Lumi-arta/desktop_cat) — MIT License.

## License

MIT
