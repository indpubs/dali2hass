import sys
import argparse
import pathlib
import logging
from voluptuous import MultipleInvalid

from .config import Config
from .hass import HomeAssistant
from .dali import Bridge


def main():
    parser = argparse.ArgumentParser(
        description="DALI to Home Assistant bridge")
    parser.add_argument(
        '--configfile', '-c', type=pathlib.Path,
        default=pathlib.Path("config.toml"),
        help="Path to configuration file"),
    parser.add_argument(
        '--dry-run', '-n', action="store_true",
        help="Don't send any commands that would alter the state of "
        "devices on the bus")
    parser.add_argument(
        '--max-address', '-m', type=int, default=63,
        help="Maximum short address to probe; can be used during testing "
        "to reduce startup time")
    parser.add_argument(
        '--debug', action="store_true",
        help="Output debug information")

    args = parser.parse_args()

    try:
        with open(args.configfile, "rb") as f:
            config = Config(f)
    except FileNotFoundError:
        print(f"Could not open config file '{args.configfile}'")
        sys.exit(1)
    except MultipleInvalid as e:
        print(str(e))
        sys.exit(1)

    if args.max_address > 63 or args.max_address < 0:
        print("max-address must be in the range 0..63")
        sys.exit(1)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    hass = HomeAssistant(config.homeassistant)
    Bridge(config.dali, hass, dry_run=args.dry_run,
           max_address=args.max_address)

    sys.exit(hass.run())
