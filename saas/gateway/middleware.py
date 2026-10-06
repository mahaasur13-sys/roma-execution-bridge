"""Gateway middleware assembly."""

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from saas.gateway.tenant_middleware import TenantMiddleware
from saas.gateway.auth_middleware import AuthMiddleware
from saas.gateway.branding_injector import BrandingInjectorMiddleware
from saas.gateway.rate_limiter import check_rate_limit, rate_limit_dependency


def setup_gateway_middleware(
    app: FastAPI,
    tenant_config: dict = None,
    redis_url: str = None,
    jwt_secret: str = None,
    allowed_origins: list[str] = None,
) -> None:
    """
    Wire all gateway middleware into a FastAPI app.

    Порядок (P1-A): Starlette собирает стек в ОБРАТНОМ порядке добавления —
    последний add_middleware() оказывается САМЫМ ВНЕШНИМ и исполняется первым.
    Значит порядок добавления здесь обратен порядку исполнения.

    Исполнение запроса (внешний → внутренний):
    1. CORS                 — внешний, обрабатывает preflight
    2. BrandingInjector     — читает tenant_id ПОСЛЕ call_next, поэтому позиция не критична
    3. TenantMiddleware     — резолвит tenant и пишет request.state.tenant_id
    4. AuthMiddleware       — читает tenant_id, который уже выставлен шагом 3

    Прежняя версия утверждала обратное («первый add_middleware = первый в цепочке»)
    и добавляла TenantMiddleware первой; из-за этого AuthMiddleware становился
    внешним, видел tenant_id=None, не находил auth_cfg и пропускал защищённые
    роуты без ключа (CWE-287). Комментарий был источником класса дефекта, а не
    только описанием инстанса.

    Безопасность опирается на ФАКТ порядка, а не на комментарий: порядок
    зафиксирован тестами saas/gateway/tests/test_gateway.py (paid-тенант без
    ключа → 401, FREE-тенант без ключа → 200).
    """
    if allowed_origins is None:
        allowed_origins = ["*"]

    # Внутренний по исполнению: проверка ключа идёт ПОСЛЕ резолва tenant.
    app.add_middleware(
        AuthMiddleware,
        jwt_secret=jwt_secret,
        allowed_origins=allowed_origins,
        tenant_config=tenant_config or {},
    )

    # Резолв тенанта — внешний относительно AuthMiddleware.
    app.add_middleware(
        TenantMiddleware,
        tenant_config=tenant_config or {},
    )

    # Инъекция брендинга — между резолвом тенанта и CORS.
    app.add_middleware(
        BrandingInjectorMiddleware,
        tenant_config=tenant_config or {},
    )

    # CORS — добавляется последним, поэтому исполняется первым (preflight).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


__all__ = [
    "setup_gateway_middleware",
    "check_rate_limit",
    "rate_limit_dependency",
]
