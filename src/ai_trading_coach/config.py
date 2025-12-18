from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


def _load_dotenv_compat(path: Path, *, override: bool = False) -> bool:
    """
    Minimal .env loader to avoid hard dependency on python-dotenv.
    Supports simple KEY=VALUE lines, ignores comments and blank lines.
    Does NOT support export, quotes/escapes expansion, or multiline values.
    """
    if not path.exists() or not path.is_file():
        return False
    loaded_any = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        # If override is False, don't overwrite non-empty env vars.
        # But DO allow .env to populate variables that exist but are empty strings,
        # because shells sometimes `export KEY=` which should not block .env.
        if (not override) and (k in os.environ) and (os.environ.get(k, "") != ""):
            continue
        os.environ[k] = v
        loaded_any = True
    return loaded_any


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


@dataclass(frozen=True)
class AppConfig:
    # Bitget (read-only)
    bitget_api_key: str
    bitget_api_secret: str
    bitget_api_password: str
    bitget_base_url: str

    # scope
    symbols: list[str]
    max_symbols: int

    # storage
    data_dir: Path
    sqlite_path: Path

    # discord
    discord_webhook_url: str
    discord_username: str

    # llm (OpenAI-compatible)
    llm_base_url: str
    llm_api_key: str
    llm_model: str

    # scheduling
    timezone: str
    daily_at: str
    weekly_dow: str
    weekly_at: str
    monthly_at: str

    # report lookback windows
    daily_lookback_days: int
    weekly_lookback_days: int
    monthly_lookback_days: int

    # rules
    rules_path: Path

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)


def load_config(env_path: str | None = None) -> AppConfig:
    """
    Loads config from environment.
    If env_path is provided and exists, it's loaded first (dotenv).
    """
    if env_path:
        _load_dotenv_compat(Path(env_path), override=False)
    else:
        # Allow running from any working directory:
        # 1) try CWD
        # 2) try repo root relative to this file: <repo>/src/ai_trading_coach/config.py -> <repo>
        cwd = Path.cwd()
        repo_root = Path(__file__).resolve().parents[2]

        for base in (cwd, repo_root):
            _load_dotenv_compat(base / ".env.local", override=False)
            _load_dotenv_compat(base / ".env", override=False)

    symbols = _parse_csv(os.getenv("SYMBOLS"))

    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    sqlite_path = Path(os.getenv("SQLITE_PATH", str(data_dir / "ai_trading_coach.sqlite3")))

    rules_path = Path(os.getenv("RULES_PATH", "./rules.yaml"))

    # Optional runtime overrides from SQLite (for platforms without env var UI).
    # We only read from SQLite when the corresponding env var is empty.
    def _maybe_from_store(k: str) -> str | None:
        if os.getenv(k, "").strip():
            return None
        try:
            from ai_trading_coach.storage.sqlite_store import SqliteStore

            store = SqliteStore(sqlite_path)
            store.ensure_schema()
            v = store.get_kv(k)
            return v.strip() if isinstance(v, str) and v.strip() else None
        except Exception:
            return None

    def _get_str(k: str, default: str = "") -> str:
        v = os.getenv(k, "").strip()
        if not v:
            sv = _maybe_from_store(k)
            if sv:
                v = sv
        return v or default

    return AppConfig(
        bitget_api_key=_get_str("BITGET_API_KEY", ""),
        bitget_api_secret=_get_str("BITGET_API_SECRET", ""),
        bitget_api_password=_get_str("BITGET_API_PASSWORD", ""),
        bitget_base_url=_get_str("BITGET_BASE_URL", "https://api.bitget.com") or "https://api.bitget.com",
        symbols=symbols,
        max_symbols=int(os.getenv("MAX_SYMBOLS", "0")),
        data_dir=data_dir,
        sqlite_path=sqlite_path,
        discord_webhook_url=_get_str("DISCORD_WEBHOOK_URL", ""),
        discord_username=_get_str("DISCORD_USERNAME", "ViperCoach") or "ViperCoach",
        llm_base_url=_get_str("LLM_BASE_URL", "https://space.ai-builders.com/backend/v1")
        or "https://space.ai-builders.com/backend/v1",
        llm_api_key=(
            _get_str("LLM_API_KEY", "")
            or _get_str("OPENAI_API_KEY", "")
        ),
        llm_model=_get_str("LLM_MODEL", "gpt-5") or "gpt-5",
        timezone=_get_str("TIMEZONE", "America/New_York") or "America/New_York",
        daily_at=_get_str("DAILY_AT", "23:00") or "23:00",
        weekly_dow=_get_str("WEEKLY_DOW", "sat").lower() or "sat",
        weekly_at=_get_str("WEEKLY_AT", "23:00") or "23:00",
        monthly_at=_get_str("MONTHLY_AT", "23:00") or "23:00",
        daily_lookback_days=int(os.getenv("DAILY_LOOKBACK_DAYS", "1")),
        weekly_lookback_days=int(os.getenv("WEEKLY_LOOKBACK_DAYS", "7")),
        monthly_lookback_days=int(os.getenv("MONTHLY_LOOKBACK_DAYS", "30")),
        rules_path=rules_path,
    )


def require_credentials(cfg: AppConfig) -> None:
    missing: list[str] = []
    if not cfg.bitget_api_key:
        missing.append("BITGET_API_KEY")
    if not cfg.bitget_api_secret:
        missing.append("BITGET_API_SECRET")
    if not cfg.bitget_api_password:
        missing.append("BITGET_API_PASSWORD")
    if missing:
        raise RuntimeError(
            "Missing Bitget credentials (READ-ONLY required): " + ", ".join(missing)
        )


def normalize_symbols(symbols: Iterable[str]) -> list[str]:
    # ccxt uses strings like "BTC/USDT:USDT" for USDT-margined swaps on some exchanges.
    out: list[str] = []
    for s in symbols:
        s2 = s.strip()
        if not s2:
            continue
        out.append(s2)
    return out


