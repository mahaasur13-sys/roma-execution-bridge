"""Crypto Payments — configuration via env vars."""
from __future__ import annotations

from pydantic_settings import BaseSettings


class CryptoSettings(BaseSettings):
    model_config = {"env_prefix": "CRYPTO_", "env_file": ".env.crypto"}

    provider: str = "nowpayments"
    nowpayments_api_key: str = ""
    nowpayments_ipn_secret: str = ""
    nowpayments_api_url: str = "https://api.nowpayments.io/v1"
    btcpay_server_url: str = ""
    btcpay_api_key: str = ""
    btcpay_store_id: str = ""
    invoice_ttl_minutes: int = 60
    force_network: str = ""
    min_confirmations: int = 3
    rate_limit_invoices_per_tenant_per_hour: int = 10

    heleket_api_key: str = ""
    heleket_api_url: str = "https://api.heleket.com/v1"
    cryptocloud_api_key: str = ""
    cryptocloud_api_url: str = "https://api.cryptocloud.plus/v2"
    monero_wallet_rpc_url: str = "http://127.0.0.1:18084/json_rpc"
    monero_daemon_rpc_url: str = "http://127.0.0.1:18081/json_rpc"
    monero_view_key: str = ""
    monero_primary_address: str = ""
    monero_poll_interval_seconds: int = 30
    monero_min_confirmations: int = 10
