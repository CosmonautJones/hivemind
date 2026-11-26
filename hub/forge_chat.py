"""Phase 4 (2.4.0) chat: every task is a conversation you can follow up on, with photos attached.

Routes (added in `register`):
- POST /api/uploads (UI)            {name, data: base64 | data: URL} -> {id, name, mime, size}. Images only (png, jpeg,
                                    webp, gif; sniffed from the bytes, never trusted from the client), <= 8 MB each.
- GET  /api/uploads/{id} (UI)       the image (chat thumbnails).
- GET  /api/agent/uploads/{id} (agent)  the image, for the agent to place next to the job before it starts.
- GET  /api/threads?limit=&q= (UI)  conversations, newest first: {threads: [{id, title, engine, node_id, status, ...}]}.
- GET  /api/threads/{id} (UI)       {thread, turns: [{id, text, attachments, status, output, ...}]}.

Core hooks (called by hub/app.py):
- `plan_reply(reply_to, message, ...)`: POST /api/jobs with `reply_to` continues the conversation. Claude/Codex turns
  that have a CLI session resume it on the SAME machine and folder (`claude -p --resume <id>`, `codex exec resume`);
  anything else (PowerShell, Ollama, old agents) starts a new turn seeded with the earlier turns ("context carry").
- `dispatch_extras(job, agent_version)`: adds `session_id` (hub-assigned `claude --session-id`), `attachments` and
  `scratch_of` to a user-lane dispatch item; `agent_ok` holds back jobs an older agent cannot run faithfully.

Uploads are a replicated table (base64 text), so a failover keeps them; they are pruned after UPLOAD_TTL_S.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import uuid
from typing import Any, Optional

from forge_store import jloads, new_id

CHAT_AGENT = (2, 4, 0)                 # agents that understand session_id / attachments / scratch_of
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_ATTACHMENTS = 8
UPLOAD_TTL_S = 30 * 86400
UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
CODEX_SESSION_RE = re.compile(r"session id:\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
ACTIVE = ("queued", "starting", "running")
CHAT_SOURCES = ("ui", "chat")
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
CARRY_TURNS = 6                        # context carry: this many earlier turns
CARRY_CHARS = 4000                     # ... each side capped to this many characters
TURN_OUTPUT_MAX = 200_000              # thread view: last N characters of each turn's output


def sniff_image(data: bytes) -> Optional[str]:
    """The image MIME type from the magic bytes, or None (not an image we accept)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def decode_upload(data: str) -> bytes:
    """base64 or a data: URL -> bytes (ValueError on bad input)."""
    s = str(data or "")
    if s.startswith("data:"):
        s = s.split(",", 1)[1] if "," in s else ""
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("data must be base64") from e


def safe_name(name: str, mime: str) -> str:
    """A file name safe to create on the agent (letters, digits, . _ -), with the extension of the sniffed type."""
    stem = NAME_SAFE_RE.sub("-", str(name or "").rsplit(".", 1)[0]).strip(".-")[:60] or "image"
    return f"{stem}.{EXT[mime]}"


def codex_session(output: str) -> Optional[str]:
    """`codex exec` prints `session id: <uuid>` in its header: the id to resume the turn with."""
    m = CODEX_SESSION_RE.search(output or "")
    return m.group(1).lower() if m else None


AGENT_LINE_RE = re.compile(r"^\[(?:forge\] \[)?[A-Za-z0-9._-]{1,64}\] ")


def clean_output(output: str, node_id: Optional[str] = None) -> str:
    """Job output without the agent / hub status lines (`[forge-01] claude started in ...`, `[hub] ...`)."""
    keep = []
    for line in (output or "").splitlines():
        if AGENT_LINE_RE.match(line) and (line.startswith("[hub] ") or line.startswith("[forge] ")
                                          or (node_id and line.startswith(f"[{node_id}] "))):
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def title_of(text: str) -> str:
    first = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    return (first[:77] + "...") if len(first) > 80 else (first or "New chat")


def carry_prompt(turns: list[dict], message: str) -> str:
    """A new turn seeded with the earlier turns (engines / machines that cannot resume a CLI session)."""
    parts = ["You are continuing an earlier conversation. Here are the previous turns (oldest first):", ""]
    for t in turns[-CARRY_TURNS:]:
        u = (t.get("text") or "")[:CARRY_CHARS]
        a = (t.get("answer") or "")[-CARRY_CHARS:]
        parts += [f"User: {u}", f"Assistant: {a or '(no answer)'}", ""]
    parts += ["The user's new message:", message]
    return "\n".join(parts)


class Chat:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.store = ctx.store

    # ---------- uploads ----------
    def save_upload(self, name: str, data: str) -> dict:
        from fastapi import HTTPException
        try:
            raw = decode_upload(data)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not raw:
            raise HTTPException(400, "empty file")
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"image is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        mime = sniff_image(raw)
        if not mime:
            raise HTTPException(415, "only PNG, JPEG, WebP and GIF images can be attached")
        now, uid = self.ctx.clock(), new_id(self.ctx.clock)
        meta = {"id": uid, "name": safe_name(name, mime), "mime": mime, "size": len(raw)}
        self.store.x("INSERT INTO uploads(id,mime,name,size,sha256,data,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                     (uid, mime, meta["name"], len(raw), hashlib.sha256(raw).hexdigest(),
                      base64.b64encode(raw).decode("ascii"), now, now))
        return meta

    def upload_bytes(self, uid: str) -> tuple[bytes, str]:
        from fastapi import HTTPException
        r = self.store.q("SELECT mime,data FROM uploads WHERE id=?", (uid,)) if UPLOAD_ID_RE.match(uid or "") else []
        if not r:
            raise HTTPException(404, "no such upload")
        return base64.b64decode(r[0]["data"]), r[0]["mime"]

    def check_attachments(self, ids: Optional[list]) -> list[dict]:
        """Validate a job's attachment ids -> [{id, name, mime, size}] (400 on unknown / too many)."""
        from fastapi import HTTPException
        ids = [str(i) for i in (ids or [])]
        if not ids:
            return []
        if len(ids) > MAX_ATTACHMENTS:
            raise HTTPException(400, f"at most {MAX_ATTACHMENTS} images per message")
        out = []
        for i in dict.fromkeys(ids):
            r = self.store.q("SELECT id,name,mime,size FROM uploads WHERE id=?", (i,)) if UPLOAD_ID_RE.match(i) else []
            if not r:
                raise HTTPException(400, f"unknown attachment {i[:16]}")
            out.append(dict(r[0]))
        return out

    def prune(self, now: float, settled: bool) -> None:
        if not settled or not self.ctx.settled_leader():
            return
        active = set()
        for r in self.store.q("SELECT attachments FROM jobs WHERE status IN ('queued','starting','running') "
                              "AND attachments IS NOT NULL"):
            active |= {a.get("id") for a in jloads(r["attachments"], []) if isinstance(a, dict)}
        for r in self.store.q("SELECT id FROM uploads WHERE created < ? LIMIT 200", (now - UPLOAD_TTL_S,)):
            if r["id"] not in active:
                self.store.delete_row("uploads", r["id"])

    # ---------- replies ----------
    def thread_rows(self, tid: str) -> list:
        # creation order (ids share a millisecond prefix but their random tail is not ordered)
        return self.store.q("SELECT * FROM jobs WHERE id=? OR thread_id=? ORDER BY created, rowid", (tid, tid))

    def turn_view(self, j: Any, with_output: bool = True) -> dict:
        out = self.store.job_output_text(j["id"], j["output"]) if with_output else ""
        ans = clean_output(out, j["node_id"])
        return {"id": j["id"], "text": j["chat_text"] if j["chat_text"] is not None else (j["prompt"] or ""),
                "attachments": jloads(j["attachments"], []), "status": j["status"], "engine": j["engine"],
                "node_id": j["node_id"], "requested": j["requested"], "cwd": j["cwd"], "created": j["created"],
                "started": j["started"], "finished": j["finished"], "exit_code": j["exit_code"],
                "wait_reason": j["wait_reason"], "full_access": bool(j["full_access"]), "project_id": j["project_id"],
                "session": jloads(j["session"], None) or jloads(j["resume"], None),
                "answer": ans[-TURN_OUTPUT_MAX:], "answer_cut": len(ans) > TURN_OUTPUT_MAX,
                "output_seq": j["output_seq"] or 0}

    def plan_reply(self, reply_to: str, message: str) -> dict:
        """POST /api/jobs {reply_to}: how to run the next turn -> {thread_id, job: overrides, resume, scratch_of}."""
        from fastapi import HTTPException
        r = self.store.q("SELECT * FROM jobs WHERE id=?", (reply_to,))
        if not r:
            raise HTTPException(404, "no such conversation")
        tid = r[0]["thread_id"] or r[0]["id"]
        rows = self.thread_rows(tid)
        last = rows[-1]
        if (last["run_as"] or "user") != "user" or last["engine"] in ("action", "admin"):
            raise HTTPException(400, "maintenance and admin tasks cannot be continued")
        if last["status"] in ACTIVE:
            raise HTTPException(409, "still working on the last message")
        node = last["dispatched_to"] or last["node_id"]
        base = {"engine": last["engine"], "cwd": last["cwd"], "full_access": bool(last["full_access"]),
                "project_id": last["project_id"], "model": last["model"], "kind": last["kind"]}
        # where the CLI session lives: the folder the turn ran in (a job without cwd runs in scratch\<its id>)
        scratch_of = last["scratch_of"] or (None if last["cwd"] else last["id"])
        sess = jloads(last["session"], None) or jloads(last["resume"], None)
        if not sess and last["engine"] == "codex":
            sid = codex_session(self.store.job_output_text(last["id"], last["output"]))
            sess = {"tool": "codex", "session_id": sid} if sid else None
        ran = not (last["status"] == "failed" and last["exit_code"] == -1)    # -1: the agent never started the CLI
        if sess and last["engine"] in ("claude", "codex") and ran and node and node != "auto":
            return {"thread_id": tid, "resume": {"tool": sess["tool"], "session_id": str(sess["session_id"])},
                    "scratch_of": scratch_of, "job": base | {"node_id": node, "prompt": message}}
        if last["engine"] == "shell":
            prompt = message                      # PowerShell: each turn is the next command
        else:
            turns = [self.turn_view(x) for x in rows]
            for t in turns:
                t["answer"] = t["answer"][-CARRY_CHARS:]
            prompt = carry_prompt(turns, message)
        same = node if node and node != "auto" else "auto"     # stay on the machine (and folder) of the last turn
        return {"thread_id": tid, "resume": None, "scratch_of": scratch_of if same != "auto" else None,
                "job": base | {"node_id": same, "prompt": prompt}}

    def after_insert(self, jid: str, thread_id: Optional[str], attachments: list[dict], scratch_of: Optional[str],
                     chat_text: Optional[str]) -> None:
        sets, args = [], []
        if thread_id:
            sets.append("thread_id=?"); args.append(thread_id)
        if attachments:
            sets.append("attachments=?"); args.append(json.dumps(attachments))
        if scratch_of:
            sets.append("scratch_of=?"); args.append(scratch_of)
        if chat_text is not None:
            sets.append("chat_text=?"); args.append(chat_text)
        if sets:
            self.store.touch("jobs", "id", jid, ", ".join(sets), tuple(args))

    # ---------- dispatch ----------
    @staticmethod
    def agent_new(agent_version: Optional[str]) -> bool:
        import forge_sched as fs
        return fs.parse_version(agent_version) >= CHAT_AGENT

    def agent_ok(self, j: Any, agent_version: Optional[str]) -> bool:
        """Jobs with photos or a scratch folder from an earlier turn need a 2.4 agent (older ones would drop them)."""
        if (j["attachments"] or j["scratch_of"]) and not self.agent_new(agent_version):
            if j["wait_reason"] != "this machine's Hivemind is older than 2.4 (needed for photos / follow-ups)":
                self.store.touch("jobs", "id", j["id"], "wait_reason=?",
                                 ("this machine's Hivemind is older than 2.4 (needed for photos / follow-ups)",))
            return False
        return True

    def dispatch_extras(self, j: Any, agent_version: Optional[str]) -> dict:
        if not self.agent_new(agent_version):
            return {}
        item: dict = {}
        if j["engine"] == "claude" and not j["resume"]:
            sess = jloads(j["session"], None)
            if not sess:
                sess = {"tool": "claude", "session_id": str(uuid.uuid4())}
                self.store.touch("jobs", "id", j["id"], "session=?", (json.dumps(sess),))
            item["session_id"] = sess["session_id"]
        att = jloads(j["attachments"], [])
        if att:
            item["attachments"] = att
        if j["scratch_of"]:
            item["scratch_of"] = j["scratch_of"]
        return item

    # ---------- thread list / detail ----------
    def threads(self, limit: int, q: str = "") -> dict:
        marks = ",".join("?" * len(CHAT_SOURCES))
        rows = self.store.q(
            "SELECT COALESCE(thread_id,id) AS tid, COUNT(*) AS n, MAX(created) AS last_ts FROM jobs "
            f"WHERE COALESCE(run_as,'user')='user' AND engine NOT IN ('action','admin') AND source IN ({marks}) "
            "GROUP BY tid ORDER BY last_ts DESC LIMIT ?", (*CHAT_SOURCES, 500 if q else limit))
        out, ql = [], q.strip().lower()
        for r in rows:
            first = self.store.q("SELECT * FROM jobs WHERE id=?", (r["tid"],))
            last = self.thread_rows(r["tid"])[-1:]
            if not first or not last:
                continue
            f, l = first[0], last[0]
            text = f["chat_text"] if f["chat_text"] is not None else (f["prompt"] or "")
            if ql and ql not in text.lower() and ql not in (l["chat_text"] or l["prompt"] or "").lower():
                continue
            tail = clean_output(self.store.job_output_tail(l["id"], 400, l["output"]), l["node_id"])
            out.append({"id": r["tid"], "title": title_of(text), "turns": r["n"], "engine": l["engine"],
                        "node_id": l["node_id"], "status": l["status"], "last_id": l["id"], "project_id": f["project_id"],
                        "created": f["created"], "updated": l["finished"] or l["started"] or l["created"],
                        "preview": tail[-160:], "photos": bool(l["attachments"] or f["attachments"])})
            if len(out) >= limit:
                break
        return {"threads": out}

    def thread(self, tid: str) -> dict:
        from fastapi import HTTPException
        r = self.store.q("SELECT id,thread_id FROM jobs WHERE id=?", (tid,))
        if not r:
            raise HTTPException(404, "no such conversation")
        tid = r[0]["thread_id"] or r[0]["id"]
        rows = self.thread_rows(tid)
        turns = [self.turn_view(x) for x in rows]
        first, last = turns[0], turns[-1]
        return {"thread": {"id": tid, "title": title_of(first["text"]), "engine": last["engine"], "node_id": last["node_id"],
                           "status": last["status"], "project_id": first["project_id"], "cwd": last["cwd"],
                           "resumable": bool(last["session"]) and last["engine"] in ("claude", "codex"),
                           "can_reply": last["status"] not in ACTIVE and last["engine"] not in ("action", "admin")},
                "turns": turns}


def register(app: Any, ctx: Any) -> None:
    from fastapi import Body, Depends, Query
    from fastapi.responses import Response

    chat = Chat(ctx)
    app.state.chat = chat
    ctx.on_tick.append(chat.prune)

    @app.post("/api/uploads", dependencies=[Depends(ctx.require_ui)])
    def upload(body: dict = Body(...)) -> dict:
        return chat.save_upload(str(body.get("name") or "image"), body.get("data") or "")

    @app.get("/api/uploads/{uid}", dependencies=[Depends(ctx.require_ui)])
    def get_upload(uid: str) -> Response:
        data, mime = chat.upload_bytes(uid)
        return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=86400",
                                                        "X-Content-Type-Options": "nosniff"})

    @app.get("/api/agent/uploads/{uid}", dependencies=[Depends(ctx.require_agent)])
    def agent_upload(uid: str) -> Response:
        data, mime = chat.upload_bytes(uid)
        return Response(data, media_type=mime)

    @app.get("/api/threads", dependencies=[Depends(ctx.require_ui)])
    def threads(limit: int = Query(default=50, ge=1, le=200), q: str = Query(default="", max_length=200)) -> dict:
        return chat.threads(limit, q)

    @app.get("/api/threads/{tid}", dependencies=[Depends(ctx.require_ui)])
    def thread(tid: str) -> dict:
        return chat.thread(tid)
