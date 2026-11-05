"""MCP server for write operations on Google Ads, with guardrails.

Reading stays with the official read-only server (googleads/google-ads-mcp).
This server only exposes reversible changes, and every change runs in two
steps: a dry run validated by Google (validateOnly), then an explicit confirm.
"""

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import google.auth
from fastmcp import FastMCP
from google.auth.transport.requests import Request

SCOPE = "https://www.googleapis.com/auth/adwords"
PLAN_TTL = 3600  # seconds; generous because the approval prompt waits on a human

mcp = FastMCP("google-ads-write")

# Plans waiting for confirmation, keyed by token. They live only in process memory.
_PENDING: dict[str, dict] = {}


# --- configuration -----------------------------------------------------------

def _digits(value) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def _id_list(raw: str) -> list[str]:
    return [d for d in (_digits(p) for p in raw.split(",")) if d]


def _api_base() -> str:
    version = os.environ.get("GOOGLE_ADS_API_VERSION", "v25")
    return f"https://googleads.googleapis.com/{version}"


def _allowed_customers() -> set[str]:
    return set(_id_list(os.environ.get("GOOGLE_ADS_MCP_WRITE_CUSTOMERS", "")))


def _max_budget_micros() -> int:
    return int(os.environ.get("GOOGLE_ADS_MCP_MAX_BUDGET_MICROS", "200000000"))


def _max_increase_pct() -> float:
    return float(os.environ.get("GOOGLE_ADS_MCP_MAX_BUDGET_INCREASE_PCT", "50"))


def _writes_enabled() -> bool:
    return os.environ.get("GOOGLE_ADS_MCP_WRITE_ENABLED", "1") != "0"


def _audit_path() -> Path:
    default = Path.home() / ".local/share/google-ads-mcp-write/audit.jsonl"
    return Path(os.environ.get("GOOGLE_ADS_MCP_AUDIT_LOG", default))


def _check_customer(customer_id: str) -> str:
    cid = _digits(customer_id)
    allowed = _allowed_customers()
    if not allowed:
        raise ValueError("GOOGLE_ADS_MCP_WRITE_CUSTOMERS is not set: no account is allowed.")
    if cid not in allowed:
        raise ValueError(f"account {cid} is not in the allowlist: {', '.join(sorted(allowed))}")
    return cid


def _to_micros(amount: float, field: str) -> int:
    micros = round(float(amount) * 1_000_000)
    if micros <= 0:
        raise ValueError(f"{field} must be positive.")
    return micros


def _money(micros: int) -> str:
    return f"{micros / 1e6:,.2f}"


# --- Google Ads REST ------------------------------------------------------------

def _headers() -> dict[str, str]:
    creds, _ = google.auth.default(scopes=[SCOPE])
    creds.refresh(Request())
    token = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN")
    if not token:
        raise ValueError("GOOGLE_ADS_DEVELOPER_TOKEN is not set.")
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "developer-token": token,
        "Content-Type": "application/json",
    }
    login_cid = _digits(os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", ""))
    if login_cid:
        headers["login-customer-id"] = login_cid
    return headers


def _describe_error(exc: urllib.error.HTTPError) -> str:
    body = exc.read().decode()
    try:
        err = json.loads(body)["error"]
        parts = [f"HTTP {exc.code} {err.get('status')}: {err.get('message')}"]
        for detail in err.get("details", []):
            for item in detail.get("errors", []):
                parts.append(
                    f"  {json.dumps(item.get('errorCode', {}), ensure_ascii=False)} "
                    f"{item.get('message')}"
                )
        return "\n".join(parts)
    except (ValueError, KeyError, TypeError, AttributeError):
        return f"HTTP {exc.code}: {body[:500]}"


def _post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{_api_base()}/{path}", data=json.dumps(payload).encode(), headers=_headers()
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_describe_error(exc)) from None


def _search(customer_id: str, query: str) -> list[dict]:
    return _post(f"customers/{customer_id}/googleAds:search", {"query": query}).get(
        "results", []
    )


def _mutate(customer_id: str, service: str, operations: list[dict], validate_only: bool) -> dict:
    # Most services use ":mutate"; the few with their own method pass it with a ":" in the name.
    method = service if ":" in service else f"{service}:mutate"
    return _post(
        f"customers/{customer_id}/{method}",
        {"operations": operations, "validateOnly": validate_only, "partialFailure": False},
    )


# --- two-step plans and audit log ------------------------------------------------

def _audit(phase: str, plan: dict, response: dict | None) -> None:
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "phase": phase,
        "kind": plan["kind"],
        "customer_id": plan["customer_id"],
        "service": plan["service"],
        "summary": plan["summary"],
        "operations": plan["operations"],
        "response": response,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _require_writes() -> None:
    if not _writes_enabled():
        raise RuntimeError("writes are disabled (GOOGLE_ADS_MCP_WRITE_ENABLED=0).")


def _store_plan(plan: dict) -> str:
    token = uuid.uuid4().hex[:12]
    plan["expires"] = time.time() + PLAN_TTL
    _PENDING[token] = plan
    _audit("plan", plan, None)
    return token


def _stage(kind: str, customer_id: str, service: str, operations: list[dict], summary: str) -> dict:
    """Run the dry run, store the plan and return the confirmation token."""
    _require_writes()
    _mutate(customer_id, service, operations, validate_only=True)  # raises if invalid
    token = _store_plan({
        "kind": kind,
        "customer_id": customer_id,
        "service": service,
        "operations": operations,
        "summary": summary,
    })
    return {
        "status": "awaiting_confirmation",
        "summary": summary,
        "confirm_token": token,
        "valid_for_seconds": PLAN_TTL,
        "next_step": "Call the same tool again with confirm_token to execute.",
    }


def _resolve(node, ctx: dict[str, str]):
    """Replace ${name} placeholders with resourceNames captured by earlier steps."""
    if isinstance(node, str):
        for key, value in ctx.items():
            node = node.replace(f"${{{key}}}", value)
        return node
    if isinstance(node, list):
        return [_resolve(item, ctx) for item in node]
    if isinstance(node, dict):
        return {k: _resolve(v, ctx) for k, v in node.items()}
    return node


def _run_sequence(customer_id: str, steps: list[dict]) -> dict:
    """Run chained steps, passing each created resourceName to the next one."""
    ctx: dict[str, str] = {}
    created = []
    for step in steps:
        operations = _resolve(step["operations"], ctx)
        result = _mutate(customer_id, step["service"], operations, False)
        name = result.get("results", [{}])[0].get("resourceName", "")
        if step.get("capture"):
            ctx[step["capture"]] = name
        created.append({"step": step.get("capture") or step["service"], "resourceName": name})
    return {"created": created}


def _stage_sequence(kind: str, customer_id: str, steps: list[dict], summary: str) -> dict:
    """Like _stage, for chained creations.

    Only the first step is dry-run: later steps reference resources that do not
    exist yet, and Google's validateOnly would reject the reference.
    """
    _require_writes()
    _mutate(customer_id, steps[0]["service"], steps[0]["operations"], validate_only=True)
    token = _store_plan({
        "kind": kind,
        "customer_id": customer_id,
        "service": "(sequence)",
        "operations": steps,
        "summary": summary,
        "sequence": True,
    })
    return {
        "status": "awaiting_confirmation",
        "summary": summary,
        "steps": [s.get("capture") or s["service"] for s in steps],
        "confirm_token": token,
        "valid_for_seconds": PLAN_TTL,
        "warning": "the dry run covers only the first step of the sequence",
        "next_step": "Call the same tool again with confirm_token to execute.",
    }


def _execute(confirm_token: str) -> dict:
    _require_writes()
    plan = _PENDING.pop(confirm_token, None)
    if plan is None:
        raise ValueError("confirm_token is invalid or was already used.")
    if time.time() > plan["expires"]:
        raise ValueError("plan expired. Call the tool again without confirm_token.")
    if plan.get("sequence"):
        response = _run_sequence(plan["customer_id"], plan["operations"])
    else:
        response = _mutate(plan["customer_id"], plan["service"], plan["operations"], False)
    _audit("execute", plan, response)
    return {"status": "executed", "summary": plan["summary"], "response": response}


def _check_status(status: str) -> str:
    status = status.upper()
    if status not in {"ENABLED", "PAUSED"}:
        raise ValueError("status must be ENABLED or PAUSED. REMOVE is not supported.")
    return status


def _check_match_type(match_type: str) -> str:
    match_type = match_type.upper()
    if match_type not in {"EXACT", "PHRASE", "BROAD"}:
        raise ValueError("match_type must be EXACT, PHRASE or BROAD.")
    return match_type


def _set_status(kind: str, customer_id: str, resource_path: str, service: str, status: str,
                label: str, confirm_token: str | None) -> dict:
    if confirm_token:
        return _execute(confirm_token)
    cid = _check_customer(customer_id)
    status = _check_status(status)
    operations = [{"update": {"resourceName": f"customers/{cid}/{resource_path}",
                              "status": status},
                   "updateMask": "status"}]
    return _stage(kind, cid, service, operations, f"{label} -> {status}")


# --- tools: inspection ---------------------------------------------------------------

@mcp.tool()
def write_status() -> dict:
    """Show the active guardrails: allowed accounts, budget limits and kill switch."""
    return {
        "writes_enabled": _writes_enabled(),
        "allowed_accounts": sorted(_allowed_customers()),
        "max_daily_budget": _max_budget_micros() / 1e6,
        "max_budget_increase_pct": _max_increase_pct(),
        "audit_log": str(_audit_path()),
        "pending_plans": len(_PENDING),
        "api": _api_base(),
    }


@mcp.tool()
def list_campaigns(customer_id: str) -> list[dict]:
    """List campaigns with id, status and budget. Read-only, to build plans."""
    cid = _check_customer(customer_id)
    rows = _search(
        cid,
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign_budget.id, campaign_budget.amount_micros "
        "FROM campaign ORDER BY campaign.name",
    )
    return [
        {
            "campaign_id": r["campaign"]["id"],
            "name": r["campaign"]["name"],
            "status": r["campaign"]["status"],
            "campaign_budget_id": r.get("campaignBudget", {}).get("id"),
            "daily_budget": int(r.get("campaignBudget", {}).get("amountMicros", 0)) / 1e6,
        }
        for r in rows
    ]


# --- tools: pause and resume ---------------------------------------------------------

@mcp.tool()
def set_campaign_status(customer_id: str, campaign_id: str, status: str,
                        confirm_token: str | None = None) -> dict:
    """Pause or resume a campaign. Without confirm_token returns the plan; with it, executes."""
    return _set_status("set_campaign_status", customer_id, f"campaigns/{_digits(campaign_id)}",
                       "campaigns", status, f"campaign {_digits(campaign_id)}", confirm_token)


@mcp.tool()
def set_ad_group_status(customer_id: str, ad_group_id: str, status: str,
                        confirm_token: str | None = None) -> dict:
    """Pause or resume an ad group."""
    return _set_status("set_ad_group_status", customer_id, f"adGroups/{_digits(ad_group_id)}",
                       "adGroups", status, f"ad group {_digits(ad_group_id)}", confirm_token)


@mcp.tool()
def set_keyword_status(customer_id: str, ad_group_id: str, criterion_ids: list[str],
                       status: str, confirm_token: str | None = None) -> dict:
    """Pause or resume several keywords of the same ad group in one operation."""
    if confirm_token:
        return _execute(confirm_token)
    cid = _check_customer(customer_id)
    status = _check_status(status)
    if not criterion_ids:
        raise ValueError("pass at least one criterion_id.")
    agid = _digits(ad_group_id)
    operations = [
        {"update": {"resourceName": f"customers/{cid}/adGroupCriteria/{agid}~{_digits(c)}",
                    "status": status},
         "updateMask": "status"}
        for c in criterion_ids
    ]
    return _stage("set_keyword_status", cid, "adGroupCriteria", operations,
                  f"{len(operations)} keyword(s) in ad group {agid} -> {status}")


# --- tools: budget and bidding ---------------------------------------------------------

@mcp.tool()
def set_campaign_budget(customer_id: str, campaign_budget_id: str, daily_amount: float,
                        confirm_token: str | None = None) -> dict:
    """Change a campaign's daily budget (account currency), within the cap and max increase."""
    if confirm_token:
        return _execute(confirm_token)

    cid = _check_customer(customer_id)
    budget_id = _digits(campaign_budget_id)
    new_micros = _to_micros(daily_amount, "daily_amount")

    cap = _max_budget_micros()
    if new_micros > cap:
        raise ValueError(f"{_money(new_micros)} exceeds the daily cap of {_money(cap)}.")

    rows = _search(
        cid,
        "SELECT campaign_budget.id, campaign_budget.name, campaign_budget.amount_micros "
        f"FROM campaign_budget WHERE campaign_budget.id = {budget_id}",
    )
    if not rows:
        raise ValueError(f"budget {budget_id} not found in account {cid}.")
    current = int(rows[0]["campaignBudget"].get("amountMicros", 0))

    limit_pct = _max_increase_pct()
    if current > 0:
        maximum = current * (1 + limit_pct / 100)
        if new_micros > maximum:
            raise ValueError(
                f"raising {_money(current)} to {_money(new_micros)} exceeds the "
                f"+{limit_pct:g}% limit (max {_money(int(maximum))})."
            )

    operations = [{
        "update": {
            "resourceName": f"customers/{cid}/campaignBudgets/{budget_id}",
            "amountMicros": str(new_micros),
        },
        "updateMask": "amount_micros",
    }]
    summary = (
        f"budget {budget_id} ({rows[0]['campaignBudget'].get('name', '')}): "
        f"{_money(current)} -> {_money(new_micros)} per day"
    )
    return _stage("set_campaign_budget", cid, "campaignBudgets", operations, summary)


@mcp.tool()
def set_campaign_manual_cpc(customer_id: str, campaign_ids: list[str],
                            confirm_token: str | None = None) -> dict:
    """Switch campaigns to manual CPC (no enhanced CPC); ad group bids apply again."""
    if confirm_token:
        return _execute(confirm_token)
    cid = _check_customer(customer_id)
    if not campaign_ids:
        raise ValueError("pass at least one campaign_id.")
    ids = [_digits(c) for c in campaign_ids]
    operations = [
        {"update": {"resourceName": f"customers/{cid}/campaigns/{c}",
                    "manualCpc": {"enhancedCpcEnabled": False}},
         "updateMask": "manual_cpc.enhanced_cpc_enabled"}
        for c in ids
    ]
    return _stage("set_campaign_manual_cpc", cid, "campaigns", operations,
                  f"campaign(s) {', '.join(ids)}: bidding strategy -> manual CPC")


@mcp.tool()
def pause_auto_apply_recommendations(customer_id: str,
                                     confirm_token: str | None = None) -> dict:
    """Turn off auto-applied recommendations (every active subscription)."""
    if confirm_token:
        return _execute(confirm_token)
    cid = _check_customer(customer_id)
    rows = _search(
        cid,
        "SELECT recommendation_subscription.resource_name FROM recommendation_subscription "
        "WHERE recommendation_subscription.status = 'ENABLED'",
    )
    names = [r["recommendationSubscription"]["resourceName"] for r in rows]
    # Types this API version does not know come back as .../UNKNOWN and cannot be addressed.
    known = sorted({n for n in names if not n.endswith("/UNKNOWN")})
    unknown = sum(1 for n in names if n.endswith("/UNKNOWN"))
    if not known:
        raise ValueError(
            f"no active subscription is addressable through the API ({unknown} UNKNOWN): "
            "turn them off in the Google Ads UI."
        )
    operations = [
        {"update": {"resourceName": n, "status": "PAUSED"}, "updateMask": "status"}
        for n in known
    ]
    types = ", ".join(n.rsplit("/", 1)[1] for n in known)
    plan = _stage("pause_auto_apply_recommendations", cid,
                  "recommendationSubscriptions:mutateRecommendationSubscription",
                  operations, f"pause {len(known)} auto-apply subscription(s): {types}")
    if unknown:
        plan["warning"] = (f"{unknown} subscription(s) show as UNKNOWN in this API version "
                           "and must be turned off in the Google Ads UI "
                           "(Recommendations > Auto-apply).")
    return plan


# --- tools: creation ---------------------------------------------------------------------

def _targets(explicit: list[str] | None, env_var: str, kind: str) -> list[str]:
    ids = [_digits(i) for i in explicit] if explicit else _id_list(os.environ.get(env_var, ""))
    if not ids:
        raise ValueError(f"pass {kind} or set {env_var}.")
    return ids


@mcp.tool()
def create_search_campaign(customer_id: str, name: str, daily_budget: float,
                           default_cpc: float, ad_group_name: str = "Ad group 1",
                           geo_target_ids: list[str] | None = None,
                           language_ids: list[str] | None = None,
                           budget_id: str | None = None,
                           confirm_token: str | None = None) -> dict:
    """Create a PAUSED Search campaign with a budget, location/language targeting and one ad group.

    Amounts are in the account currency. It is created paused on purpose: nothing
    spends until you resume it explicitly. geo_target_ids and language_ids are
    Google criterion ids (e.g. 2076 = Brazil, 1014 = Portuguese); when omitted they
    come from GOOGLE_ADS_MCP_DEFAULT_GEO_TARGETS / GOOGLE_ADS_MCP_DEFAULT_LANGUAGES.
    budget_id reuses an existing budget instead of creating one, which helps to
    resume a sequence that failed after the budget was already created.
    """
    if confirm_token:
        return _execute(confirm_token)

    cid = _check_customer(customer_id)
    budget_micros = _to_micros(daily_budget, "daily_budget")
    cpc_micros = _to_micros(default_cpc, "default_cpc")
    cap = _max_budget_micros()
    if budget_micros > cap:
        raise ValueError(f"{_money(budget_micros)} per day exceeds the cap of {_money(cap)}.")
    geos = _targets(geo_target_ids, "GOOGLE_ADS_MCP_DEFAULT_GEO_TARGETS", "geo_target_ids")
    langs = _targets(language_ids, "GOOGLE_ADS_MCP_DEFAULT_LANGUAGES", "language_ids")

    steps = []
    if budget_id:
        budget_ref = f"customers/{cid}/campaignBudgets/{_digits(budget_id)}"
    else:
        budget_ref = "${budget}"
        steps.append({
            "service": "campaignBudgets",
            "capture": "budget",
            "operations": [{"create": {
                "name": f"{name} - budget",
                "amountMicros": str(budget_micros),
                "deliveryMethod": "STANDARD",
                "explicitlyShared": False,
            }}],
        })
    steps += [
        {
            "service": "campaigns",
            "capture": "campaign",
            "operations": [{"create": {
                "name": name,
                "status": "PAUSED",
                "advertisingChannelType": "SEARCH",
                "campaignBudget": budget_ref,
                "manualCpc": {"enhancedCpcEnabled": False},
                # Required since 09/2025: without it the API rejects with FieldError.REQUIRED.
                "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
                "networkSettings": {
                    "targetGoogleSearch": True,
                    "targetSearchNetwork": False,
                    "targetContentNetwork": False,
                    "targetPartnerSearchNetwork": False,
                },
            }}],
        },
        {
            "service": "campaignCriteria",
            "capture": "targeting",
            "operations": (
                [{"create": {"campaign": "${campaign}",
                             "location": {"geoTargetConstant": f"geoTargetConstants/{g}"}}}
                 for g in geos]
                + [{"create": {"campaign": "${campaign}",
                               "language": {"languageConstant": f"languageConstants/{lang}"}}}
                   for lang in langs]
            ),
        },
        {
            "service": "adGroups",
            "capture": "ad_group",
            "operations": [{"create": {
                "name": ad_group_name,
                "campaign": "${campaign}",
                "status": "ENABLED",
                "type": "SEARCH_STANDARD",
                "cpcBidMicros": str(cpc_micros),
            }}],
        },
    ]
    summary = (
        f"create PAUSED campaign '{name}': {_money(budget_micros)}/day, "
        f"CPC {_money(cpc_micros)}, geo {','.join(geos)}, languages {','.join(langs)}, "
        f"ad group '{ad_group_name}'"
    )
    return _stage_sequence("create_search_campaign", cid, steps, summary)


@mcp.tool()
def create_ad_group(customer_id: str, campaign_id: str, name: str, default_cpc: float,
                    confirm_token: str | None = None) -> dict:
    """Create an ad group in an existing campaign (CPC in the account currency)."""
    if confirm_token:
        return _execute(confirm_token)
    cid = _check_customer(customer_id)
    cpc_micros = _to_micros(default_cpc, "default_cpc")
    operations = [{"create": {
        "name": name,
        "campaign": f"customers/{cid}/campaigns/{_digits(campaign_id)}",
        "status": "ENABLED",
        "type": "SEARCH_STANDARD",
        "cpcBidMicros": str(cpc_micros),
    }}]
    return _stage("create_ad_group", cid, "adGroups", operations,
                  f"create ad group '{name}' with CPC {_money(cpc_micros)}")


@mcp.tool()
def create_keyword(customer_id: str, ad_group_id: str, texts: list[str], match_type: str,
                   confirm_token: str | None = None) -> dict:
    """Create several keywords in an ad group. match_type: EXACT, PHRASE or BROAD."""
    if confirm_token:
        return _execute(confirm_token)
    cid = _check_customer(customer_id)
    match_type = _check_match_type(match_type)
    if not texts:
        raise ValueError("pass at least one keyword.")
    operations = [{"create": {
        "adGroup": f"customers/{cid}/adGroups/{_digits(ad_group_id)}",
        "status": "ENABLED",
        "keyword": {"text": t, "matchType": match_type},
    }} for t in texts]
    return _stage("create_keyword", cid, "adGroupCriteria", operations,
                  f"create {len(texts)} {match_type} keyword(s): {', '.join(texts)}")


@mcp.tool()
def add_negative_keyword(customer_id: str, campaign_id: str, texts: list[str],
                         match_type: str = "BROAD",
                         confirm_token: str | None = None) -> dict:
    """Add several campaign-level negative keywords, blocking unwanted searches."""
    if confirm_token:
        return _execute(confirm_token)
    cid = _check_customer(customer_id)
    match_type = _check_match_type(match_type)
    if not texts:
        raise ValueError("pass at least one negative keyword.")
    operations = [{"create": {
        "campaign": f"customers/{cid}/campaigns/{_digits(campaign_id)}",
        "negative": True,
        "keyword": {"text": t, "matchType": match_type},
    }} for t in texts]
    return _stage("add_negative_keyword", cid, "campaignCriteria", operations,
                  f"add {len(texts)} negative keyword(s) to campaign {_digits(campaign_id)}: "
                  f"{', '.join(texts)}")


@mcp.tool()
def create_responsive_search_ad(customer_id: str, ad_group_id: str, final_url: str,
                                headlines: list[str], descriptions: list[str],
                                confirm_token: str | None = None) -> dict:
    """Create a responsive search ad. Needs 3-15 headlines (30 chars) and 2-4 descriptions (90)."""
    if confirm_token:
        return _execute(confirm_token)
    cid = _check_customer(customer_id)

    if not 3 <= len(headlines) <= 15:
        raise ValueError(f"headlines: 3 to 15 (got {len(headlines)}).")
    if not 2 <= len(descriptions) <= 4:
        raise ValueError(f"descriptions: 2 to 4 (got {len(descriptions)}).")
    long_headlines = [h for h in headlines if len(h) > 30]
    if long_headlines:
        raise ValueError(f"headlines over 30 characters: {long_headlines}")
    long_descriptions = [d for d in descriptions if len(d) > 90]
    if long_descriptions:
        raise ValueError(f"descriptions over 90 characters: {long_descriptions}")
    if not final_url.startswith("https://"):
        raise ValueError("final_url must start with https://")

    operations = [{"create": {
        "adGroup": f"customers/{cid}/adGroups/{_digits(ad_group_id)}",
        "status": "ENABLED",
        "ad": {
            "finalUrls": [final_url],
            "responsiveSearchAd": {
                "headlines": [{"text": h} for h in headlines],
                "descriptions": [{"text": d} for d in descriptions],
            },
        },
    }}]
    return _stage("create_responsive_search_ad", cid, "adGroupAds", operations,
                  f"create responsive search ad with {len(headlines)} headlines -> {final_url}")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
