from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import requests
from requests import Response

from ai_trading_coach.config import AppConfig, require_credentials
from ai_trading_coach.domain.trade_lifecycle import ExecutionFill, PositionSide


def _ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _to_decimal(x: Any) -> Decimal | None:
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except Exception:
        return None


def _norm_hold_side(v: Any) -> PositionSide:
    if not isinstance(v, str):
        return PositionSide.unknown
    s = v.lower()
    if "long" in s:
        return PositionSide.long
    if "short" in s:
        return PositionSide.short
    return PositionSide.unknown


def _norm_trade_side(v: Any) -> str:
    if not isinstance(v, str):
        return "unknown"
    s = v.lower()
    if "open" in s:
        return "open"
    if "close" in s:
        return "close"
    return "unknown"


@dataclass(frozen=True)
class BitgetRestClient:
    """
    Minimal Bitget private REST client for fills (READ-ONLY).

    Signature (common Bitget pattern):
      sign = base64(hmac_sha256(secret, prehash))
      prehash = timestamp + method + requestPathWithQuery + body
    For GET: body is empty string.
    """

    base_url: str
    api_key: str
    api_secret: str
    api_passphrase: str
    locale: str = "zh-CN"

    @classmethod
    def from_config(cls, cfg: AppConfig) -> "BitgetRestClient":
        require_credentials(cfg)
        base_url = (getattr(cfg, "bitget_base_url", "") or "").strip() or "https://api.bitget.com"
        return cls(
            base_url=base_url.rstrip("/"),
            api_key=cfg.bitget_api_key,
            api_secret=cfg.bitget_api_secret,
            api_passphrase=cfg.bitget_api_password,
        )

    def _sign(self, timestamp: str, method: str, path_with_query: str, body: str) -> str:
        prehash = f"{timestamp}{method.upper()}{path_with_query}{body}"
        mac = hmac.new(self.api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _get(self, path: str, params: dict[str, Any], *, timeout_s: int = 60, retries: int = 3) -> dict[str, Any]:
        query = urlencode({k: v for k, v in params.items() if v is not None})
        path_with_query = f"{path}?{query}" if query else path
        url = f"{self.base_url}{path_with_query}"
        last_err: Exception | None = None
        for attempt in range(retries):
            ts = str(int(time.time() * 1000))
            sign = self._sign(ts, "GET", path_with_query, "")
            headers = {
                "ACCESS-KEY": self.api_key,
                "ACCESS-SIGN": sign,
                "ACCESS-PASSPHRASE": self.api_passphrase,
                "ACCESS-TIMESTAMP": ts,
                "Content-Type": "application/json",
                "locale": self.locale,
            }
            try:
                resp: Response = requests.get(url, headers=headers, timeout=timeout_s)
                if resp.status_code >= 500:
                    raise RuntimeError(f"Bitget REST server error {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 300:
                    raise RuntimeError(f"Bitget REST error {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                if str(data.get("code")) not in ("00000", "0"):
                    raise RuntimeError(f"Bitget REST failed: {data}")
                return data
            except Exception as e:
                last_err = e
                # small backoff
                time.sleep(0.4 * (attempt + 1))
                continue
        raise RuntimeError(f"Bitget REST request failed after retries: {last_err}")

    def fetch_mix_order_fills(
        self,
        *,
        product_type: str = "USDT-FUTURES",
        start: datetime,
        end: datetime,
        symbol: str | None = None,
        limit: int = 100,
        id_less_than: str | None = None,
    ) -> tuple[list[ExecutionFill], str | None]:
        """
        Fetch contract fills in [start, end].
        Bitget may limit time span; caller should window-slice if needed.
        """
        path = "/api/v2/mix/order/fills"
        params = {
            "productType": product_type,
            "startTime": _ms(start),
            "endTime": _ms(end),
            "limit": limit,
            "symbol": symbol,  # optional
            "idLessThan": id_less_than,
        }
        payload = self._get(path, params)
        data = payload.get("data") or {}
        rows = []
        end_id = None
        if isinstance(data, dict):
            rows = data.get("fillList") or []
            end_id = data.get("endId")
        elif isinstance(data, list):
            rows = data
        out: list[ExecutionFill] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            ts = r.get("cTime") or r.get("uTime")
            dt = datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc) if ts else datetime.now(timezone.utc)
            # Symbol normalization: API returns BTCUSDT; map into ccxt-like "BTC/USDT:USDT"
            sym_raw = r.get("symbol") or ""
            sym = sym_raw
            if isinstance(sym_raw, str) and sym_raw.endswith("USDT") and "/" not in sym_raw:
                base = sym_raw[:-4]
                sym = f"{base}/USDT:USDT"
            trade_side = _norm_trade_side(r.get("tradeSide") or r.get("openClose"))
            side = (r.get("side") or "").lower()
            if side not in ("buy", "sell"):
                side = "buy"

            # feeDetail can be list of dicts; totalFee often negative string.
            fee_cost = Decimal("0")
            fee_detail = r.get("feeDetail")
            if isinstance(fee_detail, list) and fee_detail:
                try:
                    fee_cost = abs(_to_decimal(fee_detail[0].get("totalFee")) or Decimal("0"))
                except Exception:
                    fee_cost = Decimal("0")
            elif isinstance(fee_detail, dict):
                fee_cost = abs(_to_decimal(fee_detail.get("totalFee")) or Decimal("0"))

            out.append(
                ExecutionFill(
                    ts=dt,
                    symbol=sym,
                    side=side,  # buy/sell
                    price=_to_decimal(r.get("price")) or Decimal("0"),
                    # Bitget mix fills commonly use baseVolume/quoteVolume (no 'size' field).
                    amount=_to_decimal(r.get("size"))
                    or _to_decimal(r.get("amount"))
                    or _to_decimal(r.get("baseVolume"))
                    or Decimal("0"),
                    fee_cost=fee_cost,
                    fee_currency="USDT",
                    maker_taker="unknown",
                    trade_side=trade_side,
                    pos_mode=str(r.get("posMode")) if r.get("posMode") is not None else None,
                    reported_profit_usdt=_to_decimal(r.get("profit")),
                    hold_side=PositionSide.unknown,  # will be filled via order detail
                    exchange="bitget",
                    trade_id=str(r.get("tradeId")) if r.get("tradeId") is not None else None,
                    order_id=str(r.get("orderId")) if r.get("orderId") is not None else None,
                    raw=r,
                )
            )
        out.sort(key=lambda f: f.ts)
        return out, (str(end_id) if end_id is not None else None)

    def fetch_mix_order_detail(
        self,
        *,
        product_type: str = "USDT-FUTURES",
        symbol_raw: str,
        order_id: str,
    ) -> dict[str, Any]:
        payload = self._get(
            "/api/v2/mix/order/detail",
            {"productType": product_type, "symbol": symbol_raw, "orderId": order_id},
        )
        d = payload.get("data") or {}
        if not isinstance(d, dict):
            return {}
        return d


def fetch_fills_windowed(
    client: BitgetRestClient,
    *,
    product_type: str,
    start: datetime,
    end: datetime,
    window_days: int = 7,
    page_limit: int = 100,
    max_pages_per_window: int = 50,
) -> list[ExecutionFill]:
    """
    Window-slice + paginate using idLessThan/endId.
    Response shape: data.fillList + data.endId
    """
    out: list[ExecutionFill] = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=window_days), end)
        id_less: str | None = None
        for _ in range(max_pages_per_window):
            fills, end_id = client.fetch_mix_order_fills(
                product_type=product_type,
                start=cur,
                end=nxt,
                limit=page_limit,
                id_less_than=id_less,
            )
            if not fills:
                break
            out.extend(fills)
            # next page cursor: use response endId (Bitget pagination contract)
            if not end_id or end_id == id_less:
                break
            id_less = end_id
            # if page isn't full, likely no more
            if len(fills) < page_limit:
                break
        cur = nxt
    # dedupe by (trade_id)
    seen: set[str] = set()
    dedup: list[ExecutionFill] = []
    for f in sorted(out, key=lambda x: x.ts):
        if f.trade_id and f.trade_id in seen:
            continue
        if f.trade_id:
            seen.add(f.trade_id)
        dedup.append(f)
    return dedup


