#!/usr/bin/env python3
"""Full-text index of past agent sessions (Claude Code, Codex) for the session-search mod.

  index.py search [--q TEXT] [--agent claude|codex] [--cwd DIR] [--limit N] [--budget SECONDS]

Each call first brings the index up to date (only files that changed since the
last call), spending at most --budget seconds, then searches it. Prints JSON:
  { "results": [...], "pending": <sessions not indexed yet>, "total": <indexed> }

The index lives in ~/.cache/herdr-session-search/index.db ($HERDR_SESSION_SEARCH_CACHE
overrides the folder) and is only a cache: delete it to rebuild. Read-only on the agents' own files.

Adding an agent: write a `<name>_sources()` generator yielding (key, sig, loader)
and register it in SOURCES.
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
CACHE = os.environ.get("HERDR_SESSION_SEARCH_CACHE") or os.path.join(HOME, ".cache", "herdr-session-search")
DB = os.path.join(CACHE, "index.db")
SCHEMA = 2
BODY_CAP = 400_000  # characters of conversation text kept per session

# Injected context, not something the person or the agent said.
NOISE = re.compile(
    r"<(system-reminder|environment_context|permissions instructions|recommended_plugins|"
    r"user_instructions|local-command-stdout|local-command-stderr|command-message|"
    r"command-args|ide_opened_file|ide_selection|turn_aborted)\b[^>]*>.*?</\1>",
    re.S,
)
CONTEXT_PREFIXES = ("<environment_context", "<permissions", "<user_instructions", "# AGENTS.md",
                    "<recommended_plugins", "Caveat: The messages below", "<INSTRUCTIONS>")


def clean(text):
    text = NOISE.sub(" ", text or "")
    text = re.sub(r"<command-name>(.*?)</command-name>", r"\1", text)
    return text.strip()


def texts(content):
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"):
                yield part.get("text") or ""


def iso_ms(ts):
    # "2026-11-02T05:08:37.992Z" -> ms since epoch
    try:
        from datetime import datetime, timezone
        return int(datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return 0


def jsonl(path):
    if path.endswith(".zst"):
        out = subprocess.run(["zstd", "-dcq", path], capture_output=True, timeout=20).stdout
        lines = out.decode("utf-8", "replace").splitlines()
    else:
        lines = open(path, encoding="utf-8", errors="replace")
    for line in lines:
        try:
            yield json.loads(line)
        except Exception:
            continue


class Doc:
    def __init__(self, agent, sid):
        self.agent, self.sid = agent, sid
        self.title = self.first = self.cwd = self.branch = ""
        self.updated = 0
        self.messages = 0
        self.parts = []
        self.size = 0
        self.extra = {}

    def add(self, text):
        if text and self.size < BODY_CAP:
            self.parts.append(text)
            self.size += len(text)

    def body(self):
        return "\n".join(self.parts)[:BODY_CAP]


# ---------- Claude Code: ~/.claude/projects/<project>/<session>.jsonl ----------

def claude_sources():
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    for path in glob.glob(os.path.join(root, "projects", "*", "*.jsonl")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_size == 0:
            continue
        sid = os.path.basename(path)[:-6]
        yield "claude:" + sid, "%d:%d" % (st.st_mtime_ns, st.st_size), lambda p=path, s=sid, m=st.st_mtime: claude_load(p, s, m)


def claude_load(path, sid, mtime):
    d = Doc("claude", sid)
    summary = custom = ""
    for o in jsonl(path):
        t = o.get("type")
        if t == "custom-title":
            custom = o.get("customTitle") or custom
        elif t == "summary":
            summary = o.get("summary") or summary
        elif t in ("user", "assistant") and not o.get("isSidechain") and not o.get("isMeta"):
            d.cwd = d.cwd or o.get("cwd") or ""
            d.branch = o.get("gitBranch") or d.branch
            if o.get("timestamp"):
                d.updated = max(d.updated, iso_ms(o["timestamp"]))
            for raw in texts((o.get("message") or {}).get("content")):
                text = clean(raw)
                if not text or text.startswith(CONTEXT_PREFIXES):
                    continue
                if t == "user":
                    d.messages += 1
                    d.first = d.first or text[:300]
                d.add(text)
    if not d.messages:
        return None
    d.title = custom or summary or d.first.split("\n")[0][:120]
    d.updated = d.updated or int(mtime * 1000)
    return d


# ---------- Codex: ~/.codex/state_5.sqlite threads + rollout files ----------

def codex_sources():
    root = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
    dbs = sorted(glob.glob(os.path.join(root, "state_*.sqlite")))
    if not dbs:
        return
    try:
        con = sqlite3.connect("file:%s?mode=ro" % dbs[-1], uri=True, timeout=2)
        rows = con.execute(
            "select id, rollout_path, cwd, coalesce(nullif(name,''), title), first_user_message, "
            "coalesce(updated_at_ms, updated_at*1000), git_branch, archived, source from threads "
            "where source not like '{%'"  # sub-agents (guardian reviews, spawned workers)
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return
    for sid, rollout, cwd, title, first, updated, branch, archived, _src in rows:
        try:
            st = os.stat(rollout)
            sig = "%d:%d:%s" % (st.st_mtime_ns, st.st_size, updated)
        except (OSError, TypeError):
            rollout, sig = None, "none:%s" % updated
        meta = dict(cwd=cwd or "", title=title or "", first=first or "", updated=updated or 0,
                    branch=branch or "", archived=bool(archived))
        yield "codex:" + sid, sig, lambda s=sid, r=rollout, m=meta: codex_load(s, r, m)


def codex_load(sid, rollout, meta):
    d = Doc("codex", sid)
    d.cwd, d.branch, d.updated = meta["cwd"], meta["branch"], meta["updated"]
    d.first = clean(meta["first"])[:300]
    d.extra["archived"] = meta["archived"]
    if rollout:
        for o in jsonl(rollout):
            p = o.get("payload") or {}
            if o.get("type") != "response_item" or p.get("type") != "message" or p.get("role") not in ("user", "assistant"):
                continue
            for raw in texts(p.get("content")):
                text = clean(raw)
                if not text or text.startswith(CONTEXT_PREFIXES):
                    continue
                if p.get("role") == "user":
                    d.messages += 1
                    d.first = d.first or text[:300]
                d.add(text)
    if not d.first and not d.parts:
        return None
    d.messages = max(d.messages, 1)
    d.title = clean(meta["title"]).split("\n")[0][:120] or d.first.split("\n")[0][:120]
    return d


SOURCES = {"claude": claude_sources, "codex": codex_sources}


# ---------- index ----------

def connect():
    os.makedirs(CACHE, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.execute("pragma journal_mode=wal")
    version = con.execute("pragma user_version").fetchone()[0]
    if version != SCHEMA:
        con.executescript(
            "drop table if exists sessions; drop table if exists fts;"
            "create table sessions(key text primary key, sig text, agent text, sid text, cwd text, title text,"
            " first text, branch text, updated integer, messages integer, extra text);"
            "create virtual table fts using fts5(title, body, cwd, tokenize='unicode61 remove_diacritics 2');"
            "pragma user_version=%d;" % SCHEMA
        )
    return con


def refresh(con, budget):
    known = dict(con.execute("select key, sig from sessions"))
    todo, seen = [], set()
    for gen in SOURCES.values():
        for key, sig, loader in gen():
            seen.add(key)
            if known.get(key) != sig:
                todo.append((key, sig, loader))
    gone = [k for k in known if k not in seen]
    for k in gone:
        drop(con, k)
    deadline = time.time() + budget
    done = 0
    for key, sig, loader in todo:
        if time.time() > deadline:
            break
        try:
            d = loader()
        except Exception as e:  # one bad file must not stop the rest
            print("index: %s: %s" % (key, e), file=sys.stderr)
            d = None
        drop(con, key)
        if d is None:  # empty session: remember it so it isn't re-read
            con.execute("insert into sessions(key, sig, agent, updated, messages) values(?,?,?,0,0)",
                        (key, sig, key.split(":")[0]))
        else:
            cur = con.execute(
                "insert into sessions(key, sig, agent, sid, cwd, title, first, branch, updated, messages, extra)"
                " values(?,?,?,?,?,?,?,?,?,?,?)",
                (key, sig, d.agent, d.sid, d.cwd, d.title, d.first, d.branch, d.updated, d.messages, json.dumps(d.extra)))
            con.execute("insert into fts(rowid, title, body, cwd) values(?,?,?,?)",
                        (cur.lastrowid, d.title, d.body(), d.cwd))
        done += 1
        if done % 25 == 0:
            con.commit()
    con.commit()
    return len(todo) - done


def drop(con, key):
    row = con.execute("select rowid from sessions where key=?", (key,)).fetchone()
    if row:
        con.execute("delete from fts where rowid=?", row)
        con.execute("delete from sessions where rowid=?", row)


def fts_query(q):
    terms = re.findall(r"\w+", q, re.U)
    return " ".join('"%s"*' % t for t in terms)


def search(con, q, agent, cwd, limit):
    where, args = ["s.messages > 0"], []
    if agent:
        where.append("s.agent = ?")
        args.append(agent)
    if cwd:
        where.append("(s.cwd = ? or s.cwd like ? escape '\\')")
        args += [cwd, cwd.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").rstrip("/") + "/%"]
    match = fts_query(q or "")
    cols = "s.agent, s.sid, s.cwd, s.title, s.first, s.branch, s.updated, s.messages, s.extra"
    if match:
        sql = ("select %s, snippet(fts, 1, char(1), char(2), '…', 14) from fts join sessions s on s.rowid = fts.rowid"
               " where fts match ? and %s order by bm25(fts, 8.0, 1.0, 2.0) + 0 limit ?") % (cols, " and ".join(where))
        rows = con.execute(sql, [match] + args + [limit]).fetchall()
    else:
        sql = "select %s, '' from sessions s where %s order by s.updated desc limit ?" % (cols, " and ".join(where))
        rows = con.execute(sql, args + [limit]).fetchall()
    out = []
    for a, sid, c, title, first, branch, updated, messages, extra, snip in rows:
        out.append(dict(agent=a, id=sid, cwd=c, title=title, first=first, branch=branch, updated=updated,
                        messages=messages, snippet=re.sub(r"\s+", " ", snip or ""), exists=os.path.isdir(c or ""),
                        **json.loads(extra or "{}")))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["search"])
    ap.add_argument("--q", default="")
    ap.add_argument("--agent", default="")
    ap.add_argument("--cwd", default="")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--budget", type=float, default=6)
    a = ap.parse_args()
    con = connect()
    pending = refresh(con, a.budget) if a.budget > 0 else 0
    total = con.execute("select count(*) from sessions where messages > 0").fetchone()[0]
    try:
        results = search(con, a.q, a.agent, a.cwd, a.limit)
    except sqlite3.OperationalError as e:
        results = []
        print("search: %s" % e, file=sys.stderr)
    json.dump(dict(results=results, pending=pending, total=total), sys.stdout)


if __name__ == "__main__":
    main()
