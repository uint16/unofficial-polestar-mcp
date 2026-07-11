"""MCP server exposing Polestar vehicle status and controls.

Unofficial community project — not affiliated with, endorsed by, or supported
by Polestar. Uses reverse-engineered APIs that may change without notice.

Authentication comes from the POLESTAR_EMAIL / POLESTAR_PASSWORD environment
variables. Tokens are persisted to POLESTAR_TOKEN_FILE (default
~/.polestar-mcp/tokens.json) so subsequent launches reuse the refresh token
instead of re-running the full login flow.

Security model
--------------
Tools that expose the vehicle (unlock, unlock trunk, open windows) are NOT
registered unless POLESTAR_ENABLE_UNLOCK is set to a truthy value. A tool
parameter like ``confirm=True`` can be set by a prompt-injected model, so it
is advisory only; the real security boundaries are (1) this environment-level
opt-in, which the model cannot change, and (2) the MCP client's per-call tool
approval. All tools carry MCP annotations (readOnlyHint / destructiveHint) so
clients can treat exposure tools with appropriate caution — do not configure
your client to blanket-auto-approve this server's tools.
"""

from __future__ import annotations

import os
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from polestar_api import PolestarApi
from polestar_api.auth import FileTokenStore
from polestar_api.models.charging import ChargeTargetLevelSettingType
from polestar_api.models.climatization import HeatingIntensity
from polestar_api.models.honkflash import HonkFlashAction
from polestar_api.vehicle import Vehicle

mcp = FastMCP("polestar")

_api: PolestarApi | None = None
_vehicles: dict[str, Vehicle] = {}

DEFAULT_TOKEN_FILE = "~/.polestar-mcp/tokens.json"

# Reads vehicle state only.
_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
# Sends a command, but one that cannot expose the vehicle and is safe to repeat.
_CONTROL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
# Audible/visible side effects; repeating repeats the effect.
_ALERT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
# Exposes the vehicle (unlock, open windows). Clients should never auto-approve.
_EXPOSES_VEHICLE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# Exposure tools (unlock, open windows) are opt-in via the environment, which
# the model cannot modify — see "Security model" in the module docstring.
UNLOCK_TOOLS_ENABLED = _env_flag("POLESTAR_ENABLE_UNLOCK")

_HEAT_LEVELS = {
    "": HeatingIntensity.UNSPECIFIED,
    "off": HeatingIntensity.OFF,
    "low": HeatingIntensity.LEVEL1,
    "medium": HeatingIntensity.LEVEL2,
    "high": HeatingIntensity.LEVEL3,
}

_HONK_ACTIONS = {
    "flash": HonkFlashAction.FLASH,
    "honk": HonkFlashAction.HONK,
    "honk_and_flash": HonkFlashAction.HONK_AND_FLASH,
}

_SOC_SETTING_TYPES = {
    "daily": ChargeTargetLevelSettingType.DAILY,
    "long_trip": ChargeTargetLevelSettingType.LONG_TRIP,
    "custom": ChargeTargetLevelSettingType.CUSTOM,
}


# -- Plumbing ----------------------------------------------------------------


async def _get_api() -> PolestarApi:
    global _api
    if _api is None:
        email = os.environ.get("POLESTAR_EMAIL")
        password = os.environ.get("POLESTAR_PASSWORD")
        if not email or not password:
            raise RuntimeError(
                "POLESTAR_EMAIL and POLESTAR_PASSWORD environment variables must "
                "be set in the MCP server configuration."
            )
        token_file = os.environ.get("POLESTAR_TOKEN_FILE", DEFAULT_TOKEN_FILE)
        api = PolestarApi(email, password, token_store=FileTokenStore(token_file))
        await api.async_init()
        _api = api
    return _api


async def _get_vehicle(vin: str | None = None) -> Vehicle:
    """Resolve a vehicle: explicit VIN > POLESTAR_VIN env > sole vehicle."""
    api = await _get_api()
    if not _vehicles:
        for v in await api.get_vehicles():
            _vehicles[v.vin] = v
    if not _vehicles:
        raise RuntimeError("No vehicles found on this Polestar account.")

    vin = vin or os.environ.get("POLESTAR_VIN") or ""
    if vin:
        if vin not in _vehicles:
            raise ValueError(
                f"VIN {vin!r} not found. Known vehicles: {', '.join(_vehicles)}"
            )
        return _vehicles[vin]
    if len(_vehicles) == 1:
        return next(iter(_vehicles.values()))
    raise ValueError(
        "Multiple vehicles on this account — pass a vin. "
        f"Known vehicles: {', '.join(_vehicles)}"
    )


def _plain(obj: Any) -> Any:
    """Convert library dataclasses/enums into JSON-friendly structures."""
    if isinstance(obj, IntEnum):
        return obj.name
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if is_dataclass(obj) and not isinstance(obj, type):
        field_names = [f.name for f in fields(obj)]
        # Timestamps become ISO-8601 strings.
        if field_names in (["seconds", "nanos"], ["seconds"]):
            seconds = getattr(obj, "seconds", 0)
            if not seconds:
                return None
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        return {name: _plain(getattr(obj, name)) for name in field_names}
    if isinstance(obj, (list, tuple)):
        return [_plain(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    return str(obj)


def _result(payload: Any, **extra: Any) -> dict[str, Any]:
    data = _plain(payload)
    if data is None:
        data = {"note": "Vehicle did not report data for this request."}
    if not isinstance(data, dict):
        data = {"result": data}
    data.update(extra)
    return data


def _heat(level: str, name: str) -> HeatingIntensity:
    try:
        return _HEAT_LEVELS[level.strip().lower()]
    except KeyError:
        raise ValueError(
            f"Invalid {name} {level!r}: use one of {', '.join(k for k in _HEAT_LEVELS if k)}"
        ) from None


# -- Vehicles ----------------------------------------------------------------


@mcp.tool(annotations=_READ)
async def list_vehicles() -> list[dict[str, Any]]:
    """List all Polestar vehicles on the account with VIN, model, year and registration."""
    api = await _get_api()
    vehicles = await api.get_vehicles()
    _vehicles.clear()
    for v in vehicles:
        _vehicles[v.vin] = v
    return [
        {
            "vin": v.vin,
            "model": v.model_name,
            "model_year": v.model_year,
            "registration": v.registration_no,
        }
        for v in vehicles
    ]


# -- Status ------------------------------------------------------------------


@mcp.tool(annotations=_READ)
async def get_battery(vin: str = "") -> dict[str, Any]:
    """Battery status: charge level %, range, charging state, power, and time to full."""
    car = await _get_vehicle(vin or None)
    return _result(await car.get_battery())


@mcp.tool(annotations=_READ)
async def get_location(vin: str = "", parked: bool = False) -> dict[str, Any]:
    """Vehicle position (last known, or last parked if parked=True), with a map link."""
    car = await _get_vehicle(vin or None)
    loc = await car.get_parked_location() if parked else await car.get_location()
    data = _result(loc)
    coord = data.get("coordinate") or {}
    if coord.get("latitude") or coord.get("longitude"):
        data["map_url"] = (
            f"https://maps.google.com/?q={coord['latitude']},{coord['longitude']}"
        )
    return data


@mcp.tool(annotations=_READ)
async def get_exterior(vin: str = "") -> dict[str, Any]:
    """Exterior status: doors, windows, sunroof, hood, tailgate, locks and alarm."""
    car = await _get_vehicle(vin or None)
    return _result(await car.get_exterior())


@mcp.tool(annotations=_READ)
async def get_odometer(vin: str = "") -> dict[str, Any]:
    """Odometer reading and trip meters."""
    car = await _get_vehicle(vin or None)
    return _result(await car.get_odometer())


@mcp.tool(annotations=_READ)
async def get_health(vin: str = "") -> dict[str, Any]:
    """Vehicle health: service warnings, brake fluid, and tyre pressures."""
    car = await _get_vehicle(vin or None)
    return _result(await car.get_health())


@mcp.tool(annotations=_READ)
async def get_availability(vin: str = "") -> dict[str, Any]:
    """Whether the vehicle is online/reachable, and why not if unavailable."""
    car = await _get_vehicle(vin or None)
    return _result(await car.get_availability())


@mcp.tool(annotations=_READ)
async def get_weather(vin: str = "") -> dict[str, Any]:
    """Weather (temperature) at the car's current location."""
    car = await _get_vehicle(vin or None)
    return _result(await car.get_weather())


@mcp.tool(annotations=_CONTROL)
async def wake_vehicle(vin: str = "") -> dict[str, Any]:
    """Wake the car from sleep so fresh data and commands go through."""
    car = await _get_vehicle(vin or None)
    return _result(await car.wakeup())


# -- Climate -----------------------------------------------------------------


@mcp.tool(annotations=_READ)
async def get_climate(vin: str = "") -> dict[str, Any]:
    """Climatization (preconditioning) status."""
    car = await _get_vehicle(vin or None)
    return _result(await car.get_climate())


@mcp.tool(annotations=_CONTROL)
async def start_climate(
    vin: str = "",
    temperature_celsius: float = 0.0,
    front_left_seat: str = "",
    front_right_seat: str = "",
    rear_left_seat: str = "",
    rear_right_seat: str = "",
    steering_wheel: str = "",
) -> dict[str, Any]:
    """Start climatization (precondition the cabin).

    temperature_celsius 0 uses the car's default. Seat/steering-wheel heating
    levels: off, low, medium, high (empty string leaves it unchanged).
    """
    car = await _get_vehicle(vin or None)
    return _result(
        await car.start_climate(
            temperature=temperature_celsius,
            front_left_seat=_heat(front_left_seat, "front_left_seat"),
            front_right_seat=_heat(front_right_seat, "front_right_seat"),
            rear_left_seat=_heat(rear_left_seat, "rear_left_seat"),
            rear_right_seat=_heat(rear_right_seat, "rear_right_seat"),
            steering_wheel=_heat(steering_wheel, "steering_wheel"),
        )
    )


@mcp.tool(annotations=_CONTROL)
async def stop_climate(vin: str = "") -> dict[str, Any]:
    """Stop climatization."""
    car = await _get_vehicle(vin or None)
    return _result(await car.stop_climate())


# -- Charging ----------------------------------------------------------------


@mcp.tool(annotations=_READ)
async def get_charging_settings(vin: str = "") -> dict[str, Any]:
    """Charging settings: target charge level (SOC), amp limit, and charge timer."""
    car = await _get_vehicle(vin or None)
    return {
        "target_soc": _plain(await car.get_target_soc()),
        "amp_limit": _plain(await car.get_amp_limit()),
        "charge_timer": _plain(await car.get_charge_timer()),
    }


@mcp.tool(annotations=_CONTROL)
async def set_charge_limit(
    percent: int, vin: str = "", setting_type: str = "daily"
) -> dict[str, Any]:
    """Set the target charge level (0-100%). setting_type: daily, long_trip, or custom."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    kind = _SOC_SETTING_TYPES.get(setting_type.strip().lower())
    if kind is None:
        raise ValueError(
            f"Invalid setting_type {setting_type!r}: use daily, long_trip, or custom"
        )
    car = await _get_vehicle(vin or None)
    return _result(await car.set_target_soc(percent, kind))


@mcp.tool(annotations=_CONTROL)
async def set_amp_limit(amps: int, vin: str = "") -> dict[str, Any]:
    """Set the AC charging amperage limit."""
    car = await _get_vehicle(vin or None)
    return _result(await car.set_amp_limit(amps))


@mcp.tool(annotations=_CONTROL)
async def start_charging(vin: str = "") -> dict[str, Any]:
    """Start charging immediately (overrides timers). The car must be plugged in."""
    car = await _get_vehicle(vin or None)
    return {"status": _plain(await car.start_charging())}


@mcp.tool(annotations=_CONTROL)
async def stop_charging(vin: str = "") -> dict[str, Any]:
    """Stop or pause an active charging session."""
    car = await _get_vehicle(vin or None)
    return {"status": _plain(await car.stop_charging())}


# -- Locks & security --------------------------------------------------------


@mcp.tool(annotations=_CONTROL)
async def lock_car(vin: str = "") -> dict[str, Any]:
    """Lock the car."""
    car = await _get_vehicle(vin or None)
    return _result(await car.lock())


@mcp.tool(annotations=_ALERT)
async def honk_and_flash(vin: str = "", action: str = "flash") -> dict[str, Any]:
    """Flash the lights or honk to help find the car. action: flash, honk, or honk_and_flash."""
    kind = _HONK_ACTIONS.get(action.strip().lower())
    if kind is None:
        raise ValueError(
            f"Invalid action {action!r}: use flash, honk, or honk_and_flash"
        )
    car = await _get_vehicle(vin or None)
    return _result(await car.honk_flash(kind))


@mcp.tool(annotations=_CONTROL)
async def close_windows(vin: str = "") -> dict[str, Any]:
    """Close all windows."""
    car = await _get_vehicle(vin or None)
    return _result(await car.close_windows())


# -- Exposure tools (opt-in) -------------------------------------------------
# Registered only when POLESTAR_ENABLE_UNLOCK is set. The confirm parameter is
# a courtesy prompt for the model, not a security control — see module
# docstring.

if UNLOCK_TOOLS_ENABLED:

    @mcp.tool(annotations=_EXPOSES_VEHICLE)
    async def unlock_car(confirm: bool = False, vin: str = "") -> dict[str, Any]:
        """Unlock the car. SAFETY: ask the user to confirm first, then call with confirm=true."""
        if not confirm:
            return {
                "error": "Unlocking exposes the vehicle. Ask the user to explicitly "
                "confirm, then retry with confirm=true."
            }
        car = await _get_vehicle(vin or None)
        return _result(await car.unlock())

    @mcp.tool(annotations=_EXPOSES_VEHICLE)
    async def unlock_trunk(confirm: bool = False, vin: str = "") -> dict[str, Any]:
        """Unlock only the trunk. SAFETY: ask the user to confirm first, then call with confirm=true."""
        if not confirm:
            return {
                "error": "Unlocking the trunk exposes the vehicle. Ask the user to "
                "explicitly confirm, then retry with confirm=true."
            }
        car = await _get_vehicle(vin or None)
        return _result(await car.unlock_trunk())

    @mcp.tool(annotations=_EXPOSES_VEHICLE)
    async def open_windows(confirm: bool = False, vin: str = "") -> dict[str, Any]:
        """Open all windows. SAFETY: ask the user to confirm first, then call with confirm=true."""
        if not confirm:
            return {
                "error": "Opening windows exposes the vehicle. Ask the user to "
                "explicitly confirm, then retry with confirm=true."
            }
        car = await _get_vehicle(vin or None)
        return _result(await car.open_windows())


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
