# Security

What SwiftFlow defends against, how, and what proves it. Every row in the tables below has a test in `tests/security/`, `tests/integration/` or a service's unit tests; run them with `.\swiftflow.ps1 test` and `.\swiftflow.ps1 test-int`.

## Threat model

| Actor | Can do | Must not be able to do |
|-------|--------|------------------------|
| Anonymous caller | Reach the gateway | Read or change anything; learn which client IDs exist |
| Partner client of tenant A | Place and read A's orders, read A's stock, manage A's webhooks | See or change tenant B's data; use admin actions; reach internal services through webhooks |
| Admin client of tenant A | Everything for tenant A, plus fault injection in development | Anything in tenant B; forge another tenant's token |
| A compromised or buggy service | Read and write its own schema | Read another service's schema; skip row-level security by accident |

## Controls

### Identity and tokens
| Control | Detail |
|---------|--------|
| Client secrets | Stored only as argon2id hashes; shown once at creation |
| Tokens | Ed25519-signed JWTs, 15 minutes, no refresh tokens |
| Verification | Algorithm allow-list of `EdDSA` only (never read from the token), key chosen by `kid` from the issuer's JWKS, `iss`, `aud`, `exp` and required claims checked |
| No shared signing secret | Only the order service holds the private key; other services verify with the public key |
| Guessing | The token endpoint is throttled per client ID; an unknown client and a wrong secret return the same answer in the same time (dummy hash) |

Tested: tampered payload, another key with the real `kid`, `alg: none`, HS256 signed with the public key (key confusion), expired, missing or unknown `kid`, wrong audience — all `401` on every service.

### Tenant isolation
| Control | Detail |
|---------|--------|
| Tenant source | Only the verified token's `tenant_id`; values in bodies and queries are ignored |
| Row-level security | `ENABLE` + `FORCE` on every tenant table; the tenant is set per transaction and reverts at commit, so pooled connections stay clean |
| Fails closed | A query with no tenant set raises an error instead of returning rows |
| No existence leak | Another tenant's order is `404`, not `403` |
| Per-service database roles | Each role owns one schema and has no rights on the others; none is superuser or `BYPASSRLS` |

Tested against the live database: a service role cannot select from another schema; a tenant session sees only its rows and cannot insert or update another tenant's; the invariant checker confirms RLS is forced on every tenant table.

### Input and injection
| Control | Detail |
|---------|--------|
| SQL | Bound parameters everywhere; `LIKE` wildcards in search terms are escaped |
| Validation | Pydantic models with length, range and pattern limits; 1 MB body limit (`413`); malformed cursors and characters the database cannot store are `422`, never `500` |
| Errors | RFC 9457 problem JSON with a request ID; no stack traces, SQL or driver messages |
| Console | React renders all server data as text (no `dangerouslySetInnerHTML`); strict Content-Security-Policy (`self` only, no inline scripts); the token is never placed in a URL |

### Webhooks (SSRF and authenticity)
| Control | Detail |
|---------|--------|
| URL rules | `https` only, no credentials, and every resolved address must be public; checked when the endpoint is added **and again before every delivery** |
| No redirects | Deliveries never follow redirects |
| Signing | `SwiftFlow-Signature: t=<unix>,v1=<HMAC-SHA256 of "t.body">`; receivers reject timestamps older than 5 minutes and compare in constant time |
| Development exception | An explicit `host:port` allow-list (the simulator); exact match, so another port on the same host is still refused |

Tested: 21 internal or malformed targets (cloud metadata address, loopback, private ranges, internal service names, other schemes, credential tricks, decimal and hex IP forms) are refused; a tampered body, stale timestamp or wrong secret is marked invalid.

### Gateway and network
| Control | Detail |
|---------|--------|
| Allow-list routing | Only known path patterns are forwarded, each to a fixed upstream; `..`, `//`, `\` and encoded characters are refused; request headers are filtered to a short allow-list |
| Simulator | Provider endpoints are never routed; control endpoints need an admin token and are routed only in `local` and `ci` |
| CORS | Only the configured console origins |
| Rate limiting | Token bucket per tenant in Redis (`429` + `Retry-After`); live-stream connections capped per tenant |
| Ports | Every published port binds to `127.0.0.1` |
| Containers | Non-root user, read-only root filesystem, all capabilities dropped, `no-new-privileges` |
| Secrets | `.env` is gitignored; only `.env.example` with development values is committed; a non-development process refuses to start with a development secret |

### Payments
| Control | Detail |
|---------|--------|
| No double charge | The order ID is the charge's idempotency key; retries after a crash or timeout return the first result |
| Refunds | A cancelled order that was charged is refunded; a cancel that races an in-flight charge voids it by key |

Tested by the invariant checker after load and chaos runs: zero double charges, no cancelled order holding money.

## Found and fixed during testing

| Finding | Fix |
|---------|-----|
| A NUL character in a search term or text field caused a `500` | Database data errors now map to `422` without echoing the driver message |
| The rate limiter silently failed open after a bad edit to its Redis script | Script repaired; a unit test now checks the script, and a live test drives the real bucket and fails if it does not run |
| Service and database ports were published on all interfaces | Bound to `127.0.0.1` |
| Containers ran with a writable filesystem and default capabilities | Read-only, capabilities dropped |
| The first design had no refund for a charged order that was later cancelled | Refund flow added to the saga |
| The live stream took its token in the URL | Moved to the `Authorization` header |
| Simulator fault controls needed no authentication | Admin token required |

## Known limits (deliberate, for a local demo)

- Kafka and Redis run without authentication or TLS. In production they would use SASL/TLS and a password, on a private network.
- Webhook signing secrets and the fallback signing key are stored unencrypted in Postgres. Production would use a KMS or secret manager, and supply the signing key from it.
- Tokens cannot be revoked before they expire (15 minutes).
- The webhook address check and the request are separate steps, so a DNS answer could change between them. Pinning the connection to the checked address would close that gap.
- The gateway's own `/metrics` and `/docs` are reachable on port 8080; the console's nginx forwards only `/v1` and `/.well-known`, and a production edge should do the same.
- The login throttle is per client ID, so someone who knows a client ID can slow that client's sign-ins.
- No dependency or image vulnerability scan has been run yet (Trivy is planned with CI).
