"""Load repo-root .env into os.environ if python-dotenv is installed."""

from pathlib import Path


def load_env(start: Path | None = None) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    root = start or Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    if env_path.exists():
        load_dotenv(env_path)
