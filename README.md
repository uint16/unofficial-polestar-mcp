# unofficial-polestar-mcp

> [!IMPORTANT]
> **Disclaimer:** This is an independent, community-built project. It is **not affiliated with, associated with, endorsed by, or supported by Polestar** in any way. "Polestar" is a trademark of its respective owner and is used here only to describe compatibility. This project relies on unofficial, reverse-engineered APIs that may break at any time. Use at your own risk.

MCP server for controlling your Polestar from Claude — climate, charging, locks, location and vehicle status via natural language.

unofficial-polestar-mcp is a Model Context Protocol server that connects Claude to your Polestar. Ask Claude to precondition the cabin, set charge limits, check battery and range, find where you parked, or flash the lights — all through the same cloud APIs the official app uses. Safety-critical actions (unlock, open windows) require explicit confirmation. Built on the [unofficial-polestar-api](https://github.com/kildahldev/unofficial-polestar-api) library. Not affiliated with Polestar.

## Tools

| Category | Tools |
| --- | --- |
| Vehicles | `list_vehicles` |
| Status | `get_battery`, `get_location`, `get_exterior`, `get_odometer`, `get_health`, `get_availability`, `get_weather`, `wake_vehicle` |
| Climate | `get_climate`, `start_climate` (temperature + seat/steering-wheel heating), `stop_climate` |
| Charging | `get_charging_settings`, `set_charge_limit`, `set_amp_limit`, `start_charging`, `stop_charging` |
| Locks | `lock_car`, `unlock_car`*, `unlock_trunk`*, `honk_and_flash` |
| Windows | `open_windows`*, `close_windows` |

\* Requires explicit user confirmation (`confirm=true`) before the command is sent.

If your account has a single vehicle it is selected automatically; otherwise pass a `vin` argument or set `POLESTAR_VIN`.

## Installation

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

Tokens are cached on disk (mode `0600` on macOS/Linux; on Windows the file inherits your user profile's ACLs) so restarts reuse the refresh token instead of re-running the full login.

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

## Example prompts

- "How charged is my Polestar and what's the range?"
- "Precondition the car to 21° and turn on the driver's seat heater."
- "Set the charge limit to 80% and start charging now."
- "Where did I park?"
- "Flash the lights, I can't find the car."

## Safety & disclaimer

- Unlocking the car or trunk and opening windows are gated behind an explicit confirmation step — Claude must ask you before sending those commands.
- This project uses unofficial, reverse-engineered APIs. They can break or change at any time. Use at your own risk.
- Not affiliated with, endorsed by, or supported by Polestar.

## License

MIT
