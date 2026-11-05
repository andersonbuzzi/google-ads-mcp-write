"""Offline tests: the Google Ads API is replaced by a fake that records every call."""

import json

import pytest

from google_ads_mcp_write import server


def call(tool, *args, **kwargs):
    # Depending on the fastmcp version, @mcp.tool() returns the function or a wrapper.
    return getattr(tool, "fn", tool)(*args, **kwargs)


class FakeApi:
    def __init__(self, search_results=None):
        self.calls = []
        self.search_results = search_results or []

    def post(self, path, payload):
        self.calls.append((path, payload))
        if path.endswith("googleAds:search"):
            return {"results": self.search_results}
        ops = payload.get("operations", [])
        return {"results": [{"resourceName": f"fake/{len(self.calls)}"} for _ in ops]}

    def mutations(self, validate_only):
        return [(p, b) for p, b in self.calls
                if "search" not in p and b.get("validateOnly") is validate_only]


@pytest.fixture
def api(monkeypatch, tmp_path):
    fake = FakeApi()
    monkeypatch.setattr(server, "_post", fake.post)
    monkeypatch.setenv("GOOGLE_ADS_MCP_WRITE_CUSTOMERS", "123-456-7890")
    monkeypatch.setenv("GOOGLE_ADS_MCP_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("GOOGLE_ADS_MCP_WRITE_ENABLED", raising=False)
    server._PENDING.clear()
    return fake


def test_account_outside_allowlist_is_refused(api):
    with pytest.raises(ValueError, match="allowlist"):
        call(server.set_campaign_status, "999", "1", "PAUSED")
    assert api.calls == []


def test_two_step_flow_dry_runs_then_executes_once(api, tmp_path):
    plan = call(server.set_campaign_status, "1234567890", "42", "paused")
    assert plan["status"] == "awaiting_confirmation"
    assert len(api.mutations(validate_only=True)) == 1
    assert api.mutations(validate_only=False) == []

    done = call(server.set_campaign_status, "1234567890", "42", "PAUSED",
                confirm_token=plan["confirm_token"])
    assert done["status"] == "executed"
    path, body = api.mutations(validate_only=False)[0]
    assert path == "customers/1234567890/campaigns:mutate"
    assert body["operations"][0]["update"] == {
        "resourceName": "customers/1234567890/campaigns/42", "status": "PAUSED"}

    with pytest.raises(ValueError, match="already used"):
        call(server.set_campaign_status, "1234567890", "42", "PAUSED",
             confirm_token=plan["confirm_token"])

    log = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [e["phase"] for e in log] == ["plan", "execute"]


def test_remove_is_not_supported(api):
    with pytest.raises(ValueError, match="REMOVE"):
        call(server.set_ad_group_status, "1234567890", "7", "REMOVED")


def test_kill_switch_blocks_writes(api, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_MCP_WRITE_ENABLED", "0")
    with pytest.raises(RuntimeError, match="disabled"):
        call(server.add_negative_keyword, "1234567890", "42", ["free"])


def test_budget_cap_and_max_increase(api, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_MCP_MAX_BUDGET_MICROS", "100000000")  # 100/day
    api.search_results = [{"campaignBudget": {"id": "9", "name": "b", "amountMicros": "30000000"}}]

    with pytest.raises(ValueError, match="cap"):
        call(server.set_campaign_budget, "1234567890", "9", 150)
    with pytest.raises(ValueError, match="limit"):
        call(server.set_campaign_budget, "1234567890", "9", 60)  # +100% over 30

    plan = call(server.set_campaign_budget, "1234567890", "9", 45)  # +50% is allowed
    assert "30.00 -> 45.00" in plan["summary"]


def test_responsive_search_ad_validation(api):
    with pytest.raises(ValueError, match="30 characters"):
        call(server.create_responsive_search_ad, "1234567890", "7", "https://x.com",
             ["ok", "ok", "x" * 31], ["d1", "d2"])
    with pytest.raises(ValueError, match="https"):
        call(server.create_responsive_search_ad, "1234567890", "7", "http://x.com",
             ["a", "b", "c"], ["d1", "d2"])


def test_create_campaign_requires_targeting(api, monkeypatch):
    monkeypatch.delenv("GOOGLE_ADS_MCP_DEFAULT_GEO_TARGETS", raising=False)
    with pytest.raises(ValueError, match="geo_target_ids"):
        call(server.create_search_campaign, "1234567890", "Test", 10, 1, language_ids=["1000"])


def test_create_campaign_chains_resource_names(api, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_MCP_DEFAULT_GEO_TARGETS", "2076")
    monkeypatch.setenv("GOOGLE_ADS_MCP_DEFAULT_LANGUAGES", "1014")
    plan = call(server.create_search_campaign, "1234567890", "Test", 10, 1.5)
    assert plan["steps"] == ["budget", "campaign", "targeting", "ad_group"]

    call(server.create_search_campaign, "1234567890", "Test", 10, 1.5,
         confirm_token=plan["confirm_token"])
    executed = api.mutations(validate_only=False)
    campaign = executed[1][1]["operations"][0]["create"]
    assert campaign["status"] == "PAUSED"
    assert campaign["campaignBudget"] == "fake/2"  # the budget created by the first step
    targeting = executed[2][1]["operations"]
    assert targeting[0]["create"]["location"]["geoTargetConstant"] == "geoTargetConstants/2076"
    assert all(op["create"]["campaign"] == "fake/3" for op in targeting)


def test_auto_apply_skips_unknown_subscriptions(api):
    api.search_results = [
        {"recommendationSubscription": {"resourceName": "customers/1/recommendationSubscriptions/KEYWORD"}},
        {"recommendationSubscription": {"resourceName": "customers/1/recommendationSubscriptions/UNKNOWN"}},
    ]
    plan = call(server.pause_auto_apply_recommendations, "1234567890")
    assert "KEYWORD" in plan["summary"]
    assert "UNKNOWN" in plan["warning"]
    path, body = api.mutations(validate_only=True)[0]
    assert path.endswith("recommendationSubscriptions:mutateRecommendationSubscription")
    assert len(body["operations"]) == 1
