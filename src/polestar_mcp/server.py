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

Command safety
--------------
Command tools serialize per vehicle (one in-flight command per car), check
the car is reachable first (waking it once if asleep), read the relevant
state before sending, and skip commands that are already satisfied (e.g.
"start charging" while charging). State reads are best-effort: if a read
fails the command still goes through, with a warning in the result — the
Polestar backend remains the final validator.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from polestar_api import PolestarApi
from polestar_api.auth import FileTokenStore
from polestar_api.models.battery import ChargerConnectionStatus, ChargingStatus
from polestar_api.models.charging import ChargeTargetLevelSettingType
from polestar_api.models.climatization import HeatingIntensity
from polestar_api.models.common import ChronosStatus
from polestar_api.models.exterior import ExteriorStatus, OpenStatus
from polestar_api.models.honkflash import HonkFlashAction
from polestar_api.models.availability import UnavailableReason
from polestar_api.vehicle import Vehicle

mcp = FastMCP("polestar")

_api: PolestarApi | None = None
_api_lock = asyncio.Lock()
_vehicles: dict[str, Vehicle] = {}
_vehicle_locks: dict[str, asyncio.Lock] = {}

DEFAULT_TOKEN_FILE = "~/.polestar-mcp/tokens.json"

# Seconds to wait after a wake-up before re-checking availability.
WAKE_SETTLE_SECONDS = 8.0

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

# Unavailability reasons where a remote wake-up can plausibly help.
_WAKEABLE_REASONS = {
    UnavailableReason.UNSPECIFIED,
    UnavailableReason.POWER_SAVING_MODE,
}

_CHARGING_ACTIVE = {ChargingStatus.CHARGING, ChargingStatus.SMART_CHARGING}


# -- Plumbing ----------------------------------------------------------------


async def _get_api() -> PolestarApi:
    global _api
    async with _api_lock:
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


def _vehicle_lock(vin: str) -> asyncio.Lock:
    """One lock per VIN so a vehicle never has two commands in flight."""
    return _vehicle_locks.setdefault(vin, asyncio.Lock())


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


# -- Command pre-flight helpers ----------------------------------------------

T = TypeVar("T")


async def _try_read(
    awaitable: Awaitable[T], what: str, warnings: list[str]
) -> T | None:
    """Best-effort state read: a failed pre-check must not block the command."""
    try:
        return await awaitable
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not read {what} before sending: {exc}")
        return None


async def _check_reachable(car: Vehicle, warnings: list[str]) -> dict[str, Any]:
    """Availability gate: wake a sleeping car once, then report reachability."""
    avail = await _try_read(car.get_availability(), "availability", warnings)
    if avail is None or avail.is_available:
        return {"available": True}

    reason = avail.unavailable_reason
    if reason in _WAKEABLE_REASONS:
        woke = await _try_read(car.wakeup(), "wake-up response", warnings)
        if woke is not None:
            await asyncio.sleep(WAKE_SETTLE_SECONDS)
            avail = await _try_read(car.get_availability(), "availability", warnings)
            if avail is None or avail.is_available:
                return {
                    "available": True,
                    "note": "Car was asleep and was woken before sending the command.",
                }
    return {
        "available": False,
        "reason": _plain(reason),
        "note": "Car is unreachable; the command was still sent and the backend "
        "will deliver it when the car reconnects.",
    }


def _command_result(
    *,
    sent: bool,
    response: Any = None,
    reason: str | None = None,
    before: Any = None,
    warnings: list[str] | None = None,
    availability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"command": "sent" if sent else "skipped"}
    if reason:
        out["reason"] = reason
    if availability and (availability.get("available") is not True or "note" in availability):
        out["availability"] = availability
    if before is not None:
        out["before"] = _plain(before)
    if warnings:
        out["warnings"] = warnings
    if sent:
        out["response"] = _plain(response)
    return out


def _chronos_name(status: int) -> str:
    try:
        return ChronosStatus(status).name
    except ValueError:
        return str(status)


def _battery_summary(battery: Any) -> dict[str, Any] | None:
    if battery is None:
        return None
    return {
        "charge_level_percent": battery.charge_level,
        "charging_status": battery.charging_status.name,
        "charger_connection": battery.charger_connection_status.name,
        "range_km": battery.range_km,
    }


def _open_parts(exterior: ExteriorStatus) -> list[str]:
    """Doors/hood/tailgate that are open or ajar."""
    parts: list[str] = []
    if exterior.any_door_open:
        parts.append("a door")
    for name, holder in (("the hood", exterior.hood), ("the tailgate", exterior.tailgate)):
        status = holder.status if holder else None
        if status and status.open_status in {OpenStatus.OPEN, OpenStatus.AJAR}:
            parts.append(name)
    return parts


def _windows_summary(exterior: ExteriorStatus | None) -> dict[str, str] | None:
    if exterior is None or exterior.windows is None:
        return None
    windows = exterior.windows
    return {
        name: (getattr(windows, name).open_status.name if getattr(windows, name) else "UNSPECIFIED")
        for name in ("front_left", "front_right", "rear_left", "rear_right")
    }


def _all_windows_closed(exterior: ExteriorStatus | None) -> bool:
    summary = _windows_summary(exterior)
    if not summary:
        return False
    return all(state == "CLOSED" for state in summary.values())


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
    async with _vehicle_lock(car.vin):
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
    Skipped if climatization is already running and no settings were given.
    """
    heat_args = {
        "front_left_seat": _heat(front_left_seat, "front_left_seat"),
        "front_right_seat": _heat(front_right_seat, "front_right_seat"),
        "rear_left_seat": _heat(rear_left_seat, "rear_left_seat"),
        "rear_right_seat": _heat(rear_right_seat, "rear_right_seat"),
        "steering_wheel": _heat(steering_wheel, "steering_wheel"),
    }
    has_settings = temperature_celsius != 0.0 or any(
        level != HeatingIntensity.UNSPECIFIED for level in heat_args.values()
    )
    car = await _get_vehicle(vin or None)
    async with _vehicle_lock(car.vin):
        warnings: list[str] = []
        availability = await _check_reachable(car, warnings)
        climate = await _try_read(car.get_climate(), "climate status", warnings)
        battery = await _try_read(car.get_battery(), "battery", warnings)

        if battery is not None and 0 < battery.charge_level < 20:
            warnings.append(
                f"Battery is at {battery.charge_level:.0f}% — climatization "
                "off-charger will reduce range further."
            )
        already_active = climate is not None and climate.is_active
        if already_active and not has_settings:
            return _command_result(
                sent=False,
                reason="Climatization is already running.",
                before=climate,
                warnings=warnings,
                availability=availability,
            )
        if already_active:
            warnings.append("Climatization was already running; settings re-applied.")

        response = await car.start_climate(temperature=temperature_celsius, **heat_args)
        return _command_result(
            sent=True,
            response=response,
            before=climate,
            warnings=warnings,
            availability=availability,
        )


@mcp.tool(annotations=_CONTROL)
async def stop_climate(vin: str = "") -> dict[str, Any]:
    """Stop climatization. Skipped if it is not running."""
    car = await _get_vehicle(vin or None)
    async with _vehicle_lock(car.vin):
        warnings: list[str] = []
        availability = await _check_reachable(car, warnings)
        climate = await _try_read(car.get_climate(), "climate status", warnings)
        if climate is not None and not climate.is_active:
            return _command_result(
                sent=False,
                reason="Climatization is not running.",
                before=climate,
                warnings=warnings,
                availability=availability,
            )
        response = await car.stop_climate()
        return _command_result(
            sent=True,
            response=response,
            before=climate,
            warnings=warnings,
            availability=availability,
        )


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
    """Set the target charge level (0-100%). setting_type: daily, long_trip, or custom.

    Skipped if the target is already at that level; warns if the target is
    below the current charge (charging would stop).
    """
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    kind = _SOC_SETTING_TYPES.get(setting_type.strip().lower())
    if kind is None:
        raise ValueError(
            f"Invalid setting_type {setting_type!r}: use daily, long_trip, or custom"
        )
    car = await _get_vehicle(vin or None)
    async with _vehicle_lock(car.vin):
        warnings: list[str] = []
        availability = await _check_reachable(car, warnings)
        current = await _try_read(car.get_target_soc(), "current charge target", warnings)
        battery = await _try_read(car.get_battery(), "battery", warnings)

        if current is not None and current.target_level == percent:
            return _command_result(
                sent=False,
                reason=f"Charge target is already {percent}%.",
                before=current,
                warnings=warnings,
                availability=availability,
            )
        if battery is not None and battery.charge_level > percent:
            warnings.append(
                f"Target {percent}% is below the current charge "
                f"({battery.charge_level:.0f}%) — charging will stop or not start."
            )
        response = await car.set_target_soc(percent, kind)
        return _command_result(
            sent=True,
            response=response,
            before=current,
            warnings=warnings,
            availability=availability,
        )


@mcp.tool(annotations=_CONTROL)
async def set_amp_limit(amps: int, vin: str = "") -> dict[str, Any]:
    """Set the AC charging amperage limit. Skipped if already at that limit."""
    car = await _get_vehicle(vin or None)
    async with _vehicle_lock(car.vin):
        warnings: list[str] = []
        availability = await _check_reachable(car, warnings)
        current = await _try_read(car.get_amp_limit(), "current amp limit", warnings)
        if current is not None and current.amperage_limit == amps:
            return _command_result(
                sent=False,
                reason=f"Amp limit is already {amps} A.",
                before=current,
                warnings=warnings,
                availability=availability,
            )
        response = await car.set_amp_limit(amps)
        return _command_result(
            sent=True,
            response=response,
            before=current,
            warnings=warnings,
            availability=availability,
        )


@mcp.tool(annotations=_CONTROL)
async def start_charging(vin: str = "") -> dict[str, Any]:
    """Start charging immediately (overrides timers).

    Skipped if already charging or if no charger is connected.
    """
    car = await _get_vehicle(vin or None)
    async with _vehicle_lock(car.vin):
        warnings: list[str] = []
        availability = await _check_reachable(car, warnings)
        battery = await _try_read(car.get_battery(), "battery", warnings)
        before = _battery_summary(battery)

        if battery is not None:
            if battery.charging_status in _CHARGING_ACTIVE:
                return _command_result(
                    sent=False,
                    reason="Already charging.",
                    before=before,
                    warnings=warnings,
                    availability=availability,
                )
            if battery.charger_connection_status == ChargerConnectionStatus.DISCONNECTED:
                return _command_result(
                    sent=False,
                    reason="No charger connected — plug the car in first.",
                    before=before,
                    warnings=warnings,
                    availability=availability,
                )
            if battery.charger_connection_status == ChargerConnectionStatus.FAULT:
                warnings.append("Charger connection reports a FAULT.")

        status = await car.start_charging()
        return _command_result(
            sent=True,
            response={"status": _chronos_name(status)},
            before=before,
            warnings=warnings,
            availability=availability,
        )


@mcp.tool(annotations=_CONTROL)
async def stop_charging(vin: str = "") -> dict[str, Any]:
    """Stop or pause an active charging session. Skipped if not charging."""
    car = await _get_vehicle(vin or None)
    async with _vehicle_lock(car.vin):
        warnings: list[str] = []
        availability = await _check_reachable(car, warnings)
        battery = await _try_read(car.get_battery(), "battery", warnings)
        before = _battery_summary(battery)

        if battery is not None and battery.charging_status not in _CHARGING_ACTIVE:
            return _command_result(
                sent=False,
                reason=f"Not charging (status: {battery.charging_status.name}).",
                before=before,
                warnings=warnings,
                availability=availability,
            )
        status = await car.stop_charging()
        return _command_result(
            sent=True,
            response={"status": _chronos_name(status)},
            before=before,
            warnings=warnings,
            availability=availability,
        )


# -- Locks & security --------------------------------------------------------


@mcp.tool(annotations=_CONTROL)
async def lock_car(vin: str = "") -> dict[str, Any]:
    """Lock the car. Skipped if already locked; warns if a door/hood/tailgate is open."""
    car = await _get_vehicle(vin or None)
    async with _vehicle_lock(car.vin):
        warnings: list[str] = []
        availability = await _check_reachable(car, warnings)
        exterior = await _try_read(car.get_exterior(), "exterior status", warnings)

        if exterior is not None:
            if exterior.is_locked:
                return _command_result(
                    sent=False,
                    reason="The car is already locked.",
                    before={"locked": True},
                    warnings=warnings,
                    availability=availability,
                )
            open_parts = _open_parts(exterior)
            if open_parts:
                warnings.append(
                    f"{' and '.join(open_parts).capitalize()} is open — the lock "
                    "command was sent, but open parts cannot be secured."
                )
        response = await car.lock()
        return _command_result(
            sent=True,
            response=response,
            before={"locked": exterior.is_locked} if exterior is not None else None,
            warnings=warnings,
            availability=availability,
        )


@mcp.tool(annotations=_ALERT)
async def honk_and_flash(vin: str = "", action: str = "flash") -> dict[str, Any]:
    """Flash the lights or honk to help find the car. action: flash, honk, or honk_and_flash."""
    kind = _HONK_ACTIONS.get(action.strip().lower())
    if kind is None:
        raise ValueError(
            f"Invalid action {action!r}: use flash, honk, or honk_and_flash"
        )
    car = await _get_vehicle(vin or None)
    async with _vehicle_lock(car.vin):
        warnings: list[str] = []
        availability = await _check_reachable(car, warnings)
        response = await car.honk_flash(kind)
        return _command_result(
            sent=True, response=response, warnings=warnings, availability=availability
        )


@mcp.tool(annotations=_CONTROL)
async def close_windows(vin: str = "") -> dict[str, Any]:
    """Close all windows. Skipped if all windows are already closed."""
    car = await _get_vehicle(vin or None)
    async with _vehicle_lock(car.vin):
        warnings: list[str] = []
        availability = await _check_reachable(car, warnings)
        exterior = await _try_read(car.get_exterior(), "exterior status", warnings)
        windows = _windows_summary(exterior)

        if _all_windows_closed(exterior):
            return _command_result(
                sent=False,
                reason="All windows are already closed.",
                before={"windows": windows},
                warnings=warnings,
                availability=availability,
            )
        response = await car.close_windows()
        return _command_result(
            sent=True,
            response=response,
            before={"windows": windows} if windows else None,
            warnings=warnings,
            availability=availability,
        )


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
        async with _vehicle_lock(car.vin):
            warnings: list[str] = []
            availability = await _check_reachable(car, warnings)
            exterior = await _try_read(car.get_exterior(), "exterior status", warnings)
            if exterior is not None and exterior.is_locked is False:
                return _command_result(
                    sent=False,
                    reason="The car is already unlocked.",
                    before={"locked": False},
                    warnings=warnings,
                    availability=availability,
                )
            response = await car.unlock()
            return _command_result(
                sent=True,
                response=response,
                before={"locked": exterior.is_locked} if exterior is not None else None,
                warnings=warnings,
                availability=availability,
            )

    @mcp.tool(annotations=_EXPOSES_VEHICLE)
    async def unlock_trunk(confirm: bool = False, vin: str = "") -> dict[str, Any]:
        """Unlock only the trunk. SAFETY: ask the user to confirm first, then call with confirm=true."""
        if not confirm:
            return {
                "error": "Unlocking the trunk exposes the vehicle. Ask the user to "
                "explicitly confirm, then retry with confirm=true."
            }
        car = await _get_vehicle(vin or None)
        async with _vehicle_lock(car.vin):
            warnings: list[str] = []
            availability = await _check_reachable(car, warnings)
            response = await car.unlock_trunk()
            return _command_result(
                sent=True, response=response, warnings=warnings, availability=availability
            )

    @mcp.tool(annotations=_EXPOSES_VEHICLE)
    async def open_windows(confirm: bool = False, vin: str = "") -> dict[str, Any]:
        """Open all windows. SAFETY: ask the user to confirm first, then call with confirm=true."""
        if not confirm:
            return {
                "error": "Opening windows exposes the vehicle. Ask the user to "
                "explicitly confirm, then retry with confirm=true."
            }
        car = await _get_vehicle(vin or None)
        async with _vehicle_lock(car.vin):
            warnings: list[str] = []
            availability = await _check_reachable(car, warnings)
            exterior = await _try_read(car.get_exterior(), "exterior status", warnings)
            windows = _windows_summary(exterior)
            response = await car.open_windows()
            return _command_result(
                sent=True,
                response=response,
                before={"windows": windows} if windows else None,
                warnings=warnings,
                availability=availability,
            )


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
