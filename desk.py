"""Control library for AOKE-based standing desks (Ergomate / Aum World Advanced Pro).

Uses the Ergomate BLE protocol over GATT service 0000ff00:
  - Write commands to characteristic ff02 (with response)
  - Read height notifications from characteristic ff01

Protocol packet format (9 bytes):
  [0xA6, 0xA8, 0x01, height_hi, height_lo, 0x00, 0x00, checksum, 0xFF]
  checksum = XOR of bytes[2:]
  height is in millimeters, range 650..1300 (65.0cm..130.0cm)
"""

import asyncio
import signal
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from bleak import BleakClient, BleakScanner

SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
HEIGHT_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

MIN_HEIGHT_MM = 650
MAX_HEIGHT_MM = 1300


def build_move_packet(target_mm: int) -> bytes:
    """Build a move-to-height command packet."""
    target_mm = max(MIN_HEIGHT_MM, min(MAX_HEIGHT_MM, target_mm))
    hi = (target_mm >> 8) & 0xFF
    lo = target_mm & 0xFF
    body = [0xA6, 0xA8, 0x01, hi, lo, 0x00, 0x00]
    chk = 0
    for b in body[2:]:
        chk ^= b
    body.append(chk)
    body.append(0xFF)
    return bytes(body)


async def scan(timeout: float = 5.0) -> list[str]:
    """Scan for desks advertising the ff00 service. Returns list of BLE addresses."""
    devices = await BleakScanner.discover(timeout=timeout)
    results = []
    for d in devices:
        uuids = d.metadata.get("uuids", [])
        if any(SERVICE_UUID.lower() in u.lower() for u in uuids):
            results.append(d.address)
    return results


class Desk:
    """Async interface to an AOKE standing desk."""

    def __init__(self, client: BleakClient):
        self._client = client
        self._height_mm: int | None = None
        self._height_updated = asyncio.Event()

    def _on_height(self, _sender, data: bytearray):
        try:
            s = bytes(data).decode("ascii")
            if len(s) == 4 and s.isdigit():
                self._height_mm = int(s)
                self._height_updated.set()
        except Exception:
            pass

    @asynccontextmanager
    async def _notifications(self):
        await self._client.start_notify(HEIGHT_UUID, self._on_height)
        try:
            yield
        finally:
            try:
                await self._client.stop_notify(HEIGHT_UUID)
            except Exception:
                pass

    @property
    def height_mm(self) -> int | None:
        """Last known height in millimeters, or None if not yet received."""
        return self._height_mm

    @property
    def height_cm(self) -> float | None:
        """Last known height in centimeters, or None if not yet received."""
        return self._height_mm / 10.0 if self._height_mm is not None else None

    async def move_to(self, target_mm: int, *, timeout: float = 30.0, tolerance_mm: int = 10) -> bool:
        """Move the desk to target height (mm). Returns True if target reached."""
        cmd = build_move_packet(target_mm)
        await self._client.write_gatt_char(WRITE_UUID, cmd, response=True)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._height_updated.clear()
            try:
                await asyncio.wait_for(self._height_updated.wait(), timeout=deadline - time.monotonic())
            except (asyncio.TimeoutError, ValueError):
                break
            if self._height_mm is not None and abs(self._height_mm - target_mm) <= tolerance_mm:
                return True
        return False

    async def move_to_cm(self, target_cm: float, **kwargs) -> bool:
        """Move the desk to target height (cm)."""
        return await self.move_to(int(target_cm * 10), **kwargs)


@asynccontextmanager
async def connect(address: str, *, timeout: float = 25.0):
    """Connect to a desk and yield a ready-to-use Desk instance.

    Usage:
        async with connect("D4E15C38-...") as desk:
            print(desk.height_cm)
            await desk.move_to_cm(73.0)
    """
    async with BleakClient(address, timeout=timeout) as client:
        desk = Desk(client)
        async with desk._notifications():
            await asyncio.sleep(0.5)
            yield desk


# --- Config ---

CONFIG_DIR = Path.home() / ".config" / "desk-control"
CONFIG_FILE = CONFIG_DIR / "config.toml"

FALLBACK_ADDRESS = ""
FALLBACK_SIT_CM = 73.0
FALLBACK_STAND_CM = 105.0


def _load_config() -> dict:
    """Load config from ~/.config/desk-control/config.toml."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        import tomllib
        return tomllib.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def _save_config(cfg: dict):
    """Save config to ~/.config/desk-control/config.toml."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    for k, v in cfg.items():
        if isinstance(v, float):
            lines.append(f"{k} = {v}")
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"')
    CONFIG_FILE.write_text("\n".join(lines) + "\n")


def _get_config():
    """Return (address, sit_cm, stand_cm) from config file."""
    cfg = _load_config()
    return (
        cfg.get("address", FALLBACK_ADDRESS),
        cfg.get("sit_cm", FALLBACK_SIT_CM),
        cfg.get("stand_cm", FALLBACK_STAND_CM),
    )


# --- CLI ---


def _notify(title: str, message: str, *, countdown: int = 0) -> bool:
    """macOS notification. With countdown>0, shows a dialog and returns True if skipped."""
    if countdown:
        script = (
            f'display dialog "{message}" with title "{title}" '
            f'buttons {{"Skip", "OK"}} default button "OK" '
            f'giving up after {countdown}'
        )
        try:
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=countdown + 5,
            )
            return "Skip" in r.stdout
        except Exception:
            return False
    subprocess.run(
        ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
        capture_output=True, timeout=5,
    )
    return False


async def _auto(address: str, sit: float, stand: float, interval: int, countdown: int):
    stop = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    async with connect(address) as desk:
        h = desk.height_cm
        print(f"Connected. Height: {h} cm")

        next_is_stand = True
        if h is not None:
            if abs(h - stand) < 2.0:
                next_is_stand = False
            elif abs(h - sit) >= 2.0:
                next_is_stand = abs(h - sit) < abs(h - stand)

        pos = "standing" if not next_is_stand else "sitting"
        print(f"Currently {pos}. Alternating every {interval // 60} min. Ctrl+C to stop.")
        _notify("Desk", f"Started. Currently {pos}.")

        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass

            target = stand if next_is_stand else sit
            label = "Stand up" if next_is_stand else "Sit down"

            skipped = await asyncio.to_thread(
                _notify, "Desk",
                f"{label}! Moving to {target:.0f} cm in {countdown}s.",
                countdown=countdown,
            )

            if stop.is_set():
                break
            if skipped:
                print(f"  Skipped: {label}")
                continue

            print(f"{label} -> {target} cm")
            ok = await desk.move_to_cm(target)
            print(f"  {'Reached' if ok else 'Timeout'}: {desk.height_cm} cm")
            _notify("Desk", f"{'Reached' if ok else 'Moved to'} {desk.height_cm} cm.")
            next_is_stand = not next_is_stand

        _notify("Desk", "Stopped.")
        print("Stopped.")


async def _setup():
    """Interactive setup: scan for desk, save config."""
    print("Scanning for desks...")
    addresses = await scan()
    if not addresses:
        print("No desks found. Make sure your desk is powered on and in range.")
        return

    if len(addresses) == 1:
        address = addresses[0]
        print(f"Found desk: {address}")
    else:
        print("Found multiple desks:")
        for i, a in enumerate(addresses, 1):
            print(f"  {i}. {a}")
        choice = input("Select desk number: ").strip()
        try:
            address = addresses[int(choice) - 1]
        except (ValueError, IndexError):
            print("Invalid selection.")
            return

    _, default_sit, default_stand = _get_config()
    sit_input = input(f"Sit height in cm [{default_sit}]: ").strip()
    sit_cm = float(sit_input) if sit_input else default_sit
    stand_input = input(f"Stand height in cm [{default_stand}]: ").strip()
    stand_cm = float(stand_input) if stand_input else default_stand

    _save_config({"address": address, "sit_cm": sit_cm, "stand_cm": stand_cm})
    print(f"Config saved to {CONFIG_FILE}")
    print(f"  address  = {address}")
    print(f"  sit_cm   = {sit_cm}")
    print(f"  stand_cm = {stand_cm}")


async def _cli():
    import argparse

    address, sit_cm, stand_cm = _get_config()

    parser = argparse.ArgumentParser(description="Control an AOKE standing desk via BLE")
    parser.add_argument("--address", "-a", default=address, help="BLE address")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="Interactive setup: scan and save config")
    sub.add_parser("sit", help=f"Move to sit position ({sit_cm} cm)")
    sub.add_parser("stand", help=f"Move to stand position ({stand_cm} cm)")
    sub.add_parser("scan", help="Scan for desks")
    sub.add_parser("height", help="Read current height")

    mv = sub.add_parser("move", help="Move to a specific height in cm")
    mv.add_argument("height", type=float, help="Target height in cm (65.0-130.0)")

    auto = sub.add_parser("auto", help="Sit/stand cycle with notifications")
    auto.add_argument("--interval", type=int, default=30, help="Interval in minutes (default: 30)")
    auto.add_argument("--countdown", type=int, default=30, help="Notification countdown in seconds (default: 30)")

    args = parser.parse_args()

    if args.command == "setup":
        await _setup()
        return

    if args.command == "scan":
        print("Scanning...")
        for a in await scan():
            print(f"  {a}")
        return

    if not args.address:
        print("No desk address configured. Run 'desk setup' first, or pass --address.")
        return

    if args.command == "auto":
        await _auto(args.address, sit_cm, stand_cm, args.interval * 60, args.countdown)
        return

    if args.command in ("sit", "stand", "move", "height"):
        async with connect(args.address) as desk:
            if args.command == "height":
                print(f"{desk.height_cm} cm" if desk.height_cm else "No data")
            else:
                target = {"sit": sit_cm, "stand": stand_cm, "move": getattr(args, "height", 0)}[args.command]
                ok = await desk.move_to_cm(target)
                print(f"{'Reached' if ok else 'Timeout'}: {desk.height_cm} cm")
        return

    parser.print_help()


def cli():
    asyncio.run(_cli())


if __name__ == "__main__":
    cli()
