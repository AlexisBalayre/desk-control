# desk-control

BLE control CLI for AOKE-based standing desks (Ergomate / Aum World Advanced Pro).

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/alexisbalayre/desk-control
```

Or clone and install locally:

```sh
git clone https://github.com/alexisbalayre/desk-control
cd desk-control
uv tool install .
```

## Setup

The repo ships with a default `config.toml` containing a pre-configured desk address and sit/stand heights. If you share the same desk, it works out of the box.

To configure your own desk, run the interactive setup:

```sh
desk setup
```

This scans for nearby desks over BLE, lets you select one, and configure your sit/stand heights. Your config is saved to `~/.config/desk-control/config.toml` and takes priority over the bundled defaults.

## Usage

```sh
desk sit        # Move to sit position
desk stand      # Move to stand position
desk height     # Read current height
desk move 80.0  # Move to a specific height (cm)
desk scan       # Scan for nearby desks
desk auto       # Automatic sit/stand cycle with notifications
```

### Auto mode

Alternates between sitting and standing on a timer, with a macOS notification before each move:

```sh
desk auto                    # 30 min interval, 30s countdown
desk auto --interval 45      # 45 min interval
desk auto --countdown 15     # 15s countdown before moving
```

You can skip a move from the notification dialog. Press `Ctrl+C` to stop.

### Manual address

If you don't want to use `desk setup`, pass the address directly:

```sh
desk -a "D4E15C38-..." sit
```

## Protocol

Uses the AOKE BLE protocol over GATT service `0000ff00`:

- **ff02** (write): 9-byte move command `[0xA6, 0xA8, 0x01, height_hi, height_lo, 0x00, 0x00, checksum, 0xFF]`
- **ff01** (notify): 4-digit ASCII height in millimeters
- Height range: 650-1300 mm (65.0-130.0 cm)
- Checksum: XOR of bytes 2-6
