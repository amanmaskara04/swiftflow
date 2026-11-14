<#
Chaos run: restart workers (and the broker once) while orders are in flight, then check
that every invariant still holds.

    .\chaos\kill_mid_saga.ps1 [-Orders 400] [-Units 150] [-Rounds 6]
#>
param([int]$Orders = 400, [int]$Units = 150, [int]$Rounds = 6)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$compose = @("compose", "-f", (Join-Path $root "deploy\compose\docker-compose.yml"), "--env-file", (Join-Path $root "deploy\compose\.env"))
$victims = @("order-worker", "inventory-worker", "notification-worker")

Write-Host "Starting $Orders orders against $Units units, with payment latency so sagas overlap the restarts..."
& $py -c "import httpx;c=httpx.Client(base_url='http://localhost:8080',trust_env=False);t=c.post('/v1/auth/token',json={'client_id':'acme-admin','client_secret':'acme-admin-secret'}).json()['access_token'];print(c.put('/v1/sim/config',json={'failure_rate':0.1,'latency_ms':300,'outage':False},headers={'Authorization':'Bearer '+t}).json())"
$load = Start-Process -FilePath $py -ArgumentList @((Join-Path $root "loadtest\oversell.py"), "--orders", $Orders, "--units", $Units, "--concurrency", "40", "--no-report", "--no-reset", "--timeout", "240") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $env:TEMP "swiftflow-chaos-load.txt")

Start-Sleep -Seconds 2
for ($i = 1; $i -le $Rounds; $i++) {
    $v = Get-Random -InputObject $victims
    Write-Host ("[{0}/{1}] killing {2}" -f $i, $Rounds, $v)
    & docker @compose kill $v | Out-Null
    Start-Sleep -Milliseconds 800
    & docker @compose start $v | Out-Null
    if ($i -eq [math]::Ceiling($Rounds / 2)) {
        Write-Host "      restarting the Kafka broker"
        & docker @compose restart kafka | Out-Null
    }
    Start-Sleep -Seconds 4
}

Write-Host "Waiting for the load run to finish and queues to drain..."
$load.WaitForExit()
& $py -c "import httpx;c=httpx.Client(base_url='http://localhost:8080',trust_env=False);t=c.post('/v1/auth/token',json={'client_id':'acme-admin','client_secret':'acme-admin-secret'}).json()['access_token'];c.put('/v1/sim/config',json={'failure_rate':0,'latency_ms':20,'outage':False},headers={'Authorization':'Bearer '+t})"
Get-Content (Join-Path $env:TEMP "swiftflow-chaos-load.txt") | Select-String -Pattern "Confirmed|Rejected|Cancelled|settled|unsettled|Oversold"
Start-Sleep -Seconds 20
& $py (Join-Path $root "chaos\check_invariants.py") --grace 15
exit $LASTEXITCODE
