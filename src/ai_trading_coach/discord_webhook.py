from __future__ import annotations

import io
import json
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class DiscordWebhook:
    url: str
    username: str = "ViperCoach"

    def send_text(self, content: str) -> None:
        if not self.url:
            raise RuntimeError("DISCORD_WEBHOOK_URL is not set.")
        # Discord content limit is 2000 chars; caller should chunk or use file upload.
        resp = requests.post(
            self.url,
            json={"content": content, "username": self.username},
            timeout=30,
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"Discord webhook failed: {resp.status_code} {resp.text}")

    def send_markdown_file(self, *, filename: str, content_md: str, content_preview: str | None = None) -> None:
        if not self.url:
            raise RuntimeError("DISCORD_WEBHOOK_URL is not set.")

        file_bytes = content_md.encode("utf-8")
        files = {"files[0]": (filename, io.BytesIO(file_bytes), "text/markdown; charset=utf-8")}
        payload: dict[str, str] = {"username": self.username}
        if content_preview:
            # Keep preview short; never spam.
            payload["content"] = content_preview[:900]
        # Discord expects payload_json for multipart webhook requests.
        data = {"payload_json": json.dumps(payload, ensure_ascii=False)}

        resp = requests.post(self.url, data=data, files=files, timeout=60)
        if resp.status_code >= 300:
            raise RuntimeError(f"Discord webhook file upload failed: {resp.status_code} {resp.text}")


