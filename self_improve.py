"""Use the adjacent setup-scrum.json for launcher settings."""
from pathlib import Path

from improver.cli import main

if __name__ == '__main__':
    raise SystemExit(main(default_settings=Path(__file__).resolve().with_name('setup-scrum.json')))
