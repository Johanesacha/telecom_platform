# Telecom API Platform — Junie Engineering Guidelines

## Project Stack
FastAPI 0.115 + SQLAlchemy 2.0 async + asyncpg + PostgreSQL 16 + Redis 7 + Celery 5.4.
Pydantic v2. Python 3.12. pytest + pytest-asyncio.

## Non-Negotiable Architecture Rules
1. NEVER put business logic in app/api/endpoints/. Routes call service methods only.
2. NEVER put DB queries in app/services/. Services call repository methods only.
3. NEVER use float for monetary amounts. Use Python Decimal from the decimal module.
4. NEVER use SQLAlchemy lazy loading. Use selectinload() or joinedload() explicitly.
5. NEVER use mutable default arguments (def f(x=[]) — always use None and set in body).
6. ALWAYS make FastAPI routes and repository methods async def.
7. ALWAYS return ApiResponse[T] or PaginatedResponse[T] from endpoint functions.
8. ALWAYS use UUID4 primary keys. Never integer autoincrement.
9. ALWAYS use UTC timestamps. Use utcnow() from app.utils.time_utils — never datetime.now().
10. NEVER import from app.security/ in generated files. That module is off-limits.

## Security Absolute Rules
- NEVER modify any file under app/security/. These are manually written and reviewed.
- NEVER generate string comparison code. All sensitive comparisons use secrets.compare_digest().
- NEVER store raw API keys. Only key_prefix (first 12 chars) and key_hash (SHA-256 hex).
- NEVER use HS256 for JWT. The algorithm is RS256.

## Import Order Discipline
Follow this dependency hierarchy — never import in reverse:
app.utils → app.domain → app.schemas → app.repositories → app.services → app.api
Circular imports will cause application startup failure.

## SQLAlchemy 2.0 Async Rules
- Use mapped_column() with Mapped[type] annotations — not Column() (SQLAlchemy 1.x style)
- Use select() not session.query() (legacy 1.x style)
- Use scalars().all() or scalar_one_or_none() for result handling
- Use async with session.begin() for transactions that must be atomic

## Celery Task Rules
- Celery tasks are regular def (not async def)
- Tasks use SYNC_DATABASE_URL and synchronous SQLAlchemy Session, not AsyncSession
- Do not put await calls inside Celery tasks

## Code Style
- ruff for linting, black for formatting, line length 100
- Type annotations on every function parameter and return value
- Docstrings on public service methods only (Google style)
- No print() — use structured logging (import logging; logger = logging.getLogger(__name__))

## Testing Rules
- All test functions are async def decorated with @pytest.mark.asyncio
- Integration tests use httpx.AsyncClient, not FastAPI TestClient
- Use factories from tests/factories.py — never hardcode UUIDs or test data
- Test file mirrors the module: sms_service.py → test_sms_service.py

## What NOT to Generate
- Do not generate or modify anything under app/security/
- Do not hand-edit alembic/versions/ files — run 'alembic revision --autogenerate' instead
- Do not generate docker files or docker-compose.yml
- Do not add dependencies not already in pyproject.toml