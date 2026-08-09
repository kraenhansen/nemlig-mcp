# nemlig-mcp
Unofficial local MCP for nemlig.com, danish delivery service

Built as a thin layer over [`nemlig_cli`](https://github.com/eisbaw/nemlig_cli),
which already solves the hard parts: the three-step XSRF/bearer/login flow, the
cookie-backed session, the two different API hosts, and the `GetAsJson`
endpoints. This package adds a token-refresh loop, a privacy filter, and a
curated tool surface.

## Tools

| Tool | Writes? | Description |
|---|---|---|
| `search_products` | no | Search the catalogue |
| `get_product_details` | no | Nutrition, allergens, attributes |
| `get_basket` | no | Current basket contents |
| `add_to_basket` | **yes** | Add a product to the basket |
| `get_order_history` | no | Previous orders |
| `get_order_details` | no | Line items of one past order |

## What this server deliberately cannot do

**It cannot place an order or read your payment cards.** Nemlig's API exposes
`POST /webapi/Order/PlaceOrderLoggedIn` (which charges a saved card) and
`GET /webapi/Checkout/GetCreditCards`. `nemlig_cli` implements neither, and this
server does not add them.

The tool surface is an allowlist, not a filtered view of the whole API, so an
endpoint cannot become reachable by accident. A test asserts this
([`tests/test_server.py`](tests/test_server.py)). `add_to_basket` is the only
tool that changes state, and filling a basket is reversible on the website.

## Privacy

Basket and order responses embed the account holder's name, street address and
phone number. Everything a tool returns lands in the model's context and in the
conversation transcript, so those fields are replaced with a marker before they
leave the server. See [`privacy.py`](src/nemlig_mcp/privacy.py).

## Setup

### 1. Install

```bash
git clone https://github.com/kraenhansen/nemlig-mcp.git
cd nemlig-mcp
uv sync
```

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+. `nemlig_cli` is
pulled from git automatically — no separate checkout needed.

### 2. Provide credentials

Two options. **Prefer the config file** — an MCP config with a password in it
is easy to commit by accident.

```bash
mkdir -p ~/.config/nemlig
cat > ~/.config/nemlig/login.json <<'EOF'
{"username": "you@example.com", "password": "your-password"}
EOF
chmod 600 ~/.config/nemlig/login.json
```

This is the same file the CLI uses, so both share one login. Alternatively set
`NEMLIG_USER` / `NEMLIG_PASS` in the environment, which takes precedence.

### 3. Register the server

```bash
claude mcp add nemlig -- uv --directory ~/code/nemlig-mcp run nemlig-mcp
```

Or add it to `.mcp.json` (project-local) or `~/.claude.json` (global). Use an
absolute path — `~` is not expanded inside `args`:

```json
{
  "mcpServers": {
    "nemlig": {
      "command": "uv",
      "args": ["--directory", "/Users/you/code/nemlig-mcp", "run", "nemlig-mcp"]
    }
  }
}
```

If you would rather not use the config file, add credentials here instead —
and make sure the file is gitignored:

```json
      "env": { "NEMLIG_USER": "you@example.com", "NEMLIG_PASS": "..." }
```

### 4. Verify

```bash
uv run pytest                        # 6 tests, no network, no credentials
uv run python tests/smoke_stdio.py   # spawns the server, lists its tools
```

Then ask Claude something like *"search nemlig for kaffebønner"*. The first
call logs in, which takes a second or two; tokens are cached for 240s after
that.

Troubleshooting:

| Symptom | Cause |
|---|---|
| `No nemlig.com credentials found` | Step 2 not done, or the config file is not valid JSON |
| `ModuleNotFoundError: nemlig_cli` | `uv sync` did not complete — rerun it |
| Tool calls fail with `HTTPError` | Wrong username/password — nemlig returns 401 from the login endpoint |

## Relationship to the upstream CLI

Building this surfaced two problems in `nemlig_cli`, both fixed upstream in
[eisbaw/nemlig_cli#3](https://github.com/eisbaw/nemlig_cli/pull/3):

1. **`login()` could hang forever.** The progress spinner was started before the
   request sequence but only stopped on the success path, on a non-daemon
   thread — so a wrong password left the process spinning instead of raising.
2. **Progress was written to stdout**, which on a stdio MCP server is the
   JSON-RPC transport. It now renders to stderr, and only when stderr is a tty.

Because of that fix, `nemlig-cli` is pinned to a commit that includes it. The
`quiet()` wrapper in [`_compat.py`](src/nemlig_mcp/_compat.py) is kept anyway:
a single stray `print()` anywhere in a dependency would corrupt the protocol
stream, and that is cheap insurance against a failure mode this severe.

## Development

```bash
uv run pytest                        # 6 tests, no network, no credentials
uv run python tests/smoke_stdio.py   # end-to-end over stdio
```

To work against a local checkout of the CLI, uncomment the `[tool.uv.sources]`
block in `pyproject.toml` and clone `nemlig_cli` as a sibling directory.

## Disclaimer

Not affiliated with nemlig.com. Uses a private API that may change or break, and
automated access may conflict with their terms of service.

## License

MIT
