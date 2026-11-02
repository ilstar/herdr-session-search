// Search past agent sessions (Claude Code, Codex, opencode) by keyword and resume one.
// Opens as a popup (mod.open:session-search). Type to search every session on
// this computer, whatever folder it ran in; Tab cycles the agent filter,
// Shift+Tab limits it to the current space's repo. ↑/↓ to pick, Enter resumes
// it in a new pane beside the focused one (in the session's folder), Cmd+Enter
// in a new tab of the space already working in that folder (or a new space
// there). Esc leaves.
//
// The search itself is index.py: an incremental SQLite full-text index of the
// agents' session files, cached in ~/.cache/herdr-session-search.

import { View, div, svg, image } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { exec, spaces, panes, focused, run, theme, props, closeOverlay, home, appHome, notify } from "herdr";

const SCRIPT = `${appHome()}/mods/session-search/index.py`;
const PYTHON = "/usr/bin/python3";

// One entry per agent: how it shows and how to resume a session. Add an
// agent here and a matching source in index.py.
const AGENTS = {
  claude: { name: "Claude", icon: "icons/claude.svg", resume: (id) => ["claude", "--resume", id] },
  codex: { name: "Codex", icon: "icons/codex.svg", resume: (id) => ["codex", "resume", id] },
  opencode: { name: "opencode", icon: "icons/opencode.svg", resume: (id) => ["opencode", "--session", id] },
};
const FILTERS = [null, ...Object.keys(AGENTS)];
const ROW = 62;
const VISIBLE = 8; // rows shown before scrolling

function tilde(p) {
  const h = home();
  return p && p.startsWith(h) ? "~" + p.slice(h.length) : p || "";
}

function ago(ms) {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d`;
  return new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

const within = (path, dir) => path === dir || path.startsWith(dir.endsWith("/") ? dir : dir + "/");

// The space (on this computer) whose terminals are in or around `cwd`: the one
// with the closest pane folder wins.
function spaceFor(cwd) {
  if (!cwd) return null;
  const tabSpace = {};
  for (const s of spaces()) if (!s.machine) for (const t of s.tabs) tabSpace[t.id] = s;
  let best = null;
  let score = -1;
  for (const p of panes()) {
    const s = tabSpace[p.tab];
    // A shell at home is near everything; it says nothing about the session.
    if (!s || !p.cwd || p.cwd === home()) continue;
    const n = within(cwd, p.cwd) ? p.cwd.length : within(p.cwd, cwd) ? cwd.length : -1;
    if (n > score) [best, score] = [s, n];
  }
  return best;
}

export default class SessionSearch extends View {
  init(_props, cx) {
    this.query = "";
    this.agent = null;
    this.scope = null; // a folder, when limited to the current space's repo
    this.scopeRoot = null;
    this.results = [];
    this.pending = 0;
    this.total = 0;
    this.loading = true;
    this.error = null;
    this.at = 0;
    this.scroll = 0;
    this.seq = 0;
    this.debounce = null;
    this.handle = cx.focus_handle();
    this.input = InputState.new({ placeholder: "Search Claude, Codex and opencode sessions…" });
    this.input.on("change", (_e, cx) => {
      this.query = this.input.value();
      this.schedule(cx, 140);
    });
    this.input.on("submit", () => this.open(this.results[this.at], false));
    this.input.focus();
    cx.timer.after(600, () => this.input.focus());
    // The current space's repo, for Shift+Tab.
    cx.spawn(async (cx) => {
      const cwd = focused()?.cwd;
      if (!cwd) return;
      this.scopeRoot = cwd;
      try {
        const r = await exec("git", ["rev-parse", "--show-toplevel"], { cwd });
        if (r.code === 0 && r.stdout.trim()) this.scopeRoot = r.stdout.trim();
      } catch {}
      cx.notify();
    });
    this.search(cx);
  }

  schedule(cx, ms) {
    this.debounce?.cancel();
    this.debounce = cx.timer.after(ms, (cx) => this.search(cx));
  }

  search(cx) {
    const seq = ++this.seq;
    const args = [SCRIPT, "search", "--q", this.query, "--limit", "80", "--budget", "8"];
    if (this.agent) args.push("--agent", this.agent);
    if (this.scope) args.push("--cwd", this.scope);
    this.loading = true;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = await exec(PYTHON, args);
        if (seq !== this.seq) return;
        if (r.code !== 0) throw new Error(r.stderr.trim().split("\n").pop() || `index.py exited ${r.code}`);
        const out = JSON.parse(r.stdout);
        this.results = out.results;
        this.pending = out.pending;
        this.total = out.total;
        this.error = null;
        this.at = 0;
        this.scroll = 0;
      } catch (e) {
        if (seq !== this.seq) return;
        this.error = String(e?.message ?? e);
      }
      this.loading = false;
      // A first build that ran out of time carries on in the next call.
      if (this.pending > 0) this.schedule(cx, 50);
      cx.notify();
    });
  }

  close() {
    closeOverlay(props().overlay);
  }

  // inTab: a new tab in the space working in the session's folder (or a new
  // space there); otherwise a new pane beside the focused one.
  open(r, inTab) {
    if (!r || this.opened) return;
    const a = AGENTS[r.agent];
    if (!a) return;
    if (!r.exists && r.agent === "claude") {
      // Claude finds a session by the folder it ran in.
      notify({ title: "Can't resume this session", body: `Its folder is gone: ${tilde(r.cwd)}`, level: "warning" });
      return;
    }
    this.opened = true;
    this.close();
    const argv = a.resume(r.id);
    const folder = r.exists ? r.cwd : home();
    const name = (r.title || "").slice(0, 40) || undefined;
    if (!inTab && focused()?.pane) {
      run("terminal.open", { argv, folder, place: "right", name });
      return;
    }
    const space = spaceFor(r.cwd);
    if (space) run("tab.new", { space: space.id, folder, argv, name });
    else run("space.new", { folder, argv, tab: name });
  }

  chip(t, id, label, on, click) {
    return div()
      .id(id)
      .px(8)
      .h(22)
      .flex()
      .items_center()
      .rounded(6)
      .text_size(11.5)
      .cursor_pointer()
      .text_color(on ? t.foreground : t.dim)
      .when(on, (el) => el.bg(t.selected))
      .hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => click(cx))
      .child(label);
  }

  setAgent(agent, cx) {
    this.agent = agent;
    this.search(cx);
  }

  toggleScope(cx) {
    if (!this.scopeRoot) return;
    this.scope = this.scope ? null : this.scopeRoot;
    this.search(cx);
  }

  snippet(t, r) {
    // index.py marks matches with \x01…\x02.
    const parts = (r.snippet || r.first || "").replace(/\s+/g, " ").split(/([\x01\x02])/);
    const segs = [];
    let hit = false;
    for (const p of parts) {
      if (p === "\x01") hit = true;
      else if (p === "\x02") hit = false;
      else if (p) segs.push({ text: p, hit });
    }
    return h_flex()
      .h(16)
      .min_w(0)
      .overflow_hidden()
      .text_size(11.5)
      .text_color(t.dim)
      .children(
        segs.map((s, i) =>
          div()
            .flex_none()
            .whitespace_nowrap()
            .when(s.hit, (el) => el.text_color(t.accent).font_semibold())
            .child(s.text),
        ),
      );
  }

  row(t, r, i, on) {
    const a = AGENTS[r.agent];
    const space = spaceFor(r.cwd);
    const where = [tilde(r.cwd), r.branch && r.branch !== "HEAD" ? r.branch : null].filter(Boolean).join("  ·  ");
    return h_flex()
      .id(`ss-${r.agent}-${r.id}`)
      .mx(4)
      .px(10)
      .h(ROW)
      .gap(10)
      .items_center()
      .rounded(7)
      .cursor_pointer()
      .when(on, (el) => el.bg(t.hover))
      .hover((s) => s.bg(t.hover))
      .on_click((e, cx) => {
        this.at = i;
        this.open(r, e.modifiers?.platform);
        cx.notify();
      })
      .child(image(a.icon).w(18).h(18).flex_none())
      .child(
        v_flex()
          .flex_1()
          .min_w(0)
          .gap(2)
          .child(
            h_flex()
              .gap(8)
              .items_center()
              .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).child(r.title || "(untitled)"))
              .when(r.archived, (el) => el.child(div().flex_none().text_size(10.5).text_color(t.dim).child("archived")))
              .child(div().flex_none().text_size(11).text_color(t.dim).child(ago(r.updated))),
          )
          .child(
            h_flex()
              .gap(8)
              .items_center()
              .text_size(11.5)
              .child(div().min_w(0).truncate().text_color(r.exists ? t.muted : t.blocked).child(where + (r.exists ? "" : "  (folder gone)")))
              .when(!!space, (el) =>
                el.child(div().flex_none().px(6).rounded(4).bg(t.raised).text_size(10.5).text_color(t.muted).child(space.name)),
              ),
          )
          .child(this.snippet(t, r)),
      );
  }

  render() {
    const t = theme();
    const rows = this.results;
    this.at = Math.max(0, Math.min(this.at, rows.length - 1));
    const total = rows.length * ROW;
    const height = Math.max(ROW, Math.min(VISIBLE * ROW, total));
    const maxScroll = Math.max(0, total - height);
    if (this.at * ROW < this.scroll) this.scroll = this.at * ROW;
    if ((this.at + 1) * ROW > this.scroll + height) this.scroll = (this.at + 1) * ROW - height;
    this.scroll = Math.max(0, Math.min(this.scroll, maxScroll));
    const first = Math.floor(this.scroll / ROW);
    const last = Math.min(rows.length, Math.ceil((this.scroll + height) / ROW));

    const status = this.error
      ? this.error
      : this.pending
        ? `Indexing… ${this.total} sessions so far`
        : `${rows.length}${rows.length >= 80 ? "+" : ""} of ${this.total} sessions${this.query ? "" : ", newest first"}`;

    return v_flex()
      .id("session-search")
      .track_focus(this.handle)
      .w_full()
      .p(18)
      .gap(10)
      .bg(t.popover)
      .text_size(13)
      .on_key_down((e, cx) => {
        const k = e.key;
        const n = rows.length;
        if (k === "escape") return this.close();
        if (k === "down" && n) this.at = (this.at + 1) % n;
        else if (k === "up" && n) this.at = (this.at + n - 1) % n;
        else if (k === "pagedown" && n) this.at = Math.min(n - 1, this.at + Math.floor(height / ROW));
        else if (k === "pageup" && n) this.at = Math.max(0, this.at - Math.floor(height / ROW));
        else if (k === "enter" && e.modifiers.platform) this.open(rows[this.at], true);
        else if (k === "tab" && e.modifiers.shift) this.toggleScope(cx);
        else if (k === "tab") this.setAgent(FILTERS[(FILTERS.indexOf(this.agent) + 1) % FILTERS.length], cx);
        else return;
        cx.stop_propagation();
        cx.notify();
      })
      .child(
        h_flex()
          .items_center()
          .gap(6)
          .child(div().flex_1().text_size(15).font_semibold().text_color(t.foreground).child("Search sessions"))
          .children(FILTERS.map((f) => this.chip(t, `ss-agent-${f ?? "all"}`, f ? AGENTS[f].name : "All", this.agent === f, (cx) => this.setAgent(f, cx))))
          .child(div().w(1).h(14).mx(4).bg(t.border))
          .child(
            this.chip(t, "ss-scope", this.scopeRoot ? `Only ${tilde(this.scopeRoot).split("/").pop() || "~"}` : "Only this space", !!this.scope, (cx) =>
              this.toggleScope(cx),
            ),
          ),
      )
      .child(
        h_flex()
          .h(36)
          .flex_none()
          .px(10)
          .gap(8)
          .items_center()
          .rounded(9)
          .border_1()
          .border_color(t.accent)
          .bg(t.background)
          .child(svg("mods/workspace-chrome/search.svg").size(15).text_color(t.dim))
          .child(Input.new(this.input).flex_1().h_full().text_size(12.8).text_color(t.foreground))
          .when(this.loading, (el) => el.child(svg("mods/workspace-chrome/spinner.svg").size(13).text_color(t.dim).spin(900))),
      )
      .child(
        div()
          .id("ss-list")
          .relative()
          .h(height)
          .flex_none()
          .overflow_hidden()
          .on_scroll_wheel((e, cx) => {
            this.scroll = Math.max(0, Math.min(this.scroll - e.delta.y, maxScroll));
            // The selection follows the wheel, so the next frame doesn't scroll back to it.
            const top = Math.ceil(this.scroll / ROW);
            const bottom = Math.floor((this.scroll + height) / ROW) - 1;
            this.at = Math.max(top, Math.min(this.at, bottom));
            cx.notify();
          })
          .when(!rows.length && !this.loading, (el) =>
            el.child(div().p(16).text_color(t.dim).child(this.query ? "No sessions match." : "No sessions yet.")),
          )
          .children(
            rows.slice(first, last).map((r, j) => {
              const i = first + j;
              return this.row(t, r, i, i === this.at).absolute().left(0).right(0).top(i * ROW - this.scroll);
            }),
          ),
      )
      .child(
        h_flex()
          .gap(14)
          .text_size(11)
          .text_color(t.dim)
          .child(div().flex_1().truncate().text_color(this.error ? t.blocked : t.dim).child(status))
          .child("↵ new pane")
          .child("⌘↵ in its space")
          .child("⇥ agent")
          .child("⇧⇥ this space")
          .child("Esc"),
      );
  }
}
