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

### 1. Clone both repos as siblings

`pyproject.toml` resolves the CLI through a relative path, so the directory
names and layout matter:

```
your-code-dir/
├── nemlig_cli/     <- underscore
└── nemlig-mcp/     <- hyphen
```

```bash
cd ~/code
git clone https://github.com/eisbaw/nemlig_cli.git
git clone https://github.com/kraenhansen/nemlig-mcp.git
cd nemlig-mcp
uv sync
```

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

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
| `ModuleNotFoundError: nemlig_cli` | Repos are not siblings, or the CLI directory is not named `nemlig_cli` |
| Tool calls fail with `HTTPError` | Wrong username/password — nemlig returns 401 from the login endpoint |

## Notes on the upstream CLI

Two things this package works around, both worth fixing upstream:

1. **Progress spinners write to stdout**, including from a background thread in
   `login()`. On a stdio MCP server stdout *is* the JSON-RPC transport, so every
   CLI call is wrapped in a redirect ([`_compat.py`](src/nemlig_mcp/_compat.py)).
   Fix: print progress to stderr.
2. **Optional dependencies are declared as required.** `opencv-python`, `pyzbar`,
   `Pillow`, `anthropic`, `google-api-python-client` and `openfoodfacts` are all
   guarded with `try/except ImportError` in the code, but listed as hard
   dependencies, so installing the CLI as a library pulls in all of them. Fix:
   move them to `[project.optional-dependencies]`.

## Development

```bash
uv run pytest
```

The tests make no network calls and need no credentials.

## Disclaimer

Not affiliated with nemlig.com. Uses a private API that may change or break, and
automated access may conflict with their terms of service.

## License

MIT
