# cloud-trading-agent

Order-execution agent for Taiwan index futures (TXF/MXF/TMF), talking to
**Fubon Securities' Fubon Neo API**, deployed on GCP Cloud Run. It receives
trading commands via Google Cloud Pub/Sub push and executes them against a
Fubon brokerage account — it is not a strategy engine; trading signals come
from an upstream producer (e.g. `slash-futures`) that publishes commands to
Pub/Sub.

This is a Fubon Neo port of [`etensword-agent`](../etensword-agent), which
targets Sinopac's Shioaji API. The wire contract (Pub/Sub payload shape,
command vocabulary) is fully compatible between the two, so this can act as a
drop-in broker swap for a given trader/account.

## Scope

- **Futures (TXF/MXF/TMF)**: fully implemented — `Buy`, `CloseAndBuy`,
  `CloseAndSell`, `MayDay`, `HasOpenInterest` (with the MXF→also-check-TXF
  special case), `ListProfitLoss`.
- **Stock orders**: explicitly out of scope for this phase. Every stock
  command path (`product.isnumeric()`) returns a `"not implemented yet
  (TODO)"` result rather than executing anything.
- **`Sell`**: not implemented, for either instrument type (matches the
  reference repo's scope too).

## Architecture

```
main.py                       FastAPI app — Pub/Sub push endpoint (`POST /`), health check (`GET /health`)
fubon_agent/
├── __init__.py                get_config() — loads YAML config, sets GOOGLE_APPLICATION_CREDENTIALS
├── agent_commands.py          AgentCommand constants — the wire contract with the upstream producer
├── agent_logging.py           logger factory (console + optional file handler)
└── api/
    ├── base.py                OrderAgentBase (command dispatch/expiry), process_order (dedup + feedback)
    └── fubon_api.py            OrderAgent — Fubon Neo session, order placement, retry/batching logic
```

Key behaviors in `fubon_api.py`:

- **Singleton session** shared across requests (`get_or_create_agent()`).
  Gunicorn runs with `--workers 1`. Cloud Run uses `--concurrency=50` with
  `--max-instances=1` so Pub/Sub bursts are accepted on one instance without
  opening multiple Fubon logins; order execution is serialized by an
  in-process lock in `main.py`.
- **Command claim**: each `command_id` is claimed in GCS with
  `if_generation_match=0` *before* processing (atomic dedup across
  instances/restarts).
- **Reconnect**: Fubon's `on_event` callback signals disconnect via event
  code `"300"`. On disconnect, the agent logs out, re-instantiates
  `FubonSDK()`, re-logs in, and re-registers all callbacks (they do not
  survive a reconnect). A non-blocking lock prevents concurrent reconnect
  attempts from the request path and the SDK's own callback thread.
- **Order batching**: any order quantity is split into chunks of
  `ORDER_BATCH_QTY = 20`.
- **Price-chasing retry**: `CloseAndBuy`/`CloseAndSell` re-chase market
  (`PRICE_CHASE_BUFFER`) on place, then on `NeedRetryError` step by
  `PRICE_RETRY_STEP` (±50) with soft offset `PRICE_RETRY_LIMIT_OFFSET`
  (±200); market can push past the soft cap so fills still complete.
- **Rate limiting**: Fubon caps accounting/position queries at 5 req/s — all
  `sdk.accounting`/`sdk.futopt` query calls go through a shared
  `RateLimiter` to avoid tripping this.
- **Margin check**: proactively calls `query_estimate_margin()` before
  placing an order (Fubon has no confirmed fine-grained "insufficient
  margin" status code, unlike Shioaji's `99Q9`/`99QB`).

See `fubon_agent/api/fubon_api.py` for inline notes on which parts of the
Fubon SDK surface are directly confirmed by [Fubon's docs](https://www.fbs.com.tw/TradeAPI/llms.txt)
vs. best-effort (mainly: exact position/order-result object field names,
which aren't fully documented).

## Configuration

Config is YAML, loaded from the path in the `FUBON_AGENT_CONF` env var
(falls back to `settings.yaml`). See `config/agent_settings.yaml` for the
template:

```yaml
order_agent:
  order_agent_type: fubon_api
  agent_id: <unique id>

fubon_api:
  person_id: <Fubon login ID>
  password: <Fubon login password>   # first-time 連線測試 only
  cert_path: <path to .pfx cert>
  cert_password: <cert password>
  api_key: <Fubon API Key secret>    # production runtime login (default)
  login_method: apikey               # apikey (default) | password
  is_dry_run: false

agent_account_mapping:
  <agent_id_1>: <account_id_1>

gcp:
  project_id: EtenSword
  topic: FuturesBot
  topic_feedback: FuturesAgent
  subscription: <subscription name>
  google_application_credentials: <path to service account json>

logging:
  log_file_path: <path>FubonAgent-{date}.log
  log_level: INFO
```

Real credentials, `.pfx` certs, and per-trader configs live under
`docker/<trader>_fubon/.cert/` and are **never committed** (see
`.gitignore`).

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e .

export FUBON_AGENT_CONF=$(pwd)/config/agent_settings.yaml  # fill in real values first
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

Note: the `fubon_neo` SDK is **not on PyPI** — it must be downloaded once
from Fubon's authenticated SDK portal and installed manually
(`pip install fubon_neo-*.whl`) before `main.py` can actually run against a
real account.

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/
```

All 27 tests run against a mocked `fubon_neo` module (stubbed in
`tests/conftest.py`, since the real SDK can't be installed from PyPI) — no
live credentials or network access required. Coverage: command
dispatch/expiry/dedup (`test_base.py`), order batching/price-chasing
retry/margin handling/reconnect/rate-limiting (
`test_order_agent_fubon_api_futures.py`), and the stock-order TODO scope
boundary (`test_order_agent_fubon_api_stocks.py`).

## Deployment

Deploys to the same GCP projects as `etensword-agent`
(`etensword-order-agent` for Artifact Registry/Cloud Run,
`etensword` for Pub/Sub/the feedback Cloud Function/the GCS dedup bucket),
using the same 3-tier Docker image structure:

1. `docker/agent_base/` — base image (Python slim + the manually-staged
   `fubon_neo` wheel). Build with `sh docker/agent_base/cloud_build_agent_base.sh`
   (this pulls the wheel from a private GCS bucket via `gsutil cp` first —
   see the script for the bucket path).
2. `docker/agent_app/` — shared app image on top of the base image. Build
   with `sh docker/agent_app/cloud_build_agent_app.sh [BUILD_VERSION]`.
3. `docker/<trader>_fubon/` — per-trader image with that trader's
   `.cert/` baked in. `docker/example_fubon/` is a template — copy it,
   rename `SERVICE_NAME`, add real `.cert/` contents, and add a line to
   `deploy_all_agents.sh`. No code changes needed to onboard a new trader.

```bash
sh deploy_all_agents.sh
```

After deploying a new Cloud Run service, run
`scripts/add_cloud_run_permission.sh` (edit `SERVICE_NAME`/`REGION` first) so
the Pub/Sub push subscription can invoke it, then coordinate with whoever
owns the upstream producer repo to wire up its subscription.

## Known open risks before first live cutover

- Exact field names on Fubon's position/order-result objects aren't fully
  documented — `fubon_agent/api/fubon_api.py` uses defensive `getattr`
  fallbacks; confirm against the real installed SDK.
- No confirmed status code for "insufficient margin" specifically (only a
  generic failure code) — mitigated via a proactive `query_estimate_margin()`
  check, but not fully closed.
- No confirmed sandbox/paper-trading environment for Fubon Neo — validate
  via `is_dry_run: true` plus small real-money trades first.

See the planning doc for full detail:
`~/.claude/plans/python-3-11-fubon-neo-synchronous-hejlsberg.md`.
