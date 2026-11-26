"""Workstream N (CONTRACT-phase3.md §5 N): alert engine, ntfy + Web Push notifiers, dead-man switch, overnight brief.

All notifier HTTP goes through a fake transport (no network), except the last test, which runs the real httpx
transport against a local fake ntfy / push / healthchecks server on port 19180 (N's range).
"""
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import forge_alerts
import forge_webpush as wp
from p2util import AGENT, UI, get_job, job, make_hub
from p2util import report as _p2_report


def report(c, node, cpu=10, **kw):
    """A 2.3 agent report (S1: it carries metrics.job_token, so no job_token_unknown alert is expected)."""
    kw.setdefault("metrics", {"cpu": cpu, "job_token": "filtered"})
    return _p2_report(c, node, cpu=cpu, **kw)

MIN = 60


class FakeTransport:
    def __init__(self):
        self.ntfy_msgs, self.pushes, self.gets = [], [], []
        self.push_code, self.get_code, self.ntfy_error = 201, 200, None

    def ntfy(self, server, msg):
        if self.ntfy_error:
            raise RuntimeError(self.ntfy_error)
        self.ntfy_msgs.append((server, msg))

    def push(self, endpoint, headers, body):
        self.pushes.append((endpoint, headers, body))
        return self.push_code

    def get(self, url):
        self.gets.append(url)
        return self.get_code


def hub(tmp_path, cfg=None, ntfy=True):
    c, app, clock = make_hub(tmp_path, {"push_host_allow": ["push.example.com"]} | (cfg or {}))
    eng = app.state.alerts
    eng.inline, eng.transport = True, FakeTransport()
    if ntfy:
        r = c.put("/api/notify/settings", headers=UI, json={"ntfy": {"enabled": True, "topic": "hm-testtopic123"}})
        assert r.status_code == 200, r.text
    return c, app, clock, eng, eng.transport


def settle(app, clock, secs=31):
    clock.advance(secs)
    app.state.tick()


def open_alerts(c):
    return c.get("/api/alerts", headers=UI).json()["alerts"]


def run_job(c, node="n1", status="failed", **kw):
    jid = job(c, node, "shell", prompt=kw.pop("prompt", "echo build"), **kw)["id"]
    items = report(c, node, cap=2, tools=("shell",)).json()["jobs"]
    assert [j["id"] for j in items] == [jid]
    r = c.post(f"/api/agent/jobs/{jid}/update", headers=AGENT,
               json={"status": status, "exit_code": 1 if status == "failed" else 0, "append": "boom\n", "node_id": node})
    assert r.status_code == 200
    return jid


# ---------------------------------------------------------------- N1 engine
def test_offline_node_opens_one_alert_and_auto_resolves_after_two_clean_ticks(tmp_path):
    # Arrange
    c, app, clock, eng, _ = hub(tmp_path)
    report(c, "n1")
    settle(app, clock, 40)                           # n1 silent for 40 s -> offline
    # Act: several ticks with the condition still true
    for _ in range(3):
        settle(app, clock, 3)
    rows = open_alerts(c)
    # Assert: one row per key
    assert [(a["kind"], a["node_id"], a["key"]) for a in rows] == [("node_offline", "n1", "node_offline:n1")]
    assert rows[0]["action"] == {"label": "Wake machine", "kind": "wake_machine", "target": {"node_id": "n1"}}
    # Act: n1 comes back; one tick without the condition keeps it, the second resolves it
    report(c, "n1")
    settle(app, clock, 1)
    assert len(open_alerts(c)) == 1
    report(c, "n1")
    settle(app, clock, 1)
    # Assert
    assert open_alerts(c) == []
    closed = c.get("/api/alerts?open=0", headers=UI).json()["alerts"]
    assert closed[0]["resolved_by"] == "auto" and closed[0]["resolved"] is not None


def test_health_alerts_keep_22_shape_and_gain_n1_fields(tmp_path):
    # Arrange
    c, app, clock, eng, _ = hub(tmp_path)
    report(c, "n1")
    c.post("/api/agent/report", headers=AGENT, json={"node_id": "n2", "capacity": 1,
                                                     "metrics": {"disks": [{"free_gb": 1, "total_gb": 100}]}})
    settle(app, clock, 40)
    c.post("/api/agent/report", headers=AGENT, json={"node_id": "n2", "capacity": 1,
                                                     "metrics": {"disks": [{"free_gb": 1, "total_gb": 100}]}})
    settle(app, clock, 1)
    # Act
    h = c.get("/api/health", headers=UI).json()
    # Assert
    by_kind = {a["kind"]: a for a in h["alerts"]}
    assert {"node_offline", "disk_full"} <= set(by_kind)
    for a in h["alerts"]:
        assert {"level", "kind", "node_id", "msg", "id", "opened", "self_healing", "action"} <= set(a)
        assert a["id"] and a["opened"]
    assert by_kind["disk_full"]["action"]["kind"] == "open_machine"


def test_ack_resolves_by_user_and_condition_reopens_only_after_an_hour(tmp_path):
    # Arrange
    c, app, clock, eng, _ = hub(tmp_path)
    report(c, "n1")
    settle(app, clock, 40)
    aid = open_alerts(c)[0]["id"]
    # Act
    assert c.post(f"/api/alerts/{aid}/ack", headers=UI).json() == {"ok": True}
    settle(app, clock, 30 * MIN)
    # Assert: still offline, but acked less than 1 h ago -> stays closed
    assert open_alerts(c) == []
    assert c.post("/api/alerts/nope/ack", headers=UI).status_code == 404
    # Act: an hour later it reopens as a new row
    settle(app, clock, 31 * MIN)
    rows = open_alerts(c)
    # Assert
    assert len(rows) == 1 and rows[0]["id"] != aid


def test_alerts_need_ui_auth_and_since_filters_by_update_time(tmp_path):
    # Arrange
    c, app, clock, eng, _ = hub(tmp_path)
    report(c, "n1")
    settle(app, clock, 40)
    t = clock()
    # Act / Assert
    assert c.get("/api/alerts").status_code == 401
    assert len(c.get(f"/api/alerts?since={t - 1}", headers=UI).json()["alerts"]) == 1
    assert c.get(f"/api/alerts?since={t + 1}", headers=UI).json()["alerts"] == []


def test_engine_runs_only_on_a_settled_leader(tmp_path):
    # Arrange: brand-new leader inside its 30 s grace
    c, app, clock, eng, _ = hub(tmp_path)
    app.state.store.x("INSERT INTO nodes(id,last_seen,online,updated) VALUES('n1', 0, 0, 1)")
    # Act
    settle(app, clock, 5)
    # Assert
    assert open_alerts(c) == []
    # Act: past the grace
    settle(app, clock, 30)
    assert len(open_alerts(c)) == 1


def test_standby_raise_alert_and_notify_are_no_ops(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path)
    ctx = app.state.ctx
    app.state.cluster.leader_id = "someone-else"
    # Act
    aid = ctx.raise_alert("rolled_back", node_id="n1", level="error", msg="x")
    ctx.notify("rolled_back", "t", "b", node_id="n1")
    # Assert
    assert aid is None and fake.ntfy_msgs == [] and ctx.resolve_alert("rolled_back:n1") is False


# ---------------------------------------------------------------- N2 rules
def test_machine_down_notifies_after_ten_minutes_once(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, {"public_url": "https://hub1.tail1234.ts.net:8787/"})
    report(c, "n1")
    settle(app, clock, 40)
    # Act: 9 minutes in
    for _ in range(3):
        settle(app, clock, 3 * MIN)
    # Assert
    assert fake.ntfy_msgs == []
    # Act: past 10 min, then many more ticks
    settle(app, clock, 2 * MIN)
    settle(app, clock, 3)
    settle(app, clock, 3)
    # Assert: exactly one notification, with a click link into the UI
    assert len(fake.ntfy_msgs) == 1
    server, msg = fake.ntfy_msgs[0]
    assert server == "https://ntfy.sh" and msg["topic"] == "hm-testtopic123"
    assert msg["title"] == "n1 is down" and "not recovering" in msg["message"]
    assert msg["click"] == "https://hub1.tail1234.ts.net:8787/#/machine/n1" and msg["priority"] == 4
    row = open_alerts(c)[0]
    assert row["last_notified"] is not None


def test_flapping_alert_notifies_at_most_once_per_key_per_6h(tmp_path):
    # Arrange: one machine_down notification
    c, app, clock, eng, fake = hub(tmp_path, {"machine_down_notify_s": 60})
    report(c, "n1")
    settle(app, clock, 40)
    settle(app, clock, 2 * MIN)
    assert len(fake.ntfy_msgs) == 1
    # Act: it recovers, goes down again 1 h later for 20 min
    report(c, "n1"); settle(app, clock, 1); report(c, "n1"); settle(app, clock, 1)
    assert open_alerts(c) == []
    clock.advance(MIN * 60)
    settle(app, clock, 40)
    settle(app, clock, 20 * MIN)
    # Assert: suppressed by the 6 h dedupe
    assert len(fake.ntfy_msgs) == 1
    assert open_alerts(c)[0]["data"]["notify_state"] == "suppressed:dedup"


def test_self_healing_offline_during_own_update_waits_for_grace(tmp_path):
    # Arrange: n1 holds the update lease (it is restarting into a new version)
    c, app, clock, eng, fake = hub(tmp_path)
    report(c, "n1")
    app.state.store.x("INSERT OR REPLACE INTO leases(id,node_id,version,granted,expires,updated) VALUES(?,?,?,?,?,?)",
                      ("lease", "n1", "2.3.0", clock(), clock() + 7200, clock()))
    assert app.state.lease.current() is not None
    settle(app, clock, 40)
    # Act: 12 minutes down (past machine_down_notify_s, inside the 15 min grace)
    for _ in range(4):
        settle(app, clock, 3 * MIN)
    # Assert
    assert open_alerts(c)[0]["self_healing"] is True and fake.ntfy_msgs == []
    # Act: past 15 min
    settle(app, clock, 4 * MIN)
    # Assert
    assert len(fake.ntfy_msgs) == 1


def test_failed_user_task_raises_alert_and_notifies(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path)
    report(c, "n1", cap=2, tools=("shell",))
    # Act
    jid = run_job(c, prompt="npm run build\nwith token sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    # Assert
    rows = open_alerts(c)
    assert [(a["kind"], a["key"]) for a in rows] == [("task_failed", f"task_failed:n1:{jid}")]
    assert rows[0]["action"] == {"label": "Retry", "kind": "retry_job", "target": {"job_id": jid}}
    assert "boom" in rows[0]["data"]["excerpt"] and "sk-ant" not in rows[0]["msg"]
    assert len(fake.ntfy_msgs) == 1 and fake.ntfy_msgs[0][1]["title"] == "Task failed on n1"
    # health lists raised alerts too
    assert any(a["kind"] == "task_failed" for a in c.get("/api/health", headers=UI).json()["alerts"])


def test_task_alerts_expire_after_a_day(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path)
    report(c, "n1", cap=2, tools=("shell",))
    run_job(c)
    # Act
    clock.advance(24 * 3600 + 1)
    report(c, "n1", cap=2, tools=("shell",))
    app.state.tick()
    # Assert
    assert open_alerts(c) == []


@pytest.mark.parametrize("kw", [{"source": "brief"}, {"source": "sync"}])
def test_brief_and_sync_failures_do_not_notify(tmp_path, kw):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path)
    report(c, "n1", cap=2, tools=("shell",))
    # Act
    run_job(c, **kw)
    # Assert
    assert open_alerts(c) == [] and fake.ntfy_msgs == []


def test_system_lane_failure_does_not_notify(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path)
    ctx = app.state.ctx
    # Act
    eng.on_job_finished({"id": "j1", "node_id": "n1", "run_as": "system", "source": "ui", "prompt": "x"}, "failed", "",
                        clock())
    # Assert
    assert open_alerts(c) == [] and fake.ntfy_msgs == [] and ctx.notify is not None


def test_finished_task_notifies_only_with_notify_flag(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path)
    report(c, "n1", cap=2, tools=("shell",))
    # Act
    quiet = run_job(c, status="done")
    loud = run_job(c, status="done", notify=True)
    # Assert
    assert get_job(c, quiet)["notify"] == 0 and get_job(c, loud)["notify"] == 1
    assert [m["title"] for _, m in fake.ntfy_msgs] == ["Task finished on n1"]
    assert [a["kind"] for a in open_alerts(c)] == ["task_finished"]


def test_rules_switch_off_a_category(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path)
    assert c.put("/api/notify/settings", headers=UI, json={"rules": {"task_failed": False}}).json()["rules"] == \
        {"task_failed": False, "task_finished": True, "machine_down": True, "autorepair": True}
    report(c, "n1", cap=2, tools=("shell",))
    # Act
    run_job(c)
    # Assert: the alert is there, the notification is not
    assert open_alerts(c)[0]["data"]["notify_state"] == "suppressed:rule" and fake.ntfy_msgs == []


def test_raise_resolve_and_direct_notify_slots(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path)
    ctx = app.state.ctx
    # Act: R raises a rollback alert (autorepair -> notifies), F raises a non-notifying kind
    a1 = ctx.raise_alert("rolled_back", node_id="n1", level="error", msg="2.3.1 rolled back on n1",
                         action={"label": "Open", "kind": "open_machine", "target": {"node_id": "n1"}})
    a2 = ctx.raise_alert("repo_conflict", node_id="n2", msg="conflict", data={"cwd": "C:/w"})
    again = ctx.raise_alert("rolled_back", node_id="n1", level="error", msg="2.3.1 rolled back on n1")
    ctx.notify("rolled_back", "Rolled back", "again", node_id="n1")        # same key -> deduplicated
    ctx.notify("upkeep_failed", "Upkeep failed", "winget", node_id="n2")    # new key -> sent
    ctx.notify("repo_conflict", "x", "y", node_id="n2")                      # not a notifying kind
    # Assert
    assert a1 == again and a1 != a2
    assert [m["title"] for _, m in fake.ntfy_msgs] == ["Update rolled back on n1", "Upkeep failed"]
    rows = {a["key"]: a for a in open_alerts(c)}
    assert rows["rolled_back:n1"]["action"]["kind"] == "open_machine"
    assert rows["repo_conflict:n2"]["data"]["cwd"] == "C:/w"
    # raised alerts survive ticks (no auto-resolve without a condition)
    settle(app, clock, 3); settle(app, clock, 3); settle(app, clock, 3)
    assert {"rolled_back:n1", "repo_conflict:n2"} <= set(a["key"] for a in open_alerts(c))
    # Act: resolve
    assert ctx.resolve_alert("repo_conflict:n2") is True and ctx.resolve_alert("repo_conflict:n2") is False
    assert "repo_conflict:n2" not in {a["key"] for a in open_alerts(c)}


def test_alert_source_self_healing_kind_never_notifies_before_grace(tmp_path):
    # Arrange: F reports a restarting service; it is self-healing by kind
    c, app, clock, eng, fake = hub(tmp_path)
    app.state.ctx.alert_sources.append(lambda now: [{"level": "warn", "kind": "service_restarting", "node_id": "n1",
                                                     "msg": "ollama restarting"}])
    # Act
    settle(app, clock, 40)
    # Assert
    a = [x for x in open_alerts(c) if x["kind"] == "service_restarting"][0]
    assert a["self_healing"] is True and fake.ntfy_msgs == []


def test_no_channel_means_suppressed_not_queued(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    report(c, "n1", cap=2, tools=("shell",))
    # Act
    run_job(c)
    # Assert
    assert open_alerts(c)[0]["data"]["notify_state"] == "suppressed:no_channel"


def test_ntfy_error_is_contained(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path)
    fake.ntfy_error = "connection refused"
    report(c, "n1", cap=2, tools=("shell",))
    # Act
    jid = run_job(c)
    # Assert: the job update still succeeded and the alert exists
    assert get_job(c, jid)["status"] == "failed" and len(open_alerts(c)) == 1
    r = c.post("/api/notify/test", headers=UI, json={"channel": "ntfy"}).json()
    assert r["ok"] is False and "connection refused" in r["results"]["ntfy"]


# ---------------------------------------------------------------- N2 settings
def test_settings_defaults_validation_and_topic(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    # Act
    s = c.get("/api/notify/settings", headers=UI).json()
    # Assert defaults
    assert s["ntfy"] == {"enabled": False, "server": "https://ntfy.sh", "topic": None, "subscribe_url": None}
    assert s["webpush"]["enabled"] is True and s["webpush"]["devices"] == 0
    assert len(wp.b64u_dec(s["webpush"]["vapid_public_key"])) == 65
    assert s["deadman"] == {"enabled": False, "url": None, "send_fail": False, "last_ping": None, "last_ok": None,
                            "last_error": None}
    # topic suggestion is not saved
    t = c.post("/api/notify/ntfy/topic", headers=UI).json()
    assert t["topic"].startswith("hm-") and len(t["topic"]) == 23 and t["subscribe_url"] == f"https://ntfy.sh/{t['topic']}"
    assert c.get("/api/notify/settings", headers=UI).json()["ntfy"]["topic"] is None
    # validation
    for bad in ({"ntfy": {"topic": "short"}}, {"ntfy": {"topic": "has space here"}}, {"ntfy": {"server": "ftp://x"}},
                {"ntfy": {"enabled": True}}, {"deadman": {"url": "nope"}}, {"deadman": {"enabled": True}},
                {"rules": {"bogus": True}}, {"rules": {"task_failed": "yes"}}, {"ntfy": "x"}):
        assert c.put("/api/notify/settings", headers=UI, json=bad).status_code == 400, bad
    # partial update returns the full shape
    r = c.put("/api/notify/settings", headers=UI,
              json={"ntfy": {"server": "https://ntfy.example.org/", "topic": t["topic"], "enabled": True}}).json()
    assert r["ntfy"] == {"enabled": True, "server": "https://ntfy.example.org", "topic": t["topic"],
                         "subscribe_url": f"https://ntfy.example.org/{t['topic']}"}
    assert set(r) == {"ntfy", "webpush", "deadman", "rules"}
    assert c.put("/api/notify/settings", json={}).status_code == 401


def test_test_endpoint_reports_per_channel(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path)
    # Act
    r = c.post("/api/notify/test", headers=UI, json={"channel": "all"}).json()
    # Assert: ntfy ok; Web Push has no device, so "all" does not count it (N-07)
    assert r == {"ok": True, "results": {"ntfy": "ok"}}
    assert c.post("/api/notify/test", headers=UI, json={"channel": "webpush"}).json() ==         {"ok": False, "results": {"webpush": {"sent": 0, "failed": 0}}}
    r = c.post("/api/notify/test", headers=UI).json()
    assert fake.ntfy_msgs[-1][1]["title"] == "Hivemind test"
    assert c.post("/api/notify/test", headers=UI, json={"channel": "sms"}).status_code == 400
    assert c.post("/api/notify/test", headers=UI, json={"channel": "ntfy"}).json() == {"ok": True,
                                                                                      "results": {"ntfy": "ok"}}


# ---------------------------------------------------------------- N2 Web Push
def browser_subscription():
    from cryptography.hazmat.primitives.asymmetric import ec
    import os
    key = ec.generate_private_key(ec.SECP256R1())
    return key, wp.b64u(wp._public_bytes(key.public_key())), wp.b64u(os.urandom(16))


def verify_vapid(headers, endpoint, pub_b64):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    auth = headers["Authorization"]
    assert auth.startswith("vapid t=")
    jwt, k = auth[len("vapid t="):].split(", k=")
    assert k == pub_b64
    h, p, s = jwt.split(".")
    claims = json.loads(wp.b64u_dec(p))
    assert claims["aud"] == "https://push.example.com" and claims["sub"].startswith("mailto:")
    raw = wp.b64u_dec(s)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), wp.b64u_dec(pub_b64))
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256()))


def test_webpush_subscribe_send_decrypt_and_unsubscribe(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    ua_key, p256dh, auth = browser_subscription()
    endpoint = "https://push.example.com/send/abc123"
    # Act
    r = c.post("/api/push/subscribe", headers=UI, json={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth},
                                                       "label": "Pixel"})
    again = c.post("/api/push/subscribe", headers=UI, json={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}})
    settings = c.get("/api/notify/settings", headers=UI).json()
    report(c, "n1", cap=2, tools=("shell",))
    jid = run_job(c)
    # Assert
    assert r.json()["ok"] and r.json()["id"] == again.json()["id"] and len(r.json()["id"]) == 32
    assert settings["webpush"]["devices"] == 1
    (ep, headers, body), = fake.pushes
    assert ep == endpoint and headers["Content-Encoding"] == "aes128gcm" and headers["Urgency"] == "high"
    verify_vapid(headers, endpoint, settings["webpush"]["vapid_public_key"])
    payload = json.loads(wp.decrypt(body, ua_key, auth))
    assert payload == {"title": "Task failed on n1", "body": payload["body"], "url": f"#/task/{jid}",
                       "tag": f"task_failed:n1:{jid}"}
    # Act: unsubscribe
    d = c.request("DELETE", "/api/push/subscribe", headers=UI, json={"endpoint": endpoint}).json()
    # Assert
    assert d == {"ok": True, "removed": True}
    assert c.get("/api/notify/settings", headers=UI).json()["webpush"]["devices"] == 0


def test_webpush_gone_subscription_is_removed_and_bad_input_rejected(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    _, p256dh, auth = browser_subscription()
    c.post("/api/push/subscribe", headers=UI, json={"endpoint": "https://push.example.com/x", "keys": {"p256dh": p256dh,
                                                                                                 "auth": auth}})
    fake.push_code = 410
    # Act
    r = c.post("/api/notify/test", headers=UI, json={"channel": "webpush"}).json()
    # Assert
    assert r == {"ok": False, "results": {"webpush": {"sent": 0, "failed": 1}}}
    assert c.get("/api/notify/settings", headers=UI).json()["webpush"]["devices"] == 0
    for bad in ({"endpoint": "http://push.example.com/x", "keys": {"p256dh": p256dh, "auth": auth}},
                {"endpoint": "https://push.example.com/x", "keys": {"p256dh": "AAAA", "auth": auth}},
                {"endpoint": "https://push.example.com/x", "keys": {"p256dh": p256dh, "auth": "AA"}},
                {"endpoint": "https://push.example.com/x"}):
        assert c.post("/api/push/subscribe", headers=UI, json=bad).status_code == 400


def test_vapid_keys_are_created_once_and_replicated_via_secrets(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    # Act
    k1 = c.get("/api/notify/settings", headers=UI).json()["webpush"]["vapid_public_key"]
    k2 = c.get("/api/notify/settings", headers=UI).json()["webpush"]["vapid_public_key"]
    rows = {r["name"]: r["value"] for r in app.state.store.q("SELECT name,value FROM secrets")}
    # Assert
    assert k1 == k2 == rows["vapid_public_key"] and wp.public_from_private(rows["vapid_private_key"]) == k1


def test_encrypt_matches_rfc8291_example():
    # Arrange: RFC 8291 §5 test vector
    from cryptography.hazmat.primitives.asymmetric import ec
    as_priv = ec.derive_private_key(int.from_bytes(wp.b64u_dec("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
                                    ec.SECP256R1())
    ua_pub = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
    auth = "BTBZMqHH6r4Tts7J_aSIgg"
    salt = wp.b64u_dec("DGv6ra1nlYgDCS1FRnbzlw")
    # Act
    body = wp.encrypt(b"When I grow up, I want to be a watermelon", ua_pub, auth, salt=salt, as_private=as_priv)
    # Assert
    assert wp.b64u(body) == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


# ---------------------------------------------------------------- N3 dead-man switch
def test_deadman_pings_every_interval_and_fail_url_with_open_error(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    r = c.put("/api/notify/settings", headers=UI, json={"deadman": {"enabled": True, "url": "https://hc-ping.com/abc"}})
    assert r.status_code == 200
    # Act: settle, then ticks within one interval
    settle(app, clock, 31)
    settle(app, clock, 100)
    settle(app, clock, 100)
    # Assert
    assert fake.gets == ["https://hc-ping.com/abc"]
    dm = c.get("/api/notify/settings", headers=UI).json()["deadman"]
    assert dm["last_ping"] == dm["last_ok"] == pytest.approx(clock() - 200)
    # Act: next interval, with send_fail and an open error alert
    c.put("/api/notify/settings", headers=UI, json={"deadman": {"send_fail": True}})
    app.state.ctx.raise_alert("rolled_back", node_id="n1", level="error", msg="x")
    settle(app, clock, 1)
    fake.get_code = 500
    settle(app, clock, 301)
    # Assert
    assert fake.gets[1:] == ["https://hc-ping.com/abc/fail", "https://hc-ping.com/abc/fail"]
    assert c.get("/api/notify/settings", headers=UI).json()["deadman"]["last_error"] == "HTTP 500"


# ---------------------------------------------------------------- N4 morning brief
def test_overnight_items_in_brief_endpoint_and_rollup_prompt(tmp_path):
    # Arrange: clock at 07:30 local; one alert at 02:00, one task finished at 03:00, one old task yesterday 12:00
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    morning = datetime.now().astimezone().replace(hour=7, minute=30, second=0, microsecond=0).timestamp()
    clock.t = morning - 19.5 * 3600            # yesterday 12:00
    report(c, "n1", cap=2, tools=("shell",))
    run_job(c, status="done", prompt="old task")
    clock.t = morning - 5.5 * 3600             # 02:00
    report(c, "n1", cap=2, tools=("shell",))
    app.state.ctx.raise_alert("rolled_back", node_id="n1", level="error", msg="2.3.1 rolled back on n1")
    clock.t = morning - 4.5 * 3600             # 03:00
    report(c, "n1", cap=2, tools=("shell",))
    run_job(c, status="done", prompt="nightly refactor of the parser " * 5)
    clock.t = morning
    # Act
    o = c.get("/api/brief/all", headers=UI).json()
    prompt, _ = app.state.p2._rollup_prompt(clock())
    # Assert
    assert o["brief"] is None and o["text"] is None
    assert [(a["kind"], a["node_id"]) for a in o["overnight"]["alerts"]] == [("rolled_back", "n1")]
    fin = o["overnight"]["finished"]
    assert len(fin) == 1 and fin[0]["status"] == "done" and len(fin[0]["prompt"]) == 80
    assert "Overnight" in prompt and "rolled_back on n1 at 02:00 (still open)" in prompt and "task done on n1 at 03:00" in prompt
    assert "old task" not in prompt


def test_overnight_since_is_18h_previous_day():
    # Arrange
    now = datetime(2026, 11, 25, 7, 30).astimezone().timestamp()
    # Act
    since = datetime.fromtimestamp(forge_alerts.overnight_since(now))
    # Assert
    assert (since.day, since.hour, since.minute) == (24, 18, 0)


# ---------------------------------------------------------------- retention
def test_resolved_alerts_are_deleted_after_30_days(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    ctx = app.state.ctx
    ctx.raise_alert("repo_conflict", node_id="n1", msg="x")
    ctx.resolve_alert("repo_conflict:n1")
    settle(app, clock, 31)
    assert len(c.get("/api/alerts?open=0", headers=UI).json()["alerts"]) == 1
    # Act
    settle(app, clock, 31 * 86400)
    # Assert
    assert c.get("/api/alerts?open=0", headers=UI).json()["alerts"] == []


# ---------------------------------------------------------------- real HTTP transport (port 19180)
class _Sink(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _record(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Sink.seen.append((self.command, self.path, dict(self.headers), self.rfile.read(n) if n else b""))

    def do_POST(self):
        self._record()
        self.send_response(201 if self.path.startswith("/push/") else 200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._record()
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")


def test_http_transport_against_local_fake_services(tmp_path):
    # Arrange
    srv = ThreadingHTTPServer(("127.0.0.1", 19180), _Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Sink.seen = []
    try:
        t = forge_alerts.HttpTransport()
        # Act
        t.ntfy("http://127.0.0.1:19180", {"topic": "hm-abcdefgh", "title": "Täst ·", "message": "hello"})
        code = t.push("http://127.0.0.1:19180/push/1", {"TTL": "60", "Content-Encoding": "aes128gcm"}, b"\x01\x02")
        get = t.get("http://127.0.0.1:19180/ping/abc")
    finally:
        srv.shutdown()
        srv.server_close()
    # Assert
    (m1, p1, h1, b1), (m2, p2, h2, b2), (m3, p3, _, _) = _Sink.seen
    assert (m1, p1) == ("POST", "/") and json.loads(b1)["title"] == "Täst ·"
    assert (m2, p2, b2, h2["TTL"], code) == ("POST", "/push/1", b"\x01\x02", "60", 201)
    assert (m3, p3, get) == ("GET", "/ping/abc", 200)


def test_background_worker_delivers_without_blocking(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path)
    eng.inline = False
    done = threading.Event()
    orig = fake.ntfy

    def ntfy(server, msg):
        orig(server, msg)
        done.set()
    fake.ntfy = ntfy
    report(c, "n1", cap=2, tools=("shell",))
    # Act
    run_job(c)
    # Assert
    assert done.wait(5) and len(fake.ntfy_msgs) == 1



# ---------------------------------------------------------------- review fixes (N-01 .. N-10)
def subscribe(c, endpoint="https://push.example.com/x", session_id=None, app=None):
    _, p256dh, auth = browser_subscription()
    r = c.post("/api/push/subscribe", headers=UI, json={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}})
    assert r.status_code == 200, r.text
    if session_id is not None:
        app.state.store.x("UPDATE push_subs SET session_id=? WHERE id=?", (session_id, r.json()["id"]))
    return r.json()["id"]


def devices(c):
    return c.get("/api/notify/settings", headers=UI).json()["webpush"]["devices"]


def test_push_transport_errors_never_delete_a_subscription(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    subscribe(c)

    def boom(*a):
        raise RuntimeError("network unreachable")
    fake.push = boom
    # Act: far more than the old 5-failure limit
    results = [eng.send_push({"title": "t", "body": "b"}) for _ in range(30)]
    # Assert
    assert results[-1] == {"sent": 0, "failed": 1}
    assert devices(c) == 1
    assert app.state.store.q("SELECT fails FROM push_subs")[0]["fails"] == 0


def test_push_unavailable_or_5xx_keeps_subscriptions(tmp_path, monkeypatch):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    subscribe(c)
    # Act: cryptography missing on this hub
    monkeypatch.setattr(wp, "available", lambda: False)
    r1 = [eng.send_push({"title": "t"}) for _ in range(10)]
    monkeypatch.undo()
    fake.push_code = 503
    r2 = [eng.send_push({"title": "t"}) for _ in range(10)]
    # Assert
    assert r1[-1] == {"sent": 0, "failed": 0} and fake.pushes and r2[-1] == {"sent": 0, "failed": 1}
    assert devices(c) == 1


def test_push_rejections_drop_a_sub_only_after_a_day_and_many_tries(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    subscribe(c)
    fake.push_code = 403
    # Act: 25 rejections within the first day
    for _ in range(25):
        eng.send_push({"title": "t"})
    # Assert
    assert devices(c) == 1
    # Act: a day later, still rejected
    clock.advance(25 * 3600)
    eng.send_push({"title": "t"})
    # Assert
    assert devices(c) == 0


def test_acked_raised_alert_stays_closed_for_an_hour(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    ctx = app.state.ctx
    aid = ctx.raise_alert("repo_conflict", node_id="n1", msg="conflict")
    assert c.post(f"/api/alerts/{aid}/ack", headers=UI).status_code == 200
    # Act: F raises it again on its next tick
    again = ctx.raise_alert("repo_conflict", node_id="n1", msg="conflict")
    # Assert
    assert again is None and open_alerts(c) == []
    # Act: an hour later it is still true
    clock.advance(3601)
    later = ctx.raise_alert("repo_conflict", node_id="n1", msg="conflict")
    # Assert
    assert later and later != aid and [a["id"] for a in open_alerts(c)] == [later]


def test_subscribe_without_keys_is_503_and_leader_creates_keys_on_tick(tmp_path, monkeypatch):
    # Arrange: keys not there yet and this hub may not create them (as on a standby)
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    _, p256dh, auth = browser_subscription()

    def no_keys():
        raise wp.WebPushError("VAPID keys are created by the leader")
    monkeypatch.setattr(eng, "vapid_keys", no_keys)
    # Act
    r = c.post("/api/push/subscribe", headers=UI, json={"endpoint": "https://push.example.com/x",
                                                       "keys": {"p256dh": p256dh, "auth": auth}})
    # Assert
    assert r.status_code == 503 and "retry" in r.json()["detail"]
    # Arrange / Act: as the settled leader, one tick creates the keys without anyone asking
    monkeypatch.undo()
    assert not app.state.store.q("SELECT 1 FROM secrets WHERE name LIKE 'vapid_%'")
    settle(app, clock, 40)
    # Assert
    names = {x["name"] for x in app.state.store.q("SELECT name FROM secrets WHERE name LIKE 'vapid_%'")}
    assert names == {"vapid_private_key", "vapid_public_key"}


def test_second_outage_inside_dedup_window_notifies_once_window_passes(tmp_path):
    # Arrange: notified at T, flap at T+1h
    c, app, clock, eng, fake = hub(tmp_path, {"machine_down_notify_s": 60})
    report(c, "n1")
    settle(app, clock, 40)
    settle(app, clock, 2 * MIN)
    assert len(fake.ntfy_msgs) == 1
    report(c, "n1"); settle(app, clock, 1); report(c, "n1"); settle(app, clock, 1)
    clock.advance(60 * MIN)
    settle(app, clock, 40)
    settle(app, clock, 2 * MIN)
    assert len(fake.ntfy_msgs) == 1 and open_alerts(c)[0]["data"]["notify_state"] == "suppressed:dedup"
    # Act: still down past T+6h
    clock.advance(5 * 3600)
    settle(app, clock, 3)
    settle(app, clock, 3)
    # Assert: exactly one more notification
    assert len(fake.ntfy_msgs) == 2 and open_alerts(c)[0]["data"]["notify_state"] == "sent"


def test_revoked_or_expired_session_loses_its_push_devices(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    st = app.state.store
    live, revoked = "a" * 64, "b" * 64
    for sid, rev in ((live, 0), (revoked, 1)):
        st.x("INSERT INTO ui_sessions(id,label,created,last_seen,expires,revoked,updated) VALUES(?,?,?,?,?,?,?)",
             (sid, "d", clock(), clock(), clock() + 86400 * 30, rev, clock()))
    keep = subscribe(c, "https://push.example.com/live", live, app)
    subscribe(c, "https://push.example.com/revoked", revoked, app)
    young = subscribe(c, "https://push.example.com/young", "c" * 64, app)   # session row not replicated yet
    legacy = subscribe(c, "https://push.example.com/legacy", "legacy", app)
    app.state.ctx.ui_auth = lambda request: {"session_id": None}            # S sessions active from here on
    # Act
    settle(app, clock, 40)
    # Assert
    assert {r["id"] for r in st.q("SELECT id FROM push_subs")} == {keep, young, legacy}
    # Act: an hour later the missing session counts as gone
    eng._last_housekeep = None
    settle(app, clock, 3700)
    # Assert
    assert {r["id"] for r in st.q("SELECT id FROM push_subs")} == {keep, legacy}
    assert eng.on_session_revoked(live) == 1
    assert st.q("SELECT COUNT(*) n FROM push_subs")[0]["n"] == 1


def test_overnight_keeps_failures_and_newest_and_skips_system_lane(tmp_path):
    # Arrange: 40 user jobs done overnight, one failed early, one system-lane job
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    morning = datetime.now().astimezone().replace(hour=7, minute=30, second=0, microsecond=0).timestamp()
    st = app.state.store
    base = morning - 12 * 3600
    st.x("INSERT INTO jobs(id,node_id,engine,prompt,status,created,finished,updated) VALUES(?,?,?,?,?,?,?,?)",
         ("jfail", "n1", "shell", "early failure", "failed", base, base + 60, base))
    for i in range(40):
        t = base + 600 + i * 600
        st.x("INSERT INTO jobs(id,node_id,engine,prompt,status,created,finished,updated) VALUES(?,?,?,?,?,?,?,?)",
             (f"j{i:02d}", "n1", "shell", f"task {i}", "done", t, t, t))
    st.x("INSERT INTO jobs(id,node_id,engine,prompt,status,created,finished,run_as,updated) VALUES(?,?,?,?,?,?,?,?,?)",
         ("jsys", "n1", "shell", "upkeep", "done", morning - 60, morning - 60, "system", morning))
    clock.t = morning
    # Act
    o = eng.overnight(morning)
    # Assert
    ids = [j["id"] for j in o["finished"]]
    assert len(ids) == 30 and "jfail" in ids and "j39" in ids and "j00" not in ids and "jsys" not in ids
    assert [j["finished"] for j in o["finished"]] == sorted(j["finished"] for j in o["finished"])
    assert o["truncated"] == {"alerts": 0, "finished": 11}
    assert "11 older or resolved items not listed" in eng.digest_text(morning)


def test_condition_msg_with_changing_number_is_rewritten_at_most_every_5_min(tmp_path):
    # Arrange: a condition whose msg changes every tick
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    n = {"i": 0}

    def src(now):
        n["i"] += 1
        return [{"level": "warn", "kind": "watchdog_stale", "node_id": "n9", "msg": f"stale {n['i']}s"}]
    app.state.ctx.alert_sources.append(src)
    settle(app, clock, 40)
    first = open_alerts(c)[0]["msg"]
    # Act: ticks within 5 min
    for _ in range(5):
        settle(app, clock, 10)
    # Assert
    assert open_alerts(c)[0]["msg"] == first
    # Act: past 5 min
    settle(app, clock, 300)
    # Assert
    assert open_alerts(c)[0]["msg"] != first


def test_outbound_url_checks_and_deadman_masking(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    _, p256dh, auth = browser_subscription()
    keys = {"p256dh": p256dh, "auth": auth}
    # Act / Assert: push endpoints
    for ep in ("https://127.0.0.1/x", "https://169.254.169.254/latest", "https://192.168.1.5/x", "https://localhost/x",
               "https://evil.example.org/x", "https://u:p@fcm.googleapis.com/x", "https://fcm.googleapis.com.evil.io/x"):
        assert c.post("/api/push/subscribe", headers=UI, json={"endpoint": ep, "keys": keys}).status_code == 400, ep
    assert c.post("/api/push/subscribe", headers=UI, json={"endpoint": "https://fcm.googleapis.com/fcm/send/abc",
                                                          "keys": keys}).status_code == 200
    # settings URLs: no userinfo, loopback or link-local; a LAN / tailnet ntfy is fine
    for bad in ({"ntfy": {"server": "http://user:pw@ntfy.example.org"}}, {"ntfy": {"server": "http://127.0.0.1:80"}},
                {"deadman": {"url": "http://169.254.169.254/x"}}, {"deadman": {"url": "https://a:b@hc-ping.com/x"}}):
        assert c.put("/api/notify/settings", headers=UI, json=bad).status_code == 400, bad
    assert c.put("/api/notify/settings", headers=UI, json={"ntfy": {"server": "http://100.64.1.2:8080"}}).status_code == 200
    # deadman URL is masked, and sending the masked value back keeps the real one
    secret = "https://hc-ping.com/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    s = c.put("/api/notify/settings", headers=UI, json={"deadman": {"enabled": True, "url": secret}}).json()
    assert s["deadman"]["url"] == "https://hc-ping.com/…e1f0" and "4b5a" not in json.dumps(s)
    c.put("/api/notify/settings", headers=UI, json={"deadman": {"url": s["deadman"]["url"], "send_fail": True}})
    assert eng.pref("notify.deadman")["url"] == secret

    def boom(url):
        raise RuntimeError(f"connect failed for {url}")
    fake.get = boom
    settle(app, clock, 40)
    err = c.get("/api/notify/settings", headers=UI).json()["deadman"]["last_error"]
    assert "connect failed" in err and "4b5a" not in err


def test_deadman_fail_url_keeps_query_and_ignores_task_failures(tmp_path):
    # Arrange
    c, app, clock, eng, fake = hub(tmp_path, ntfy=False)
    c.put("/api/notify/settings", headers=UI,
          json={"deadman": {"enabled": True, "url": "https://hc-ping.com/abc/?create=1", "send_fail": True}})
    report(c, "n1", cap=2, tools=("shell",))
    run_job(c)                                       # open task_failed error alert
    # Act
    clock.advance(35)
    report(c, "n1", cap=2, tools=("shell",))         # n1 stays online
    app.state.tick()
    # Assert: a failed user task does not flip the monitor
    assert fake.gets == ["https://hc-ping.com/abc/?create=1"]
    # Act: an infrastructure error
    app.state.ctx.raise_alert("rolled_back", node_id="n1", level="error", msg="x")
    settle(app, clock, 301)
    # Assert
    assert fake.gets[-1] == "https://hc-ping.com/abc/fail?create=1"
    assert forge_alerts.fail_url("https://hc-ping.com/abc") == "https://hc-ping.com/abc/fail"
