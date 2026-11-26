"""2.4 chat: photo uploads, follow-ups (reply_to) that resume the same CLI session, thread list/detail."""
import base64

import forge_chat as fc
from p2util import AGENT, UI, get_job, job, make_hub, report

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def upload(c, raw=PNG, name="shot.png"):
    return c.post("/api/uploads", json={"name": name, "data": base64.b64encode(raw).decode()}, headers=UI)


def dispatched(c, node, version="2.4.0"):
    return report(c, node, version=version).json().get("jobs", [])


def finish(c, jid, node, status="done", text="answer\n", exit_code=0):
    r = c.post(f"/api/agent/jobs/{jid}/update", headers=AGENT,
               json={"status": "running", "node_id": node, "append": f"[{node}] claude started in C:\\w\n"})
    assert r.status_code == 200, r.text
    r = c.post(f"/api/agent/jobs/{jid}/update", headers=AGENT,
               json={"status": status, "exit_code": exit_code, "node_id": node, "append": text})
    assert r.status_code == 200, r.text


# ---------- pure helpers ----------
def test_sniff_and_names():
    assert fc.sniff_image(PNG) == "image/png" and fc.sniff_image(JPEG) == "image/jpeg"
    assert fc.sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert fc.sniff_image(b"GIF89a....") == "image/gif"
    assert fc.sniff_image(b"<svg onload=alert(1)>") is None
    assert fc.safe_name("../../evil name.exe", "image/png") == "evil-name.png"
    assert fc.safe_name("", "image/jpeg") == "image.jpg"


def test_clean_output_drops_agent_lines_only():
    out = "[n1] claude started in C:\\w\n[hub] requeued\nreal [n1] text\n[other] kept\nfinal"
    assert fc.clean_output(out, "n1") == "real [n1] text\n[other] kept\nfinal"


def test_codex_session_and_carry():
    assert fc.codex_session("x\nsession id: 0199A1B2-0000-4000-8000-0123456789ab\n") == "0199a1b2-0000-4000-8000-0123456789ab"
    assert fc.codex_session("none") is None
    p = fc.carry_prompt([{"text": "q1", "answer": "a1"}], "q2")
    assert "User: q1" in p and "Assistant: a1" in p and p.endswith("q2")


# ---------- uploads ----------
def test_upload_roundtrip_and_validation(tmp_path):
    c, app, _ = make_hub(tmp_path)
    r = upload(c)
    assert r.status_code == 200 and r.json()["mime"] == "image/png" and r.json()["size"] == len(PNG)
    uid = r.json()["id"]
    g = c.get(f"/api/uploads/{uid}", headers=UI)
    assert g.status_code == 200 and g.content == PNG and g.headers["content-type"] == "image/png"
    assert c.get(f"/api/agent/uploads/{uid}", headers=AGENT).content == PNG
    assert c.get(f"/api/uploads/{uid}").status_code == 401                         # UI auth required
    assert upload(c, b"MZ\x90\x00 not an image").status_code == 415
    assert c.post("/api/uploads", json={"data": "%%%"}, headers=UI).status_code == 400
    data_url = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
    assert c.post("/api/uploads", json={"name": "a.jpg", "data": data_url}, headers=UI).json()["mime"] == "image/jpeg"
    assert c.get("/api/uploads/nope!", headers=UI).status_code == 404


def test_job_with_attachments_dispatches_them_to_new_agents_only(tmp_path):
    c, app, _ = make_hub(tmp_path)
    report(c, "n1", version="2.4.0")
    uid = upload(c).json()["id"]
    assert job(c, "n1", attachments=["missingid123"])["_status"] == 400
    j = job(c, "n1", prompt="what is in this photo?", attachments=[uid])
    assert j["_status"] == 200
    assert get_job(c, j["id"])["attachments"][0]["id"] == uid
    # an older agent does not get it (it would silently drop the photo)
    assert dispatched(c, "n1", version="2.3.0") == []
    assert "older than 2.4" in (get_job(c, j["id"])["wait_reason"] or "")
    items = dispatched(c, "n1")
    assert len(items) == 1 and items[0]["attachments"][0]["name"] == "shot.png"
    assert items[0]["session_id"] and get_job(c, j["id"])["session"]["session_id"] == items[0]["session_id"]


# ---------- follow-ups ----------
def test_reply_resumes_same_session_on_same_node_and_folder(tmp_path):
    c, app, _ = make_hub(tmp_path)
    report(c, "n1", version="2.4.0")
    report(c, "n2", version="2.4.0")
    j = job(c, "n1", prompt="first")
    sid = dispatched(c, "n1")[0]["session_id"]
    assert job(c, reply_to=j["id"], prompt="too soon")["_status"] == 409           # still working
    finish(c, j["id"], "n1")
    r = job(c, reply_to=j["id"], prompt="second", engine=None)
    assert r["_status"] == 200 and r["node_id"] == "n1" and r["engine"] == "claude"
    item = dispatched(c, "n1")[0]
    assert item["resume"] == {"tool": "claude", "session_id": sid}
    assert "session_id" not in item and item["scratch_of"] == j["id"] and item["prompt"] == "second"
    finish(c, r["id"], "n1", text="answer two\n")
    # a reply to ANY turn continues the latest one
    r3 = job(c, reply_to=j["id"], prompt="third")
    assert dispatched(c, "n1")[0]["resume"]["session_id"] == sid
    t = c.get(f"/api/threads/{r3['id']}", headers=UI).json()
    assert t["thread"]["id"] == j["id"] and [x["text"] for x in t["turns"]] == ["first", "second", "third"]
    assert t["turns"][1]["answer"] == "answer two"                                 # agent status lines dropped
    lst = c.get("/api/threads", headers=UI).json()["threads"]
    assert lst[0]["id"] == j["id"] and lst[0]["turns"] == 3 and lst[0]["title"] == "first"


def test_reply_with_cwd_keeps_cwd_and_no_scratch(tmp_path):
    c, app, _ = make_hub(tmp_path)
    report(c, "n1", version="2.4.0")
    j = job(c, "n1", prompt="fix it", cwd="C:\\src\\repo")
    dispatched(c, "n1")
    finish(c, j["id"], "n1")
    job(c, reply_to=j["id"], prompt="and test it")
    item = dispatched(c, "n1")[0]
    assert item["cwd"] == "C:\\src\\repo" and "scratch_of" not in item and item["resume"]["tool"] == "claude"


def test_reply_without_session_carries_context(tmp_path):
    c, app, _ = make_hub(tmp_path)
    report(c, "n1", version="2.2.0", tools=("claude", "codex", "ollama"))
    j = job(c, "n1", prompt="old agent question")
    assert "session_id" not in dispatched(c, "n1", version="2.2.0")[0]            # pre-2.4 agent: no session
    finish(c, j["id"], "n1", text="old answer\n")
    r = job(c, reply_to=j["id"], prompt="follow up")
    g = get_job(c, r["id"])
    assert g["resume"] is None and "User: old agent question" in g["prompt"] and "Assistant: old answer" in g["prompt"]
    assert g["chat_text"] == "follow up" and g["thread_id"] == j["id"]


def test_reply_to_codex_uses_printed_session_id(tmp_path):
    c, app, _ = make_hub(tmp_path)
    report(c, "n1", version="2.4.0")
    j = job(c, "n1", engine="codex", prompt="hi")
    dispatched(c, "n1")
    finish(c, j["id"], "n1", text="session id: 11111111-2222-4333-8444-555555555555\nhello\n")
    job(c, reply_to=j["id"], prompt="more")
    assert dispatched(c, "n1")[0]["resume"] == {"tool": "codex", "session_id": "11111111-2222-4333-8444-555555555555"}


def test_shell_reply_runs_next_command_on_same_machine(tmp_path):
    c, app, _ = make_hub(tmp_path)
    report(c, "n1", version="2.4.0")
    j = job(c, "n1", engine="shell", prompt="Get-Date")
    dispatched(c, "n1")
    finish(c, j["id"], "n1")
    r = job(c, reply_to=j["id"], prompt="hostname")
    item = dispatched(c, "n1")[0]
    assert r["node_id"] == "n1" and item["engine"] == "shell" and item["prompt"] == "hostname"


def test_agent_failure_falls_back_to_context_carry(tmp_path):
    c, app, _ = make_hub(tmp_path)
    report(c, "n1", version="2.4.0")
    j = job(c, "n1", prompt="q")
    dispatched(c, "n1")
    finish(c, j["id"], "n1", status="failed", exit_code=-1, text="[n1] engine 'claude' is not installed\n")
    r = job(c, reply_to=j["id"], prompt="again")
    assert get_job(c, r["id"])["resume"] is None


def test_admin_jobs_are_not_threads(tmp_path):
    c, app, _ = make_hub(tmp_path)
    assert c.get("/api/threads", headers=UI).json() == {"threads": []}
    assert c.get("/api/threads/nope", headers=UI).status_code == 404
    assert job(c, reply_to="nope", prompt="x")["_status"] == 404
