from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi import Header
from pydantic import BaseModel, Field

from ai_trading_coach.config import load_config
from ai_trading_coach.llm_client import LlmClient
from ai_trading_coach.scheduler.run_reviews import ReviewRunner
from ai_trading_coach.scheduler.scheduler_app import create_background_scheduler
from ai_trading_coach.storage.sqlite_store import SqliteStore


class ChatRequest(BaseModel):
    user_message: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    assistant_message: str
    model: str


class BootstrapRequest(BaseModel):
    # Secrets (optional)
    bitget_api_key: str | None = None
    bitget_api_secret: str | None = None
    bitget_api_password: str | None = None

    discord_webhook_url: str | None = None
    discord_username: str | None = None

    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None

    # Non-secret
    timezone: str | None = None
    daily_at: str | None = None
    weekly_dow: str | None = None
    weekly_at: str | None = None
    monthly_at: str | None = None
    enable_scheduler: bool | None = None


def _require_ai_builder_token(auth: str | None) -> None:
    expected = os.getenv("AI_BUILDER_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=403, detail="AI_BUILDER_TOKEN not set in container.")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization: Bearer <token>.")
    got = auth.removeprefix("Bearer ").strip()
    if got != expected:
        raise HTTPException(status_code=403, detail="Invalid token.")


def create_app() -> FastAPI:
    app = FastAPI(title="AI Trading Coach API", version="0.1.0")

    # Many hosting platforms (incl. Koyeb) default health checks to "/".
    # Keep "/" always-OK and zero-config so the service can become healthy
    # even if secrets are not configured yet.
    @app.get("/")
    def root() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/admin/status")
    def admin_status(authorization: str | None = Header(default=None)) -> dict:
        _require_ai_builder_token(authorization)
        cfg = load_config()
        sched = getattr(app.state, "scheduler", None)
        daily_next = None
        if sched is not None:
            try:
                job = sched.get_job("daily_review")
                daily_next = job.next_run_time.isoformat() if job and job.next_run_time else None
            except Exception:
                daily_next = None
        # only report presence, never echo secrets
        return {
            "bitget": {
                "api_key_set": bool(cfg.bitget_api_key),
                "api_secret_set": bool(cfg.bitget_api_secret),
                "api_password_set": bool(cfg.bitget_api_password),
            },
            "discord": {
                "webhook_set": bool(cfg.discord_webhook_url),
                "username_set": bool(cfg.discord_username),
            },
            "llm": {
                "base_url_set": bool(cfg.llm_base_url),
                "api_key_set": bool(cfg.llm_api_key),
                "model": cfg.llm_model,
            },
            "schedule": {
                "timezone": cfg.timezone,
                "daily_at": cfg.daily_at,
                "weekly_dow": cfg.weekly_dow,
                "weekly_at": cfg.weekly_at,
                "monthly_at": cfg.monthly_at,
                "enable_scheduler_effective": bool(cfg.enable_scheduler),
                "enable_scheduler_env": os.getenv("ENABLE_SCHEDULER", ""),
                "scheduler_running": bool(sched is not None),
                "daily_next_run_time": daily_next,
            },
        }

    @app.post("/admin/bootstrap")
    def admin_bootstrap(req: BootstrapRequest, authorization: str | None = Header(default=None)) -> dict:
        """
        One-time bootstrap for platforms without env var UI.
        Stores values into SQLite app_kv and also updates process env for immediate effect.
        """
        _require_ai_builder_token(authorization)
        cfg = load_config()
        store = SqliteStore(cfg.sqlite_path)
        store.ensure_schema()

        def _set(key: str, value: str, *, secret: bool) -> None:
            store.set_kv(key=key, value=value, is_secret=secret)
            os.environ[key] = value

        if req.bitget_api_key:
            _set("BITGET_API_KEY", req.bitget_api_key.strip(), secret=True)
        if req.bitget_api_secret:
            _set("BITGET_API_SECRET", req.bitget_api_secret.strip(), secret=True)
        if req.bitget_api_password:
            _set("BITGET_API_PASSWORD", req.bitget_api_password.strip(), secret=True)

        if req.discord_webhook_url is not None:
            _set("DISCORD_WEBHOOK_URL", req.discord_webhook_url.strip(), secret=True)
        if req.discord_username is not None:
            _set("DISCORD_USERNAME", req.discord_username.strip(), secret=False)

        if req.llm_base_url is not None:
            _set("LLM_BASE_URL", req.llm_base_url.strip(), secret=False)
        if req.llm_api_key is not None:
            _set("LLM_API_KEY", req.llm_api_key.strip(), secret=True)
        if req.llm_model is not None:
            _set("LLM_MODEL", req.llm_model.strip(), secret=False)

        if req.timezone is not None:
            _set("TIMEZONE", req.timezone.strip(), secret=False)
        if req.daily_at is not None:
            _set("DAILY_AT", req.daily_at.strip(), secret=False)
        if req.weekly_dow is not None:
            _set("WEEKLY_DOW", req.weekly_dow.strip(), secret=False)
        if req.weekly_at is not None:
            _set("WEEKLY_AT", req.weekly_at.strip(), secret=False)
        if req.monthly_at is not None:
            _set("MONTHLY_AT", req.monthly_at.strip(), secret=False)
        if req.enable_scheduler is not None:
            _set("ENABLE_SCHEDULER", "1" if req.enable_scheduler else "0", secret=False)

        # Apply scheduler toggle immediately without requiring a redeploy/restart.
        # - If scheduler exists, restart it to pick up latest config.
        # - If disabled, shut it down.
        try:
            existing = getattr(app.state, "scheduler", None)
            if existing is not None:
                try:
                    existing.shutdown(wait=False)
                except Exception:
                    pass
                app.state.scheduler = None

            cfg2 = load_config()
            if cfg2.enable_scheduler:
                runner2 = ReviewRunner(cfg2)
                sched2 = create_background_scheduler(runner=runner2)
                sched2.start()
                app.state.scheduler = sched2
        except Exception:
            # Don't fail bootstrap for scheduler lifecycle issues; admin can retry.
            pass

        return {"ok": True}

    @app.on_event("startup")
    def _startup() -> None:
        # Optional: run scheduler in the same process (single service / single port).
        # IMPORTANT: keep uvicorn workers=1; otherwise jobs may run multiple times.
        cfg = load_config()
        if not cfg.enable_scheduler:
            return
        runner = ReviewRunner(cfg)
        sched = create_background_scheduler(runner=runner)
        sched.start()
        app.state.scheduler = sched

    @app.on_event("shutdown")
    def _shutdown() -> None:
        sched = getattr(app.state, "scheduler", None)
        if sched is not None:
            try:
                sched.shutdown(wait=False)
            except Exception:
                pass

    @app.post("/at/chat", response_model=ChatResponse)
    def at_chat(req: ChatRequest) -> ChatResponse:
        cfg = load_config()
        try:
            llm = LlmClient(cfg)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

        system = (
            "You are a trading retrospective officer (viper coach tone). "
            "Rules: analyze ONLY past events; no predictions; no trade instructions; "
            "no order placement/modification/cancellation logic; be specific and evidence-driven."
        )

        # Endpoint contract: request body has only `user_message`.
        # We pass it through as user content; any higher-level app will add evidence/context upstream.
        result = llm.chat(system=system, user=req.user_message, temperature=0.2)
        return ChatResponse(assistant_message=result.content, model=cfg.llm_model)

    return app


app = create_app()


