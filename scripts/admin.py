"""swiftflow-admin: create tenants and API clients.

    python scripts/admin.py create-tenant --slug acme --name "Acme Outfitters"
    python scripts/admin.py create-client --tenant acme --role partner [--client-id acme-shop] [--secret ...]

Connects as the order service role (ORDER_DATABASE_URL, or built from deploy/compose/.env).
The secret is printed once; only its argon2 hash is stored.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import uuid
from pathlib import Path

import asyncpg
from argon2 import PasswordHasher

ROOT = Path(__file__).resolve().parent.parent
ROLE_SCOPES = {
    "admin": ["admin", "orders:read", "orders:write", "inventory:read", "webhooks:read", "webhooks:write"],
    "partner": ["orders:read", "orders:write", "inventory:read", "webhooks:read", "webhooks:write"],
}


def load_env() -> dict[str, str]:
    """deploy/compose/.env (or .env.example) merged under the process environment."""
    env: dict[str, str] = {}
    for name in (".env.example", ".env"):
        path = ROOT / "deploy" / "compose" / name
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    env.update(os.environ)
    return env


def order_dsn(env: dict[str, str]) -> str:
    return env.get("ORDER_DATABASE_URL") or (
        f"postgresql://order_svc:{env['ORDER_DB_PASSWORD']}@{env.get('POSTGRES_HOST', 'localhost')}:5432/swiftflow"
    )


async def ensure_tenant(conn: asyncpg.Connection, slug: str, name: str) -> str:
    row = await conn.fetchrow(
        "INSERT INTO tenants (id, slug, name) VALUES ($1, $2, $3) "
        "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id",
        uuid.uuid5(uuid.NAMESPACE_DNS, f"{slug}.tenants.swiftflow"),
        slug,
        name,
    )
    return str(row["id"])


async def ensure_client(conn: asyncpg.Connection, tenant_id: str, client_id: str, secret: str, role: str) -> None:
    await conn.execute(
        """
        INSERT INTO api_clients (id, tenant_id, client_id, secret_hash, role, scopes) VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (client_id) DO UPDATE
          SET secret_hash = EXCLUDED.secret_hash, role = EXCLUDED.role, scopes = EXCLUDED.scopes, revoked_at = NULL
        """,
        uuid.uuid4(),
        tenant_id,
        client_id,
        PasswordHasher().hash(secret),
        role,
        ROLE_SCOPES[role],
    )


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="swiftflow-admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("create-tenant")
    t.add_argument("--slug", required=True)
    t.add_argument("--name", required=True)
    c = sub.add_parser("create-client")
    c.add_argument("--tenant", required=True, help="tenant slug")
    c.add_argument("--role", choices=sorted(ROLE_SCOPES), default="partner")
    c.add_argument("--client-id")
    c.add_argument("--secret")
    args = parser.parse_args(argv)

    conn = await asyncpg.connect(order_dsn(load_env()))
    try:
        if args.cmd == "create-tenant":
            print("tenant_id:", await ensure_tenant(conn, args.slug, args.name))
        else:
            tenant_id = await conn.fetchval("SELECT id FROM tenants WHERE slug = $1", args.tenant)
            if tenant_id is None:
                print(f"no tenant with slug {args.tenant}", file=sys.stderr)
                return 1
            client_id = args.client_id or f"{args.tenant}-{args.role}-{secrets.token_hex(3)}"
            secret = args.secret or secrets.token_urlsafe(32)
            await ensure_client(conn, str(tenant_id), client_id, secret, args.role)
            print(f"client_id:     {client_id}\nclient_secret: {secret}\n(the secret is not stored and will not be shown again)")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
