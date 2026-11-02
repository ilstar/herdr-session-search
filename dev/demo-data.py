#!/usr/bin/env python3
"""Writes made-up Claude Code, Codex and opencode sessions for screenshots, so a preview
never shows anyone's real sessions.

  dev/demo-data.py OUT [--projects ~/code]

Creates OUT/claude (a CLAUDE_CONFIG_DIR), OUT/codex (a CODEX_HOME), OUT/opencode
(an OPENCODE_DATA_DIR) and an empty
folder per demo project under --projects (so rows don't show "folder gone").
Then point index.py at them:

  CLAUDE_CONFIG_DIR=OUT/claude CODEX_HOME=OUT/codex OPENCODE_DATA_DIR=OUT/opencode \\
    HERDR_SESSION_SEARCH_CACHE=OUT/cache \\
    python3 mods/session-search/index.py search --q login
"""

import argparse
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone

H = 3600 * 1000
D = 24 * H

# (agent, project, branch, age in ms, title, user message, assistant reply)
SESSIONS = [
    ("claude", "acme-web", "fix/login-redirect", 20 * 60 * 1000, "Fix login redirect loop",
     "After logging in, users on Safari bounce back to /login forever. Can you find out why?",
     "The session cookie is set with SameSite=Strict, so the redirect from the identity provider drops it. "
     "Switching the login callback cookie to SameSite=Lax fixes the loop; I added a test for the redirect."),
    ("codex", "acme-api", "main", 3 * H, "Rate-limit the login endpoint",
     "Add rate limiting to POST /login: 5 attempts per minute per IP, then 429.",
     "Added a token-bucket limiter in middleware/rate_limit.py and wired it to the login route. "
     "Failed attempts are counted per IP and per account."),
    ("claude", "acme-web", "main", 9 * H, "Dark mode for the settings page",
     "The settings page ignores the dark theme. Make it follow prefers-color-scheme.",
     "Moved the hard-coded colors to CSS variables and added a dark palette. The login form shares the same tokens now."),
    ("codex", "mobile-app", "feat/biometric", 1 * D + 2 * H, "Face ID login on iOS",
     "Let people log in with Face ID after the first password login.",
     "Stored a refresh token in the Keychain behind biometric access control; the login screen offers Face ID when one exists."),
    ("claude", "acme-api", "chore/deps", 2 * D, "Upgrade Postgres driver",
     "Bump the Postgres driver and fix whatever breaks.",
     "Upgraded to the new major version; two queries used a removed type cast. Tests pass."),
    ("claude", "docs-site", "main", 4 * D, "Write the SSO login guide",
     "Write a guide for setting up SSO login with Okta and Google Workspace.",
     "Added docs/sso.md with both providers, screenshots placeholders and a troubleshooting section for login errors."),
    ("opencode", "acme-web", "fix/login-a11y", 5 * H, "Accessible login form errors",
     "Screen readers don't announce login errors. Fix the form.",
     "Linked each error to its field with aria-describedby and moved focus to the first invalid field on submit."),
    ("opencode", "cli-tool", "main", 3 * D, "Shell completions",
     "Generate shell completions for bash, zsh and fish.",
     "Added a completions subcommand that prints the script for each shell, plus install notes in the README."),
    ("codex", "infra", "main", 6 * D, "Terraform for the staging cluster",
     "Create Terraform for a small staging Kubernetes cluster.",
     "Added modules for the network, the cluster and a node pool, with variables for the region and size."),
    ("claude", "acme-web", "perf/bundle", 9 * D, "Shrink the JS bundle",
     "The main bundle is 1.2 MB. Find the biggest wins.",
     "Lazy-loaded the chart library and the login page's password strength meter; the bundle is down to 640 KB."),
    ("codex", "acme-api", "fix/session-expiry", 12 * D, "Sessions expire too early",
     "People get logged out after 10 minutes even with remember me checked.",
     "The refresh job used the access token's TTL. Refresh tokens now get their own 30-day expiry."),
    ("claude", "cli-tool", "main", 20 * D, "Add --json output",
     "Every command should support --json for scripting.",
     "Added a shared output formatter and --json on all subcommands, with snapshot tests."),
]


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--projects", default=os.path.expanduser("~/code"))
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    now = int(time.time() * 1000)

    os.makedirs(os.path.join(out, "codex", "sessions"), exist_ok=True)
    con = sqlite3.connect(os.path.join(out, "codex", "state_5.sqlite"))
    con.execute("create table if not exists threads(id text primary key, rollout_path text, cwd text, name text,"
                " title text, first_user_message text, updated_at integer, updated_at_ms integer, git_branch text,"
                " archived integer, source text)")

    os.makedirs(os.path.join(out, "opencode"), exist_ok=True)
    oc = sqlite3.connect(os.path.join(out, "opencode", "opencode.db"))
    oc.execute("create table if not exists session_v2(id text primary key, parent_id text, directory text,"
               " title text, time_updated integer, time_archived integer)")
    oc.execute("create table if not exists session_message(id text primary key, session_id text, type text,"
               " seq integer, time_created integer, time_updated integer, data text)")

    for agent, project, branch, age, title, ask, answer in SESSIONS:
        cwd = os.path.join(a.projects, project)
        os.makedirs(cwd, exist_ok=True)
        sid = str(uuid.uuid4())
        t = now - age
        if agent == "claude":
            folder = os.path.join(out, "claude", "projects", cwd.replace("/", "-"))
            os.makedirs(folder, exist_ok=True)
            lines = [
                {"type": "user", "cwd": cwd, "gitBranch": branch, "timestamp": iso(t - 60000),
                 "message": {"role": "user", "content": ask}},
                {"type": "assistant", "cwd": cwd, "gitBranch": branch, "timestamp": iso(t),
                 "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}},
                {"type": "custom-title", "customTitle": title, "sessionId": sid},
            ]
            with open(os.path.join(folder, sid + ".jsonl"), "w") as f:
                f.write("\n".join(json.dumps(l) for l in lines) + "\n")
        elif agent == "opencode":
            sid = "ses_" + uuid.uuid4().hex[:26]
            oc.execute("insert into session_v2 values(?,?,?,?,?,?)", (sid, None, cwd, title, t, None))
            for seq, (kind, data) in enumerate([
                    ("user", {"text": ask}),
                    ("assistant", {"content": [{"type": "text", "text": answer}]})]):
                oc.execute("insert into session_message values(?,?,?,?,?,?,?)",
                           ("msg_%s_%d" % (sid, seq), sid, kind, seq, t - 60000 + seq * 60000, t, json.dumps(data)))
        else:
            rollout = os.path.join(out, "codex", "sessions", "rollout-%s.jsonl" % sid)
            lines = [
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                      "content": [{"type": "input_text", "text": ask}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                      "content": [{"type": "output_text", "text": answer}]}},
            ]
            with open(rollout, "w") as f:
                f.write("\n".join(json.dumps(l) for l in lines) + "\n")
            con.execute("insert into threads values(?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, rollout, cwd, None, title, ask, t // 1000, t, branch, 0, "cli"))
    con.commit()
    oc.commit()
    print(out)


if __name__ == "__main__":
    main()
