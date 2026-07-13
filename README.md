# unofficial-polestar-mcp

> [!IMPORTANT]
> **Disclaimer:** This is an independent, community-built project. It is **not affiliated with, associated with, endorsed by, or supported by Polestar** in any way. "Polestar" is a trademark of its respective owner and is used here only to describe compatibility. This project relies on unofficial, reverse-engineered APIs that may break at any time. Use at your own risk.

MCP server for controlling your Polestar from any MCP-capable AI assistant — Claude, ChatGPT/Codex, Gemini, and others. Climate, charging, locks, location and vehicle status via natural language.

unofficial-polestar-mcp is a [Model Context Protocol](https://modelcontextprotocol.io) server that connects AI assistants to your Polestar. Ask your assistant to precondition the cabin, set charge limits, check battery and range, find where you parked, or flash the lights — all through the same cloud APIs the official app uses. Safety-critical actions (unlock, open windows) are disabled by default and require explicit confirmation. Built on the [unofficial-polestar-api](https://github.com/kildahldev/unofficial-polestar-api) library. Not affiliated with Polestar.

## Tools

| Category | Tools |
| --- | --- |
| Vehicles | `list_vehicles` |
| Status | `get_battery`, `get_location`, `get_exterior`, `get_odometer`, `get_health`, `get_availability`, `get_weather`, `wake_vehicle` |
| Climate | `get_climate`, `start_climate` (temperature + seat/steering-wheel heating), `stop_climate` |
| Charging | `get_charging_settings`, `set_charge_limit`, `set_amp_limit`, `start_charging`, `stop_charging` |
| Locks | `lock_car`, `unlock_car`*, `unlock_trunk`*, `honk_and_flash` |
| Windows | `open_windows`*, `close_windows` |

\* **Disabled by default.** Tools that expose the vehicle (unlock, unlock trunk, open windows) are only registered when the server is started with `POLESTAR_ENABLE_UNLOCK=1`, and additionally ask for confirmation (`confirm=true`) before sending the command.

All tools carry MCP annotations: status tools are marked `readOnlyHint`, exposure tools `destructiveHint`, so MCP clients can prompt accordingly.

If your account has a single vehicle it is selected automatically; otherwise pass a `vin` argument or set `POLESTAR_VIN`.

### Command pre-flight behavior

Before sending a command, the server:

- **Serializes commands per vehicle** — one in-flight command per car, so parallel tool calls can't race each other.
- **Checks reachability** — if the car is asleep it sends one wake-up and re-checks; if it's unreachable for another reason (in use, OTA update, …) the command is still sent and the result says so.
- **Reads the relevant state first** and includes it in the result — e.g. `start_charging` reports charge level and charger connection, `lock_car` warns if a door, hood, or tailgate is open.
- **Skips already-satisfied commands** — "start charging" while charging, "lock" while locked, or setting a charge limit to its current value returns `"command": "skipped"` with the reason instead of firing a redundant command.

State reads are best-effort: if a pre-check read fails, the command is still sent and the result carries a warning — the Polestar backend remains the final validator.

## Installation

### Option A: MCP bundle (easiest, Claude Desktop)

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (the bundle uses it to fetch dependencies on first launch).
2. Download `unofficial-polestar-mcp-<version>.mcpb` from the [latest release](https://github.com/uint16/unofficial-polestar-mcp/releases/latest).
3. Double-click the file (or drag it into Claude Desktop → Settings → Extensions).
4. Enter your Polestar email and password in the extension settings — the password is stored in your OS keychain, not in a config file. Unlock/open-windows tools stay disabled unless you flip the toggle.

### Option B: from source

Requires Python 3.11+.

**macOS / Linux:**

```sh
git clone https://github.com/<you>/unofficial-polestar-mcp
cd unofficial-polestar-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

The server binary is at `.venv/bin/polestar-mcp`.

**Windows (PowerShell):**

```powershell
git clone https://github.com/<you>/unofficial-polestar-mcp
cd unofficial-polestar-mcp
python -m venv .venv
.venv\Scripts\pip install -e .
```

The server binary is at `.venv\Scripts\polestar-mcp.exe`.

## Configuration

The server authenticates with your Polestar account credentials via environment variables:

| Variable | Required | Description |
| --- | --- | --- |
| `POLESTAR_EMAIL` | yes | Polestar account email |
| `POLESTAR_PASSWORD` | yes | Polestar account password |
| `POLESTAR_VIN` | no | Default vehicle when the account has several |
| `POLESTAR_TOKEN_FILE` | no | Token cache path (default `~/.polestar-mcp/tokens.json`) |
| `POLESTAR_ENABLE_UNLOCK` | no | Set to `1` to register the `unlock_car`, `unlock_trunk`, and `open_windows` tools (off by default) |

Tokens are cached on disk (mode `0600` on macOS/Linux; on Windows the file inherits your user profile's ACLs) so restarts reuse the refresh token instead of re-running the full login.

## Client setup

This is a standard stdio MCP server — it works with any MCP-capable client and is not tied to a specific AI provider.

### Claude Desktop (macOS / Windows)

Add to your Claude Desktop config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "polestar": {
      "command": "/path/to/unofficial-polestar-mcp/.venv/bin/polestar-mcp",
      "env": {
        "POLESTAR_EMAIL": "you@example.com",
        "POLESTAR_PASSWORD": "your-password"
      }
    }
  }
}
```

On Windows, use the `Scripts` binary and escape backslashes in JSON:

```json
"command": "C:\\path\\to\\unofficial-polestar-mcp\\.venv\\Scripts\\polestar-mcp.exe"
```

### Claude Code (macOS / Linux / Windows)

```sh
claude mcp add polestar \
  -e POLESTAR_EMAIL=you@example.com \
  -e POLESTAR_PASSWORD=your-password \
  -- /path/to/unofficial-polestar-mcp/.venv/bin/polestar-mcp
```

On Windows, point to `.venv\Scripts\polestar-mcp.exe` instead.

### OpenAI Codex CLI

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.polestar]
command = "/path/to/unofficial-polestar-mcp/.venv/bin/polestar-mcp"
env = { POLESTAR_EMAIL = "you@example.com", POLESTAR_PASSWORD = "your-password" }
```

(ChatGPT's own connector support is limited to remote MCP servers; for local stdio servers like this one, use Codex CLI or another desktop client.)

### Gemini CLI

Add to `~/.gemini/settings.json` (or `.gemini/settings.json` in a project):

```json
{
  "mcpServers": {
    "polestar": {
      "command": "/path/to/unofficial-polestar-mcp/.venv/bin/polestar-mcp",
      "env": {
        "POLESTAR_EMAIL": "you@example.com",
        "POLESTAR_PASSWORD": "your-password"
      }
    }
  }
}
```

### Other MCP clients (Cursor, VS Code, Windsurf, …)

The same `command` + `env` configuration shape works for VS Code Copilot, Cursor, Windsurf, and any other MCP client.

Notes that apply to every client:

- The `POLESTAR_ENABLE_UNLOCK` gate is enforced by the server itself, so it protects you regardless of client or model.
- The `readOnlyHint`/`destructiveHint` tool annotations are hints — clients differ in whether they surface them. Keep per-tool confirmation enabled and avoid auto-approve/YOLO modes with this server.

## Example prompts

- "How charged is my Polestar and what's the range?"
- "Precondition the car to 21° and turn on the driver's seat heater."
- "Set the charge limit to 80% and start charging now."
- "Where did I park?"
- "Flash the lights, I can't find the car."

## Safety & disclaimer

- **Exposure tools are opt-in.** `unlock_car`, `unlock_trunk`, and `open_windows` are not available at all unless you start the server with `POLESTAR_ENABLE_UNLOCK=1`. This is an environment-level switch that a prompt-injected model cannot flip.
- **Do not blanket-auto-approve this server's tools in your MCP client.** The in-tool `confirm=true` step is a courtesy prompt for the model, not a security boundary — anything the model reads (web pages, emails, documents) could try to talk it into passing `confirm=true`. Your client's per-call tool approval is the real gate for destructive actions; the tools are annotated (`readOnlyHint`/`destructiveHint`) so clients can distinguish safe reads from vehicle commands.
- Vehicle data returned by tools (location, VIN, registration) enters the model context and is processed under your AI provider's data policy — whichever assistant you connect (Claude, ChatGPT, Gemini, …).
- This project uses unofficial, reverse-engineered APIs. They can break or change at any time. Use at your own risk.
- Not affiliated with, endorsed by, or supported by Polestar.

## License

MIT
