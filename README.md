# google-ads-mcp-write

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant **make changes** to a Google Ads account: pause and resume campaigns, ad groups and keywords, add negative keywords, change budgets, create search campaigns and ads, switch bidding back to manual CPC and turn off auto-applied recommendations.

It is the write-side companion to Google's official, read-only [googleads/google-ads-mcp](https://github.com/googleads/google-ads-mcp). Use the official server to **read** (reports, search terms, change history) and this one to **act**.

Every change runs in two steps: the tool first asks Google to validate the change without applying it, and only executes when called again with the confirmation token it returned. Each plan and execution is written to an audit log.

## Tools

| Tool | What it does |
|---|---|
| `write_status` | Shows the active configuration: allowed accounts, budget limits, kill switch, audit log path |
| `list_campaigns` | Lists campaigns with id, status and daily budget (read-only, to build plans) |
| `set_campaign_status` | Pauses or resumes a campaign |
| `set_ad_group_status` | Pauses or resumes an ad group |
| `set_keyword_status` | Pauses or resumes several keywords of an ad group at once |
| `set_campaign_budget` | Changes a campaign's daily budget |
| `set_campaign_manual_cpc` | Switches campaigns to manual CPC, so ad group bids apply again |
| `pause_auto_apply_recommendations` | Pauses every active auto-apply recommendation subscription |
| `create_search_campaign` | Creates a Search campaign (always **paused**), with budget, location and language targeting and a first ad group |
| `create_ad_group` | Creates an ad group in an existing campaign |
| `create_keyword` | Adds keywords to an ad group (EXACT, PHRASE or BROAD) |
| `add_negative_keyword` | Adds campaign-level negative keywords |
| `create_responsive_search_ad` | Creates a responsive search ad (3–15 headlines, 2–4 descriptions) |

Amounts (budgets, CPC bids) are in the account's currency, as plain numbers: `30` means 30.00.

### The two-step flow

```text
you:        "pause the Brand campaign"
assistant → set_campaign_status(customer_id, campaign_id, "PAUSED")
server    → asks Google to validate the change (validateOnly), stores the plan
          ← {"status": "awaiting_confirmation", "summary": "campaign 123 -> PAUSED",
             "confirm_token": "9f41…", "valid_for_seconds": 3600}
assistant → set_campaign_status(…, confirm_token="9f41…")
server    → applies the change and appends it to the audit log
          ← {"status": "executed", …}
```

Pair this with your client's tool-approval prompts (see [Approving each change](#approving-each-change)) so that you approve each call yourself.

## Guardrails

- **Account allowlist.** The server only touches the accounts listed in `GOOGLE_ADS_MCP_WRITE_CUSTOMERS`. With the variable empty, every write is refused.
- **Budget limits.** A daily budget can't go above `GOOGLE_ADS_MCP_MAX_BUDGET_MICROS`, and a single change can't raise a budget by more than `GOOGLE_ADS_MCP_MAX_BUDGET_INCREASE_PCT`.
- **No deletions.** There is no REMOVE operation: things are paused, never deleted. New campaigns are created paused.
- **Kill switch.** `GOOGLE_ADS_MCP_WRITE_ENABLED=0` blocks every write without uninstalling anything.
- **Audit log.** Every plan and every execution, with the exact operations sent and Google's response, goes to a JSON Lines file.
- **Plans expire** after one hour and each confirmation token works once.

## Requirements

1. A Google Ads **developer token** with Basic or Standard access (Google Ads UI → Tools → API Center). A token with only Explorer/test access works on test accounts.
2. A Google Cloud project with the **Google Ads API** enabled.
3. Google credentials with the `https://www.googleapis.com/auth/adwords` scope, found through [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials).
4. Python 3.10 or newer.

### Getting credentials

The simplest route is your own Google account through `gcloud`, with an OAuth client of type **Desktop app** created in your Cloud project (APIs & Services → Credentials). Download its JSON and run:

```bash
gcloud auth application-default login \
  --client-id-file=/path/to/client_secret.json \
  --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform
gcloud auth application-default set-quota-project YOUR_CLOUD_PROJECT
```

The official read-only server uses the same credentials, so one login serves both.

> In Google Workspace organizations with session-control policies, these credentials may expire every day or so (`invalid_rapt` / "Reauthentication is needed"). Run the same command again when that happens.

## Install

```bash
pipx install git+https://github.com/andersonbuzzi/google-ads-mcp-write.git
```

or run it without installing:

```bash
uvx --from git+https://github.com/andersonbuzzi/google-ads-mcp-write.git google-ads-mcp-write
```

## Configuration

All configuration comes from environment variables, set in your MCP client's server entry.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `GOOGLE_ADS_DEVELOPER_TOKEN` | yes | — | Your developer token |
| `GOOGLE_ADS_MCP_WRITE_CUSTOMERS` | yes | — | Comma-separated customer ids the server may change, e.g. `1234567890,9876543210` |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | when using a manager account | — | Manager (MCC) id to act through |
| `GOOGLE_ADS_MCP_MAX_BUDGET_MICROS` | no | `200000000` | Highest daily budget allowed, in micros (200000000 = 200.00) |
| `GOOGLE_ADS_MCP_MAX_BUDGET_INCREASE_PCT` | no | `50` | Largest increase allowed in one budget change, in percent |
| `GOOGLE_ADS_MCP_DEFAULT_GEO_TARGETS` | no | — | Default location ids for new campaigns, e.g. `2840` (United States), `2076` (Brazil) |
| `GOOGLE_ADS_MCP_DEFAULT_LANGUAGES` | no | — | Default language ids for new campaigns, e.g. `1000` (English), `1014` (Portuguese) |
| `GOOGLE_ADS_MCP_AUDIT_LOG` | no | `~/.local/share/google-ads-mcp-write/audit.jsonl` | Where the audit log is written |
| `GOOGLE_ADS_MCP_WRITE_ENABLED` | no | `1` | `0` blocks every write |
| `GOOGLE_ADS_API_VERSION` | no | `v25` | Google Ads REST API version |
| `GOOGLE_CLOUD_PROJECT` | sometimes | — | Quota project, if your credentials don't carry one |

Location and language ids come from Google's [geo targets](https://developers.google.com/google-ads/api/data/geotargets) and [language codes](https://developers.google.com/google-ads/api/data/codes-formats#languages) tables.

### Connecting an MCP client

Most MCP clients accept a server entry in this format:

```json
{
  "mcpServers": {
    "google-ads-write": {
      "command": "google-ads-mcp-write",
      "env": {
        "GOOGLE_ADS_DEVELOPER_TOKEN": "your-token",
        "GOOGLE_ADS_MCP_WRITE_CUSTOMERS": "1234567890",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "9876543210"
      }
    }
  }
}
```

Check your client's documentation for where this file lives.

### Approving each change

If your client can ask before running a tool, turn that on for every tool that changes the account, which is all of them except `write_status` and `list_campaigns`:

`set_campaign_status`, `set_ad_group_status`, `set_keyword_status`, `set_campaign_budget`, `set_campaign_manual_cpc`, `pause_auto_apply_recommendations`, `create_search_campaign`, `create_ad_group`, `create_keyword`, `add_negative_keyword`, `create_responsive_search_ad`.

Together with the two-step flow, nothing changes in the account without you seeing the plan and saying yes.

### Using it with the official read-only server

Install [googleads/google-ads-mcp](https://github.com/googleads/google-ads-mcp) next to this one. A typical session: the assistant reads search terms through the official server, proposes negatives, and adds them through `add_negative_keyword` after you approve.

## Good to know

- **Auto-apply recommendations can undo your settings.** Google may switch bidding strategies or broaden keyword match types on its own when auto-apply is on. `pause_auto_apply_recommendations` turns off every subscription the API can address. Subscription types newer than the API version come back as `UNKNOWN` and can only be turned off in the UI (Recommendations → Auto-apply); the tool tells you how many there are.
- **Campaign creation is a chain** (budget → campaign → targeting → ad group). Google can only dry-run the first step, because the others reference resources that don't exist yet. If a later step fails, pass the created `budget_id` to retry without leaving an orphan budget.
- **Plans live in memory.** Restarting the server drops pending plans; just ask again.

## Development

```bash
git clone https://github.com/andersonbuzzi/google-ads-mcp-write.git
cd google-ads-mcp-write
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/pytest
```

The tests replace the Google Ads API with a fake, so they run offline and never touch a real account.

## License

[MIT](LICENSE). Not affiliated with or endorsed by Google. Google Ads is a trademark of Google LLC.
