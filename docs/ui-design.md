# UI design — SwiftFlow Console

## 1. Brief

| | |
|---|---|
| **Product** | SwiftFlow Console, the web app an operations team uses every shift |
| **Audience** | Ops leads and warehouse coordinators at a multi-warehouse retailer; also the interviewer watching a demo |
| **Single job** | Show where every order is, and prove the stock numbers are true |
| **Second job** | Make the distributed system visible: services, ports, events and failures, live |

The design draws on the warehouse floor: **shipping labels, manifest paper, rack location codes, and the yellow-and-black floor markings that mean "something is moving here."** It's a light, cool interface (not cream, not dark-with-neon) because ops consoles sit on screens in bright rooms all day.

## 1a. Revision 2 — the reference prototype wins

**`docs/design/prototype.html` is the visual source of truth.** Open it in a browser (`#orders,drawer`, `#map,dark`, `#chaos`, `#stock` select screens). Where this document and the prototype disagree on look, follow the prototype; where the prototype is silent (sign-in, webhooks, dialogs, empty and error states), follow this document in the prototype's style. Changes from the first draft:

| Area | Revision 2 |
|------|-----------|
| Shell | The left rail is **ink-dark** (`#141B24`) in both themes, 216 px, with the wordmark `SWIFT` white + `FLOW` yellow in Archivo 900 expanded. The active item has a 3 px yellow bar. A "Under the hood" group separates System map and Chaos lab from the daily screens |
| Overview hero | **The line board** replaces the KPI row as the hero: the five stations as one thick rail, the count of orders at each station, yellow ID tokens that slide to the next station as events arrive, and a red branch to the two terminals (Rejected, Cancelled). Beside it, the **oversold odometer**: four dark wheels reading `0000` with "Stock is true." KPIs become a quiet second row |
| Tokens | Wash colours added for badges: `--go-wash`, `--stop-wash`, `--route-wash`, `--signal-wash`; `--sunk` for inset areas. Exact values are in the prototype's `:root` |
| Health strip | One bordered chip per node: light, name, port in mono |
| Chaos lab | A "PASSED" / "FAILED" rubber-stamp (rotated −3°, 2 px border) on the last oversell run |
| Session | The token lives in memory and `sessionStorage`; the client secret lives **in memory only**. When the token expires and the secret is gone (after a reload), return to sign-in |
| Live stream | Read with `fetch` + `ReadableStream` and an `Authorization` header, not `EventSource` with a token in the URL |
| System map | Draw partner-sim to the right of notification, with dashed lines labelled "charge" (from order) and "webhook" (from notification). Node status comes from `/v1/system/health`. The prototype's dashed lines are a sketch; tidy them |
| Line board branch | Two red branches leave the rail between stations and run down to their terminals: after Placed to **Rejected**, after Paid to **Cancelled** |
| Not built | The command palette (`Ctrl K`), tenant switching from the top bar (sign out and use another client instead) and a mock-data mode were cut; the Overview's third KPI shows units reserved rather than webhook delivery rate |

## 2. Signature: the saga rail

Each order's lifecycle is drawn as a **rail line with stations**, like a transit map: `Placed → Reserved → Paid → Confirmed → Shipped`. The train (a filled marker) sits at the current station. When the saga compensates, the line **branches down** to a red terminal (`Released → Cancelled`) or stops at `Rejected`. Each station shows its timestamp and the event that moved it, in mono.

```
 Placed ━━━━━ Reserved ━━━━━ Paid ━━━━━ Confirmed ━━━━━ Shipped
  ●10:15:00.12   ●10:15:00.31  ●10:15:00.58  ◉ 10:15:00.60   ○
  OrderPlaced    StockReserved  approved     OrderConfirmed

 Placed ━━━━━ Reserved ━━━━━ Paid
  ●             ●              ╳ declined
                               ┃
                               ┗━━━ Released ━━━ Cancelled
                                     ●             ◉ payment_declined
```

The rail appears at three sizes:
- **Full** — order detail drawer, with timestamps and events
- **Compact** — one row per order in the Orders table (stations as dots, no labels)
- **Live** — the Overview's "In flight" strip, where markers move as SSE events arrive

This is the one bold element. Everything else stays quiet so the rail reads instantly.

## 3. Design tokens

### Colour

| Token | Light | Dark | Use |
|-------|-------|------|-----|
| `--paper` | `#EDF0F3` | `#0F141A` | App background (cool manifest grey) |
| `--sheet` | `#FFFFFF` | `#171D25` | Cards, tables, drawers |
| `--ink` | `#18202B` | `#E6EBF0` | Primary text, rail line |
| `--ink-2` | `#56616F` | `#9AA6B4` | Secondary text, labels |
| `--rule` | `#D5DBE2` | `#2A333F` | Hairlines, table borders |
| `--signal` | `#F2C230` | `#F2C230` | Safety yellow: in flight, focus ring, primary action fill |
| `--signal-ink` | `#1A1400` | `#1A1400` | Text on yellow |
| `--go` | `#0F8B6F` | `#2BB894` | Confirmed, shipped, healthy |
| `--stop` | `#C8402E` | `#EF6B57` | Rejected, cancelled, down, compensation branch |
| `--route` | `#2D5BD7` | `#7C9BFF` | Links, reserved state, info |

Status mapping (used everywhere: badges, rail stations, charts):

| Status | Colour | Glyph |
|--------|--------|-------|
| `PENDING` | `--signal` | ◐ |
| `RESERVED` | `--route` | ◑ |
| `CONFIRMED` | `--go` | ● |
| `SHIPPED` | `--ink` | ■ |
| `REJECTED` | `--stop` | ✕ |
| `CANCELLED` | `--stop` (outline) | ⊘ |

Status is never shown by colour alone: every badge has a glyph and a word.

### Type

| Role | Face | Settings |
|------|------|----------|
| Display | **Archivo**, width 125 (expanded), weight 700–800 | Screen titles, KPI figures, rail station names. Uppercase with 0.04em tracking for labels; this is the shipping-label voice |
| Body | **Instrument Sans**, 400 / 500 / 600 | Everything people read |
| Data | **IBM Plex Mono**, 400 / 500 | IDs, SKUs, rack codes, timestamps, ports, JSON |

Scale (rem): 0.75 · 0.8125 · 0.875 (body) · 1 · 1.25 · 1.75 · 2.5 (KPI) · 3.5 (oversold counter). Line height 1.5 for body, 1.1 for display. Tabular numerals on all figures.

### Space, shape, depth

- 4 px base grid; spacing steps 4 · 8 · 12 · 16 · 24 · 32 · 48.
- Radius: 6 px on controls, 10 px on cards. Badges are 4 px, label-like, never pills.
- Depth from hairlines, not shadows. The only shadow is on drawers.
- **Hazard stripe:** a 4 px diagonal yellow/ink stripe (`repeating-linear-gradient(135deg, …)`) marks exactly two things: the Chaos Lab header and any banner saying a fault is injected. It must stay rare to stay meaningful.

### Motion

- Rail markers glide between stations over 400 ms (`cubic-bezier(.2,.8,.2,1)`).
- New rows in the live event tape slide in from the top over 200 ms, then the yellow left border fades over 2 s.
- KPI figures tick to new values; nothing else animates.
- `prefers-reduced-motion`: markers and rows appear in place, figures change instantly.

## 4. Layout shell

```
┌────────┬───────────────────────────────────────────────────────────────────┐
│ SWIFT  │  Acme Outfitters ▾        ORD ● INV ● NOT ● SIM ● KFK ●    ⌘K  ◐  │
│ FLOW   ├───────────────────────────────────────────────────────────────────┤
│        │                                                                   │
│ ▣ Over │   SCREEN TITLE                                     [ Primary ]    │
│ ☰ Ordr │   One-line description of what this screen is for                 │
│ ▤ Stck │                                                                   │
│ ↯ Hook │   ┌─────────────────────────────────────────────────────────────┐ │
│ ◎ Map  │   │                                                             │ │
│ ⚠ Chao │   │  content                                                    │ │
│        │   │                                                             │ │
│        │   └─────────────────────────────────────────────────────────────┘ │
│ admin  │                                                                   │
│ acme ⏻ │                                                                   │
└────────┴───────────────────────────────────────────────────────────────────┘
```

- **Left rail** (72 px collapsed, 220 px expanded): icon + label, current screen marked with a yellow 3 px bar.
- **Top bar:** tenant name, the **health strip** (one light per service, from `/v1/system/health`, polled every 5 s; hover shows port and latency) and the light/dark theme toggle. The command palette and tenant switching in the original design were not built (see 1a).
- **Content:** max width 1440 px, 32 px gutter (16 px on mobile).
- Below 900 px the rail becomes a bottom tab bar with five items; Chaos Lab moves into the overflow.

## 5. Screens

### 5.1 Sign in

```
┌───────────────────────────────────────────────────────────┐
│  SWIFTFLOW                                                │
│  Order fulfillment that never oversells.                  │
│                                                           │
│  Client ID      [ acme-admin                ]             │
│  Client secret  [ ••••••••••••              ]             │
│                 [   Sign in   ]                           │
│                                                           │
│  Demo clients (local only)                                │
│  ┌──────────────┬───────────┬────────────────────────┐    │
│  │ acme-admin   │ admin     │ Acme Outfitters    Use │    │
│  │ acme-partner │ partner   │ Acme Outfitters    Use │    │
│  │ globex-admin │ admin     │ Globex Supply      Use │    │
│  └──────────────┴───────────┴────────────────────────┘    │
└───────────────────────────────────────────────────────────┘
```
- As built, sign-in is also the landing page: beside the form it explains the project (what it proves, the services, an animated order journey) and carries the same theme toggle as the shell.
- "Use" fills both fields. The demo table appears only when the API reports `environment = local`.
- Error: "That client ID and secret don't match. Check both and try again."
- The token is held in memory and `sessionStorage`; on expiry (15 min) the app silently requests a new one with the stored credentials, or returns to sign-in.

### 5.2 Overview

```
 OVERVIEW                                         Last 60 min ▾
 ┌──────────┬──────────┬──────────┬──────────┬───────────────┐
 │ IN FLIGHT│ CONFIRMED│ REJECTED │ SAGA p95 │ OVERSOLD UNITS│
 │   12     │   1,204  │    38    │  0.61 s  │      0        │
 └──────────┴──────────┴──────────┴──────────┴───────────────┘
 ┌──────────────────────────────────────┐ ┌──────────────────┐
 │ Orders per minute (stacked by status)│ │ LIVE EVENT TAPE  │
 │ ▁▂▃▅▆▇▆▅▃▂▃▅▇█▇▅                     │ │ 10:15:00.60      │
 │                                      │ │ OrderConfirmed   │
 └──────────────────────────────────────┘ │ #0b6e… · 3,600¢  │
 ┌──────────────────────────────────────┐ │ 10:15:00.31      │
 │ IN FLIGHT (live saga rails)          │ │ StockReserved    │
 │ #0b6e ●━━●━━◉━━○━━○                  │ │ …                │
 │ #91aa ●━━◉━━○━━○━━○                  │ │                  │
 └──────────────────────────────────────┘ └──────────────────┘
```
- **Oversold units** is the hero figure: 3.5 rem, Archivo 800, `--go` when 0, and it would turn `--stop` if it ever weren't. It sums `oversold_rows` from inventory.
- The event tape is the SSE stream: event type in Archivo caps, order ID and figures in mono, colour bar by status.
- Empty state: "No orders yet. Place one from Orders, or run the oversell test in Chaos Lab."

### 5.3 Orders

```
 ORDERS                                              [ New order ]
 [ Search ID or customer ref ]  Status: All ▾ Pending Reserved …
 ┌──────────┬───────────────┬──────────────────┬───────┬────────┬────────┐
 │ ORDER    │ CUSTOMER REF  │ PROGRESS         │ ITEMS │ TOTAL  │ AGE    │
 ├──────────┼───────────────┼──────────────────┼───────┼────────┼────────┤
 │ 0b6e3f1a │ shopify-#10412│ ●━●━●━◉━○ CONFIRMED │ 3  │ $54.00 │ 2m     │
 │ 91aa07c2 │ amazon-88-12  │ ●━✕ REJECTED     │ 1     │ —      │ 5m     │
 └──────────┴───────────────┴──────────────────┴───────┴────────┴────────┘
                                                        [ Load more ]
```
- The status filter is a row of pills, **one status at a time**: choosing another replaces the current one, choosing it again returns to All. (The API accepts several `status` values; the console sends one.)
- Row click opens the **order drawer** (right, 520 px): header with ID (copy button), status badge, customer ref; the **full saga rail**; items table (SKU, qty, warehouse code, unit price); the event list from `/v1/events?aggregate_id=` with expandable JSON; actions **Cancel order** (with confirm) and **Mark shipped** (admin only, shown only when `CONFIRMED`).
- **New order** dialog: SKU picker with live availability next to each SKU, qty stepper, customer ref, and a generated idempotency key shown in mono with a "new key" button — so a demo can show a replayed request returning the same order.
- The list refetches when an SSE event for a visible order arrives; no polling.

### 5.4 Stock

```
 STOCK                                   [ Add product ] [ Add warehouse ]
 ┌───────────────┬──────────────────────┬──────────────────────┬────────┐
 │ SKU           │ BLR-1 Bengaluru N.   │ DEL-2 Delhi Central  │ TOTAL  │
 ├───────────────┼──────────────────────┼──────────────────────┼────────┤
 │ TEE-CLASSIC-M │ ████████░░░░  88/100 │ ██████░░░░░  52/60   │ 140    │
 │ Classic tee M │ 12 reserved          │ 8 reserved           │ avail. │
 └───────────────┴──────────────────────┴──────────────────────┴────────┘
```
- Each cell is a **bin card**: a bar where the ink part is available, the route-blue part is reserved, and the empty track is the gap to the last cycle count. Figures in mono: `available/on hand`.
- Clicking a cell opens **Adjust stock**: mode (Set count / Add or remove), quantity, reason, and the current version. A stale version returns "Someone changed this stock while you were editing. Reload to see the latest count."
- A cell flashes yellow for 2 s when a `StockLevelChanged` event arrives.

### 5.5 Webhooks

Two tabs: **Endpoints** and **Deliveries**.
- Endpoints: URL, event types as label chips, status, created. **Add endpoint** dialog; on success, a one-time panel shows the signing secret with "Copy secret — it won't be shown again." A **Use the test receiver** shortcut fills `http://partner-sim:8004/v1/hooks/demo` and registers the secret with the simulator, so signature checks show green.
- Deliveries: status badge, event type, attempts, next attempt (relative), last response code and error, **Retry** on failed rows.
- **Test receiver** panel: what the simulator received, with a "Signature valid" or "Signature invalid" stamp per request.

### 5.6 System map

The live architecture diagram, drawn in SVG from `/v1/system/health`:

```
                    ┌──────────┐
                    │ web :3000│
                    └────┬─────┘
                    ┌────┴─────┐
                    │ gw :8080 │
                    └┬───┬───┬─┘
          ┌──────────┘   │   └──────────┐
   ┌──────┴─────┐ ┌──────┴─────┐ ┌──────┴──────┐   ┌──────────────┐
   │ order :8001│ │ inv  :8002 │ │ notif :8003 │   │ sim   :8004  │
   │ worker 9101│ │ worker 9102│ │ worker 9103 │   └──────────────┘
   └──────┬─────┘ └──────┬─────┘ └──────┬──────┘
   ═══════╧══════ KAFKA :9092 ══════════╧════════  (topics, lag)
   [ Postgres :5432 ]                [ Redis :6379 ]
```
- Each node: name, port, status light, latency. Down nodes get a `--stop` outline and the error text.
- The Kafka bus is drawn as a thick ink line; event pulses travel along it when SSE events arrive (off under reduced motion).
- Side panel: topics with partition count and the last event seen per topic.

### 5.7 Chaos Lab

Hazard-striped header: "Faults you inject here are real. They affect every tenant in this environment."

| Control | Effect |
|---------|--------|
| Payment decline rate (0–100 % slider) | `PUT /v1/sim/config failure_rate` |
| Payment latency (0–5000 ms) | `latency_ms` |
| Payment provider outage (switch) | `outage` → circuit breaker opens, orders wait in `RESERVED` |
| **Oversell test** | Pick a SKU, set its stock to N (default 100), fire M concurrent orders (default 500) from the browser, each with its own idempotency key |

The oversell test result panel fills live as orders settle:

```
 OVERSELL TEST  TEE-CLASSIC-M · 100 in stock · 500 orders
 ┌───────────┬───────────┬───────────┬──────────────────┐
 │ CONFIRMED │ REJECTED  │ IN FLIGHT │ OVERSOLD UNITS   │
 │    100    │    400    │     0     │        0         │
 └───────────┴───────────┴───────────┴──────────────────┘
 Stock after: on hand 100 · reserved 100 · available 0     ✔ PASSED
 Placed in 1.84 s · all settled in 6.20 s · p95 accept latency 212 ms
```

## 6. Components

| Component | Notes |
|-----------|-------|
| `SagaRail` | Props: `history`, `status`, `size: full / compact / live`. Pure SVG; stations evenly spaced; compensation branch drawn when history contains a release or decline |
| `StatusBadge` | Glyph + word, 4 px radius, colour from the status map |
| `BinCard` | Stacked bar + mono figures; accessible name "88 available of 100 on hand, 12 reserved" |
| `HealthStrip` | One light per node; `aria-live="polite"` announces changes |
| `EventTape` | Virtualised list, max 200 items kept |
| `Kpi` | Label (Archivo caps 0.75 rem) over figure (Archivo 2.5 rem, tabular) |
| `Mono` | IBM Plex Mono span with optional copy button |
| `Drawer`, `Dialog`, `Toast` | Focus-trapped, Escape closes, return focus to trigger |
| `ProblemAlert` | Renders an RFC 9457 body: title, detail, and request ID in mono for support |

## 7. Writing rules

- Sentence case everywhere except Archivo label caps.
- Buttons say what happens: **Place order**, **Cancel order**, **Mark shipped**, **Save count**, **Add endpoint**, **Retry delivery**, **Run oversell test**. The toast repeats the verb: "Order placed", "Count saved".
- Name things by what people control: "Endpoints", not "webhook subscriptions"; "Stock", not "inventory levels". Technical names (event types, ports) appear in mono as supporting detail, not as labels.
- Errors say what happened and what to do; they never apologise. API problem titles are shown as-is, since they are written to the same rules.

## 8. Accessibility and quality floor

- Contrast: body text ≥ 4.5:1 on `--paper` and `--sheet` in both themes; yellow is used as a fill behind `--signal-ink`, never as text on white.
- Visible focus: 2 px `--signal` ring with a 2 px `--ink` offset, on every interactive element.
- Full keyboard use: table rows are buttons, drawers trap focus.
- Charts have a table alternative ("View as table").
- Works from 360 px wide; tables collapse to stacked cards below 700 px.

## 9. Frontend stack

| Concern | Choice |
|---------|--------|
| Build | Vite + React 18 + TypeScript (strict) |
| Routing | React Router |
| Server state | TanStack Query (SSE events invalidate the matching queries) |
| Styling | Tailwind CSS with the tokens above as CSS variables; light and dark via `data-theme` and `prefers-color-scheme` |
| Charts | Recharts, coloured from the status map |
| Icons | lucide-react |
| Fonts | `@fontsource` packages for Archivo (variable, with width axis), Instrument Sans, IBM Plex Mono — bundled, no CDN |

Directory layout:
```
web/src/
  api/          client.ts (fetch + problem parsing + token refresh), hooks per resource, sse.ts
  components/   SagaRail, StatusBadge, BinCard, HealthStrip, EventTape, Kpi, Drawer, Dialog, …
  screens/      SignIn, Overview, Orders, Stock, Webhooks, SystemMap, ChaosLab
  styles/       tokens.css, globals.css
  lib/          format.ts (money, time, ids), idempotency.ts
```
