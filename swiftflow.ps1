<#
SwiftFlow task runner.   .\swiftflow.ps1 <command> [args]

  up [-Observability]   build and start the full stack in Docker
  down | reset          stop the stack | stop it and delete all data
  infra                 start only Postgres, Redis and Kafka
  dev                   run every service on the host, one window each (needs: infra)
  seed                  create demo tenants, clients, warehouses, products and stock
  test                  unit and contract tests (no stack needed)
  test-int              integration, end-to-end and security tests (needs: up + seed)
  load | chaos | check  oversell load test | chaos run | invariant checker
  shots                 capture console screenshots into docs/screenshots
  logs [service] | ps   follow logs | list containers and ports
#>
param([Parameter(Position = 0)][string]$Command = "help", [Parameter(Position = 1, ValueFromRemainingArguments = $true)]$Rest, [switch]$Observability)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$envFile = Join-Path $root "deploy\compose\.env"
if (-not (Test-Path $envFile)) { Copy-Item (Join-Path $root "deploy\compose\.env.example") $envFile; Write-Host "created deploy\compose\.env from .env.example" }
$compose = @("compose", "-f", (Join-Path $root "deploy\compose\docker-compose.yml"), "--env-file", $envFile)
if ($Observability) { $compose += @("--profile", "observability") }
$services = @("order", "inventory", "notification", "gateway", "partner-sim")

function Invoke-Checked { & $args[0] @($args | Select-Object -Skip 1); if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
function Get-EnvValue([string]$name) { (Get-Content $envFile | Where-Object { $_ -match "^$name=" } | Select-Object -First 1) -replace "^$name=", "" }

switch ($Command) {
    "up"    { if ($Observability) { $env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://jaeger:4318" }; Invoke-Checked docker @compose up -d --build; Write-Host "`nConsole  http://localhost:3000`nGateway  http://localhost:8080`nNext:    .\swiftflow.ps1 seed" }
    "down"  { Invoke-Checked docker @compose --profile observability down }
    "reset" { Invoke-Checked docker @compose --profile observability down -v }
    "infra" { Invoke-Checked docker @compose up -d postgres redis kafka }
    "ps"    { Invoke-Checked docker @compose ps }
    "logs"  { Invoke-Checked docker @compose logs -f --tail 100 @Rest }
    "seed"  { Invoke-Checked $py (Join-Path $root "scripts\seed.py") }
    "load"  { Invoke-Checked $py (Join-Path $root "loadtest\oversell.py") @Rest }
    "check" { Invoke-Checked $py (Join-Path $root "chaos\check_invariants.py") @Rest }
    "chaos" { & (Join-Path $root "chaos\kill_mid_saga.ps1") @Rest; exit $LASTEXITCODE }
    "shots" { Invoke-Checked $py (Join-Path $root "scripts\screenshots.py") @Rest }
    "test"  {
        Invoke-Checked $py -m ruff check (Join-Path $root "libs") (Join-Path $root "services") (Join-Path $root "scripts") (Join-Path $root "tests") (Join-Path $root "loadtest") (Join-Path $root "chaos")
        Push-Location $root; try { Invoke-Checked $py -m pytest libs -q } finally { Pop-Location }
        foreach ($s in $services) {   # each service names its package "app", so each runs in its own process
            Push-Location (Join-Path $root "services\$s"); try { Write-Host "== $s"; Invoke-Checked $py -m pytest tests -q -p no:cacheprovider } finally { Pop-Location }
        }
    }
    "test-int" {
        Push-Location $root
        try {
            Invoke-Checked $py -m pytest tests/integration tests/security -q -p no:cacheprovider
            Invoke-Checked $py tests/e2e/smoke.py
        } finally { Pop-Location }
    }
    "dev" {
        # Hybrid mode: services on the host, each on its own port, against Dockerised infra.
        $pg = "localhost:5432/swiftflow"
        $common = "`$env:ENVIRONMENT='local'; `$env:LOG_FORMAT='console'; `$env:SWIFTFLOW_CONTRACTS_DIR='$root\libs\contracts\schemas'; `$env:PROVIDER_API_KEY='$(Get-EnvValue PROVIDER_API_KEY)'; `$env:DEMO_CLIENTS='$(Get-EnvValue DEMO_CLIENTS)'; `$env:WEBHOOK_ALLOWED_HOSTS='localhost:8004'; `$env:WEBHOOK_ALLOW_HTTP='true'; "
        $db = @{ order = "postgresql://order_svc:$(Get-EnvValue ORDER_DB_PASSWORD)@$pg"; inventory = "postgresql://inventory_svc:$(Get-EnvValue INVENTORY_DB_PASSWORD)@$pg"; notification = "postgresql://notification_svc:$(Get-EnvValue NOTIFICATION_DB_PASSWORD)@$pg" }
        $uv = Join-Path $root ".venv\Scripts\uvicorn.exe"
        $procs = @(
            @("order-api", "order", "& '$uv' app.main:app --port 8001 --reload"), @("order-worker", "order", "& '$py' -m app.worker"),
            @("inventory-api", "inventory", "& '$uv' app.main:app --port 8002 --reload"), @("inventory-worker", "inventory", "& '$py' -m app.worker"),
            @("notification-api", "notification", "& '$uv' app.main:app --port 8003 --reload"), @("notification-worker", "notification", "& '$py' -m app.worker"),
            @("partner-sim", "partner-sim", "& '$uv' app.main:app --port 8004 --reload"), @("gateway", "gateway", "& '$uv' app.main:app --port 8080 --reload")
        )
        foreach ($p in $procs) {
            $dbUrl = if ($db.ContainsKey($p[1])) { "`$env:DATABASE_URL='$($db[$p[1]])'; " } else { "" }
            $cmd = "`$host.UI.RawUI.WindowTitle='$($p[0])'; $common$dbUrl`$env:SERVICE_NAME='$($p[0])'; Set-Location '$root\services\$($p[1])'; $($p[2])"
            Start-Process powershell -ArgumentList @("-NoExit", "-Command", $cmd)
        }
        Start-Process powershell -ArgumentList @("-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='web'; Set-Location '$root\web'; npm run dev")
        Write-Host "Started 9 windows. Console: http://localhost:5173   Gateway: http://localhost:8080   Swagger: http://localhost:8001/docs (8002, 8003, 8004)"
        Write-Host "Stop the Docker copies of the services first if they are running:  docker $($compose -join ' ') stop order-api order-worker inventory-api inventory-worker notification-api notification-worker partner-sim gateway web"
    }
    default { Get-Help $PSCommandPath; Get-Content $PSCommandPath | Select-Object -Skip 1 -First 13 }
}
