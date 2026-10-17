"""Autopilot: research, SEO, drafting, quality gate, trust levels and safety rails."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from stark import autopilot as ap
from stark.core.db import ContentItem
from stark.main import create_app
from stark.tools import social

from .conftest import ScriptedProvider, text

NOW = datetime.now(timezone.utc)
EVERY_3H = [f"{h:02d}:00" for h in range(0, 24, 3)]


def rss(items: list[tuple[str, str, str]]) -> str:
    body = "".join(f"<item><title>{t}</title><link>{u}</link><description>{d}</description>"
                   f"<pubDate>{NOW.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>" for t, u, d in items)
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>Feed</title>{body}</channel></rss>"


STORY = ("Open source AI agents get a new safety toolkit",
         "https://news.example.com/agents-toolkit",
         "The toolkit adds approval checks for AI agents. It was released on Monday with 12 built-in rules.")
ARTICLE = ("<html><body><h1>Open source AI agents get a new safety toolkit</h1><p>The toolkit adds approval "
           "checks for AI agents. It was released on Monday with 12 built-in rules.</p></body></html>")


def web(feed: str, seen: dict | None = None):
    seen = seen if seen is not None else {}

    def handler(req: httpx.Request):
        seen.setdefault("urls", []).append(str(req.url))
        if req.url.path == "/feed.xml":
            return httpx.Response(200, text=feed, headers={"content-type": "application/rss+xml"})
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.host == "news.example.com":
            return httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


class Brain:
    """Scripted writer (E.D.I.T.H.) and fact-checker (JOCASTA)."""

    def __init__(self, posts: dict[str, str] | None = None, supported: bool = True, raw: str | None = None):
        self.posts = posts or {"bluesky": "A new open source toolkit adds approval checks for AI agents, with 12 "
                                          "built-in rules. Worth a look if you run agents.",
                               "mastodon": "Open source AI agents just got a safety toolkit: approval checks and 12 "
                                           "built-in rules, released on Monday."}
        self.supported = supported
        self.raw = raw
        self.calls: list[str] = []

    def __call__(self, agent, request):
        self.calls.append(agent)
        if agent == "edith":
            return [text(self.raw if self.raw is not None else json.dumps({"posts": self.posts}))]
        if agent == "jocasta":
            return [text("```json\n" + json.dumps({"results": {p: {"supported": self.supported,
                                                                   "problems": [] if self.supported else ["invented"]}
                                                               for p in self.posts}}) + "\n```")]
        return [text("ok")]


@pytest.fixture
def brain():
    return Brain()


@pytest.fixture
def client(settings, brain):
    settings.scheduler_interval_s = 3600  # tests drive the scheduler by hand
    settings.allow_private_fetch = True  # mock hosts are not resolvable
    app = create_app(settings, {"openrouter": ScriptedProvider(brain)})
    with TestClient(app) as c:
        c.headers["authorization"] = f"Bearer {settings.api_token}"
        c.app.state.kernel.autopilot.transport = web(rss([STORY]))
        yield c


def make(client, **over):
    body = {"name": "AI safety news", "topics": ["AI agents", "open source"], "keywords": ["ai agents", "open source"],
            "platforms": ["bluesky", "mastodon"], "sources": [{"type": "rss", "value": "https://feeds.example.com/feed.xml"}],
            "post_times": EVERY_3H, "timezone": "UTC", "veto_minutes": 30, "min_gap_minutes": 60}
    body.update(over)
    r = client.post("/api/autopilot/campaigns", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def items(client, cid):
    return client.get(f"/api/autopilot/campaigns/{cid}").json()["items"]


# ------------------------------------------------------------------ pure helpers
def test_feed_parser_reads_rss_and_atom_and_refuses_entities():
    got = ap.parse_feed(rss([STORY]))
    assert got[0]["title"] == STORY[0] and got[0]["url"] == STORY[1] and got[0]["published_at"]
    atom = ("<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>Hello</title>"
            "<link rel='alternate' href='https://a.example/x'/><summary>&lt;b&gt;Hi&lt;/b&gt; there</summary>"
            "<updated>2026-10-17T10:00:00Z</updated></entry></feed>")
    a = ap.parse_feed(atom)[0]
    assert a["url"] == "https://a.example/x" and a["summary"] == "Hi there"
    bomb = "<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'aaaa'>]><rss><channel><item><title>&a;</title></item></channel></rss>"
    with pytest.raises(ap.AutopilotError):
        ap.parse_feed(bomb)


def test_compose_adds_link_and_hashtags_within_the_limit():
    body = "word " * 120
    out = ap.compose("bluesky", body, "https://news.example.com/a", ["#AiAgents", "#OpenSource"])
    assert social.weighted_length("bluesky", out) <= 300
    assert out.rstrip().endswith("#AiAgents #OpenSource") and "https://news.example.com/a" in out
    x = ap.compose("x", "Short post.", "https://news.example.com/" + "a" * 200, ["#One"])
    assert social.weighted_length("x", x) <= 280  # links count as 23 on X


def test_helpers():
    assert ap.hashtag("ai agents") == "#AiAgents" and ap.hashtag("AI safety") == "#AISafety"
    assert ap.hashtag("2026") is None
    assert ap.fit("One. Two three four five.", 12) == "One." or ap.fit("One. Two three four five.", 12).endswith("…")
    assert ap.parse_json('Sure! ```json\n{"posts": {"x": "a } b"}}\n``` hope it helps')["posts"]["x"] == "a } b"
    with pytest.raises(ap.AutopilotError):
        ap.parse_json("no json here")


def test_scoring_ignores_irrelevant_stories_and_prefers_keywords():
    c = {"topics": ["AI agents"], "keywords": ["ai agents"], "stats": {}}
    hit, kws = ap.score_item(c, {"title": "New AI agents framework", "summary": "", "published_at": NOW}, NOW)
    miss, _ = ap.score_item(c, {"title": "Best pizza in Naples", "summary": "", "published_at": NOW}, NOW)
    assert hit >= ap.SCORE_THRESHOLD and kws == ["ai agents"] and miss == 0


def test_quality_gate():
    src = STORY[0] + " " + STORY[2]
    good = "A toolkit adds approval checks for AI agents with 12 rules.\n\nhttps://news.example.com/agents-toolkit"
    kw = dict(source_url=STORY[1], source_text=src, recent=[], fact={"supported": True}, fact_check_on=True,
              blocked_terms=[])
    assert ap.quality_gate("bluesky", good, **kw)["verdict"] == "pass"
    made_up = ap.quality_gate("bluesky", good.replace("12 rules", "40 rules"), **kw)
    assert made_up["verdict"] == "review" and "40" in made_up["reasons"][0]["message"]
    assert ap.quality_gate("bluesky", good + " https://evil.example/x", **kw)["verdict"] == "review"
    assert ap.quality_gate("bluesky", good.replace("toolkit", "toolkit after the earthquake"), **kw)["verdict"] == "review"
    assert ap.quality_gate("bluesky", good, **{**kw, "recent": [good]})["verdict"] == "reject"
    assert ap.quality_gate("bluesky", good, **{**kw, "fact": {"supported": False}})["verdict"] == "review"
    assert ap.quality_gate("bluesky", good, **{**kw, "blocked_terms": ["toolkit"]})["verdict"] == "reject"
    # only a real JSON true passes the fact-check
    for junk in ("false", "yes", 1, None):
        assert ap.quality_gate("bluesky", good, **{**kw, "fact": {"supported": junk}})["verdict"] == "review"
    # a bare web address the model slipped in is not the source
    assert ap.quality_gate("bluesky", good.replace("12 rules.", "12 rules, see evil.example/win."), **kw)["verdict"] == "review"


def test_slots_respect_timezone_and_lead():
    c = {"post_times": ["09:00"], "timezone": "Asia/Karachi"}
    now = datetime(2026, 10, 17, 3, 30, tzinfo=timezone.utc)  # 08:30 in Karachi
    assert ap.slots(c, now, timedelta(minutes=10))[0] == datetime(2026, 10, 17, 4, 0, tzinfo=timezone.utc)
    assert ap.slots(c, now, timedelta(minutes=45))[0] == datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ the API
def test_campaign_validation(client):
    base = {"name": "x1", "platforms": ["bluesky"], "sources": [{"type": "hn", "value": "ai"}], "topics": ["ai"]}
    assert client.post("/api/autopilot/campaigns", json={**base, "platforms": ["instagram"]}).status_code == 400
    assert client.post("/api/autopilot/campaigns", json={**base, "post_times": ["25:00"]}).status_code == 400
    assert client.post("/api/autopilot/campaigns", json={**base, "timezone": "Mars/Base"}).status_code == 400
    assert client.post("/api/autopilot/campaigns", json={**base, "topics": []}).status_code == 400
    assert client.post("/api/autopilot/campaigns", json={**base, "trust_level": "auto"}).status_code == 409
    assert client.post("/api/autopilot/campaigns", json=base).status_code == 200
    assert client.post("/api/autopilot/campaigns", json=base).status_code == 409  # same name


def test_copilot_researches_drafts_and_waits_for_you(client, brain):
    c = make(client)
    assert c["trust_level"] == "copilot"
    report = client.post(f"/api/autopilot/campaigns/{c['id']}/run").json()
    assert report["new_stories"] == 1 and report["drafted"] == 2 and report["awaiting"] == 2, report
    assert brain.calls == ["edith", "jocasta"]  # one writing call for all platforms, one fact-check
    posts = items(client, c["id"])
    assert {p["platform"] for p in posts} == {"bluesky", "mastodon"}
    for p in posts:
        assert p["status"] == "scheduled" and not p["auto_publish"]  # nothing goes out without you
        assert STORY[1] in p["text"] and "#AiAgents" in p["text"]  # STARK adds link + SEO hashtags
        assert p["autopilot"]["gate"]["verdict"] == "pass" and p["autopilot"]["source_url"] == STORY[1]
    # the scheduler never publishes them
    k = client.app.state.kernel
    with k.db.session() as s:
        for row in s.query(ContentItem).all():
            row.scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
    import asyncio
    assert asyncio.run(social.run_scheduler_once(k, k.tools.get("social.publish").social, k.settings)) == []
    # the same story is never drafted twice
    again = client.post(f"/api/autopilot/campaigns/{c['id']}/run").json()
    assert again["new_stories"] == 0 and again["drafted"] == 0


def test_batch_approve_then_scheduler_publishes(client):
    c = make(client)
    client.post(f"/api/autopilot/campaigns/{c['id']}/run")
    posts = items(client, c["id"])
    r = client.post("/api/autopilot/approve", json={"items": [{"id": p["id"], "fingerprint": p["fingerprint"]}
                                                              for p in posts]}).json()
    assert r["approved"] == 2
    stale = client.post("/api/autopilot/approve", json={"items": [{"id": posts[0]["id"], "fingerprint": "0" * 64}]})
    assert stale.json()["results"][0]["ok"] is False  # an approval only covers the text you saw
    k = client.app.state.kernel
    with k.db.session() as s:
        for row in s.query(ContentItem).all():
            row.scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
    import asyncio
    done = asyncio.run(social.run_scheduler_once(k, k.tools.get("social.publish").social, k.settings))
    assert sorted(d["outcome"] for d in done) == ["published", "published"]
    assert all(p["status"] == "published" and p["simulated"] for p in items(client, c["id"]))


def test_veto_level_schedules_after_the_window_and_veto_resets_trust(client):
    c = make(client, trust_level="veto", veto_minutes=45)
    client.post(f"/api/autopilot/campaigns/{c['id']}/run")
    posts = items(client, c["id"])
    assert all(p["auto_publish"] for p in posts)
    for p in posts:
        assert datetime.fromisoformat(p["scheduled_for"]) >= datetime.now(timezone.utc) + timedelta(minutes=44)
    assert client.post(f"/api/autopilot/items/{posts[0]['id']}/veto").json()["ok"]
    after = {p["id"]: p for p in items(client, c["id"])}
    assert after[posts[0]["id"]]["status"] == "rejected" and not after[posts[0]["id"]]["auto_publish"]
    # full auto stays locked until enough clean posts
    r = client.patch(f"/api/autopilot/campaigns/{c['id']}", json={"trust_level": "auto"})
    assert r.status_code == 409 and "10" in r.json()["detail"]


def _published(k, c, n, *, simulated=False, campaign_id=None):
    with k.db.session() as s:
        for i in range(n):
            s.add(ContentItem(platform="bluesky", text=f"post {i}", campaign=c["name"], status="published",
                              created_by="autopilot", simulated=simulated,
                              published_at=datetime.now(timezone.utc) + timedelta(seconds=1),
                              autopilot={"campaign_id": campaign_id or c["id"]}))


def test_auto_unlocks_only_after_real_clean_posts_of_this_campaign(client, settings):
    c = make(client, trust_level="veto")
    k = client.app.state.kernel
    _published(k, c, ap.AUTO_UNLOCK, simulated=True)  # simulations never earn trust
    assert client.patch(f"/api/autopilot/campaigns/{c['id']}", json={"trust_level": "auto"}).status_code == 409
    _published(k, c, ap.AUTO_UNLOCK)
    r = client.patch(f"/api/autopilot/campaigns/{c['id']}", json={"trust_level": "auto"})
    assert r.status_code == 200 and r.json()["trust_level"] == "auto" and r.json()["auto_unlocked"]
    # deleting and recreating a campaign under the same name does not inherit the old track record
    client.delete(f"/api/autopilot/campaigns/{c['id']}")
    again = make(client, trust_level="veto")
    assert again["streak"] == 0
    assert client.patch(f"/api/autopilot/campaigns/{again['id']}", json={"trust_level": "auto"}).status_code == 409


class Interrupt(Brain):
    """Runs `during(k, campaign_id)` while E.D.I.T.H. is writing (a run takes minutes in real life)."""

    def __init__(self, during):
        super().__init__()
        self.during = during
        self.k = None
        self.cid = None

    def __call__(self, agent, request):
        if agent == "edith" and self.k is not None:
            self.during(self.k, self.cid)
        return super().__call__(agent, request)


def interrupt_client(settings, during):
    settings.scheduler_interval_s = 3600
    settings.allow_private_fetch = True
    brain = Interrupt(during)
    app = create_app(settings, {"openrouter": ScriptedProvider(brain)})
    return app, brain


@pytest.mark.parametrize("what", ["pause", "disable", "copilot", "estop"])
def test_stopping_a_campaign_during_a_run_stops_its_new_posts(settings, what):
    def during(k, cid):
        a = k.autopilot
        if what == "pause":
            a.pause(a.get(cid), "Paused by you.", actor="user")
        elif what == "disable":
            a.save(cid, enabled=False)
        elif what == "copilot":
            a.save(cid, trust_level="copilot")
        else:
            k.engage_emergency_stop()

    app, brain = interrupt_client(settings, during)
    with TestClient(app) as c:
        c.headers["authorization"] = f"Bearer {settings.api_token}"
        k = c.app.state.kernel
        k.autopilot.transport = web(rss([STORY]))
        camp = make(c, trust_level="veto")
        brain.k, brain.cid = k, camp["id"]
        import asyncio
        asyncio.run(k.autopilot.run(camp["id"]))
        k.perms.emergency_stop = False
        social.pause_queue(k, False)
        with k.db.session() as s:
            rows = s.query(ContentItem).all()
            assert rows and all(not r.auto_publish and r.approved_fp is None for r in rows), what
            for r in rows:
                r.scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
                r.paused = False
        assert asyncio.run(social.run_scheduler_once(k, k.tools.get("social.publish").social, k.settings)) == []


def test_deleting_a_campaign_during_a_run_queues_nothing(settings):
    def during(k, cid):
        from stark.core.db import Campaign

        with k.db.session() as s:
            s.delete(s.get(Campaign, cid))

    app, brain = interrupt_client(settings, during)
    with TestClient(app) as c:
        c.headers["authorization"] = f"Bearer {settings.api_token}"
        k = c.app.state.kernel
        k.autopilot.transport = web(rss([STORY]))
        camp = make(c, trust_level="veto")
        brain.k, brain.cid = k, camp["id"]
        import asyncio
        report = asyncio.run(k.autopilot.run(camp["id"]))
        assert any("deleted" in e for e in report["errors"])
        with k.db.session() as s:
            assert s.query(ContentItem).count() == 0


def test_lowering_trust_or_vetoing_withdraws_autopilot_approvals(client, brain):
    feed = rss([STORY, ("Open source AI agents ship v2", "https://news.example.com/v2", "AI agents v2 is out.")])
    client.app.state.kernel.autopilot.transport = web(feed)
    texts = iter(["A toolkit adds approval checks for AI agents.", "Version two of the AI agents project is out."])
    orig = Brain.__call__

    def varied(self, agent, request):
        if agent == "edith":
            self.posts = {"bluesky": next(texts)}
        return orig(self, agent, request)
    brain.__class__ = type("Varied", (Brain,), {"__call__": varied})
    c = make(client, trust_level="veto", platforms=["bluesky"], min_gap_minutes=60)
    client.post(f"/api/autopilot/campaigns/{c['id']}/run")
    going = [p for p in items(client, c["id"]) if p["auto_publish"]]
    assert len(going) == 2
    # vetoing one post: the other one Autopilot approved by itself now waits for you
    client.post(f"/api/autopilot/items/{going[0]['id']}/veto")
    after = {p["id"]: p for p in items(client, c["id"])}
    assert not after[going[1]["id"]]["auto_publish"] and after[going[1]["id"]]["status"] == "scheduled"
    # you approve it yourself, then lower the trust level: your own approval stays
    client.post("/api/autopilot/approve", json={"items": [{"id": going[1]["id"],
                                                           "fingerprint": after[going[1]["id"]]["fingerprint"]}]})
    client.patch(f"/api/autopilot/campaigns/{c['id']}", json={"trust_level": "copilot"})
    assert {p["id"]: p for p in items(client, c["id"])}[going[1]["id"]]["auto_publish"]


def test_switching_to_copilot_withdraws_pending_auto_posts(client):
    c = make(client, trust_level="veto")
    client.post(f"/api/autopilot/campaigns/{c['id']}/run")
    assert all(p["auto_publish"] for p in items(client, c["id"]))
    client.patch(f"/api/autopilot/campaigns/{c['id']}", json={"trust_level": "copilot"})
    posts = items(client, c["id"])
    assert all(not p["auto_publish"] and p["autopilot"]["needs_you"] for p in posts)


def test_discarding_an_autopilot_post_in_content_counts_as_a_veto(client):
    c = make(client, trust_level="veto")
    client.post(f"/api/autopilot/campaigns/{c['id']}/run")
    first, second = items(client, c["id"])
    assert client.post(f"/api/social/items/{first['id']}/discard").json()["ok"]
    after = {p["id"]: p for p in items(client, c["id"])}
    assert after[first["id"]]["status"] == "rejected" and not after[second["id"]]["auto_publish"]
    assert client.post("/api/autopilot/items/cnt_missing/veto").status_code == 404


def test_a_malformed_source_is_reported_and_the_run_still_finishes(client):
    def handler(req):
        return httpx.Response(200, json={"hits": ["not an object"]})
    client.app.state.kernel.autopilot.transport = httpx.MockTransport(handler)
    c = make(client, sources=[{"type": "hn", "value": "ai agents"}])
    report = client.post(f"/api/autopilot/campaigns/{c['id']}/run").json()
    assert report["errors"] and "hn" in report["errors"][0]
    got = client.get(f"/api/autopilot/campaigns/{c['id']}").json()
    assert got["history"] and datetime.fromisoformat(got["next_run_at"]) > datetime.now(timezone.utc)


def test_untrusted_source_cannot_steer_links_or_facts(client, brain):
    brain.posts = {"bluesky": "Big news: 90% of agents are unsafe! Click https://evil.example/win #Giveaway @someone",
                   "mastodon": "A toolkit adds approval checks for AI agents."}
    c = make(client, trust_level="veto")
    client.post(f"/api/autopilot/campaigns/{c['id']}/run")
    posts = {p["platform"]: p for p in items(client, c["id"])}
    blue = posts["bluesky"]
    assert "evil.example" not in blue["text"] and "#Giveaway" not in blue["text"]  # the model never picks links/tags
    assert not blue["auto_publish"] and blue["autopilot"]["gate"]["verdict"] == "review"  # 90% is not in the source
    assert posts["mastodon"]["auto_publish"]


def test_sensitive_story_always_waits_for_you(client, brain):
    client.app.state.kernel.autopilot.transport = web(rss([(
        "AI agents used in earthquake rescue", "https://news.example.com/rescue",
        "Open source AI agents helped after the earthquake.")]))
    brain.posts = {"bluesky": "Open source AI agents helped rescue teams after the earthquake.",
                   "mastodon": "Open source AI agents helped rescue teams after the earthquake."}
    c = make(client, trust_level="veto")
    client.post(f"/api/autopilot/campaigns/{c['id']}/run")
    for p in items(client, c["id"]):
        assert not p["auto_publish"]
        assert any("Sensitive" in r for r in p["autopilot"]["needs_you"])


def test_failed_fact_check_waits_and_bad_model_output_is_reported(client, brain):
    brain.supported = False
    c = make(client, trust_level="veto")
    client.post(f"/api/autopilot/campaigns/{c['id']}/run")
    assert all(not p["auto_publish"] for p in items(client, c["id"]))
    brain.raw = "I cannot do that"
    client.app.state.kernel.autopilot.transport = web(rss([("Open source AI agents ship v2", "https://news.example.com/v2",
                                                            "AI agents v2 is out.")]))
    rep = client.post(f"/api/autopilot/campaigns/{c['id']}/run").json()
    assert rep["drafted"] == 0 and any("writing failed" in e for e in rep["errors"])
    tasks = client.get("/api/tasks?limit=20").json()
    assert any(t["kind"] == "autopilot" and t["status"] == "failed" for t in tasks)


def test_daily_cap_and_spacing(client, brain):
    feed = rss([(f"Open source AI agents story {n}", f"https://news.example.com/s{n}", f"AI agents news {n}.")
                for n in range(8)])
    client.app.state.kernel.autopilot.transport = web(feed)
    brain.posts = {"bluesky": "Open source AI agents news."}
    c = make(client, platforms=["bluesky"], daily_cap=1, max_new_per_run=5, min_gap_minutes=600)
    for _ in range(3):
        client.post(f"/api/autopilot/campaigns/{c['id']}/run")
    queued = [p for p in items(client, c["id"]) if p["status"] == "scheduled"]
    days = [datetime.fromisoformat(p["scheduled_for"]).date() for p in queued]
    assert len(days) == len(set(days)) and 1 <= len(queued) <= 3  # at most one per day


def test_pause_resume_and_emergency_stop(client):
    c = make(client, trust_level="veto")
    client.post(f"/api/autopilot/campaigns/{c['id']}/run")
    assert client.post(f"/api/autopilot/campaigns/{c['id']}/pause?paused=true").json()["paused"]
    assert all(p["paused"] for p in items(client, c["id"]) if p["status"] == "scheduled")
    assert client.post(f"/api/autopilot/campaigns/{c['id']}/run").status_code == 409
    client.post(f"/api/autopilot/campaigns/{c['id']}/pause?paused=false")
    k = client.app.state.kernel
    k.engage_emergency_stop()
    import asyncio
    assert asyncio.run(k.autopilot.tick()) == []


def test_crisis_in_mentions_pauses_campaigns(client):
    c = make(client, trust_level="veto")
    k = client.app.state.kernel
    sc = k.tools.get("social.publish").social
    later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()

    def bsky(req: httpx.Request):
        if req.url.path.endswith("createSession"):
            return httpx.Response(200, json={"accessJwt": "jwt", "did": "did:plc:x", "handle": "me.bsky.social"})
        if req.url.path.endswith("listNotifications"):
            return httpx.Response(200, json={"notifications": [{
                "reason": "mention", "uri": "at://x/1", "author": {"handle": "angry.example"}, "indexedAt": later,
                "record": {"text": "Your app leaked my data, my lawyer will contact you"}}]})
        return httpx.Response(404)
    t = httpx.MockTransport(bsky)
    for a in sc.adapters.values():
        a.transport = t
    k.settings.bluesky_handle, k.settings.bluesky_app_password = "me.bsky.social", "abcd-efgh-ijkl-mnop"
    import asyncio
    asyncio.run(k.autopilot.tick())
    got = client.get(f"/api/autopilot/campaigns/{c['id']}").json()
    assert got["paused"] and "crisis" in got["pause_reason"]
    # resuming acknowledges it: the same mention does not pause it again
    client.post(f"/api/autopilot/campaigns/{c['id']}/pause?paused=false")
    asyncio.run(k.autopilot.crisis_check(k.autopilot.all()))
    assert not client.get(f"/api/autopilot/campaigns/{c['id']}").json()["paused"]


def test_two_failures_in_a_row_pause_the_campaign(client):
    c = make(client, trust_level="veto")
    k = client.app.state.kernel
    with k.db.session() as s:
        for n in range(2):
            s.add(ContentItem(platform="bluesky", text=f"f{n}", campaign=c["name"], status="failed",
                              created_by="autopilot", autopilot={"campaign_id": c["id"]}))
    rep = client.post(f"/api/autopilot/campaigns/{c['id']}/run").json()
    assert "failed twice" in rep["skipped"]
    assert client.get(f"/api/autopilot/campaigns/{c['id']}").json()["paused"]


def test_engagement_feeds_keyword_stats(client):
    c = make(client)
    k = client.app.state.kernel
    with k.db.session() as s:
        s.add(ContentItem(platform="bluesky", text="p", campaign=c["name"], status="published", created_by="autopilot",
                          simulated=False, external_id="at://did:plc:x/app.bsky.feed.post/1",
                          published_at=datetime.now(timezone.utc),
                          autopilot={"campaign_id": c["id"], "keywords": ["ai agents"]}))
    sc = k.tools.get("social.publish").social

    def bsky(req: httpx.Request):
        if req.url.path.endswith("createSession"):
            return httpx.Response(200, json={"accessJwt": "jwt", "did": "did:plc:x", "handle": "me"})
        if req.url.path.endswith("getPosts"):
            return httpx.Response(200, json={"posts": [{"uri": "at://did:plc:x/app.bsky.feed.post/1", "likeCount": 7,
                                                        "repostCount": 2, "replyCount": 1}]})
        return httpx.Response(404)
    for a in sc.adapters.values():
        a.transport = httpx.MockTransport(bsky)
    k.settings.bluesky_handle, k.settings.bluesky_app_password = "me", "abcd-efgh-ijkl-mnop"
    import asyncio
    assert asyncio.run(k.autopilot.refresh_metrics(c["id"])) == 1
    table = client.get(f"/api/autopilot/campaigns/{c['id']}").json()["keywords_table"]
    row = next(r for r in table if r["keyword"] == "ai agents")
    assert row["avg_engagement"] == 13 and row["hashtag"] == "#AiAgents"


def test_delete_cancels_pending_posts_and_digest(client):
    c = make(client, trust_level="veto")
    client.post(f"/api/autopilot/campaigns/{c['id']}/run")
    d = client.get("/api/autopilot/digest").json()["campaigns"][0]
    assert len(d["going_out"]) == 2
    out = client.delete(f"/api/autopilot/campaigns/{c['id']}").json()
    assert out["cancelled_posts"] == 2
    q = client.get("/api/social/queue").json()["items"]
    assert all(i["status"] == "rejected" for i in q)


def test_autopilot_needs_auth(client):
    r = client.get("/api/autopilot/campaigns", headers={"authorization": "Bearer nope"})
    assert r.status_code == 401
