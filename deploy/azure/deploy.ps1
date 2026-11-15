<#
Creates (or updates) the SwiftFlow VM on Azure.   .\deploy\azure\deploy.ps1 <command>

  create   resource group, VM with a public DNS name, ports 80 and 443 only; first boot installs and starts everything
  update   pull the latest commit on the VM and rebuild
  status   VM power state and the bootstrap log tail
  stop     deallocate the VM (stops compute charges; the disk and address remain)
  start    start it again and bring it up to the latest commit
  cd-setup one-time: let the GitHub workflow deploy to this VM (creates an Azure app identity limited to
           the VM, trusts this repository's "demo" environment through OIDC, stores three repository secrets)
  destroy  delete the resource group and everything in it

Needs: az login. SSH is not opened; management goes through `az vm run-command`.
#>
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [string]$ResourceGroup = "swiftflow-rg",
    [string]$Location = "eastasia",
    [string]$Size = "Standard_B2als_v2",
    [string]$DnsLabel = "swiftflow-amanmaskara04",
    [string]$Repo = "amanmaskara04/swiftflow"
)
$ErrorActionPreference = "Stop"
$az = "D:\Apps\AzureCLI\bin\az.cmd"
$vm = "swiftflow-vm"
$fqdn = "$DnsLabel.$Location.cloudapp.azure.com"

function Invoke-Az { & $az @args; if ($LASTEXITCODE -ne 0) { throw "az $($args[0]) $($args[1]) failed" } }
function Invoke-OnVm([string]$script) {
    Invoke-Az vm run-command invoke -g $ResourceGroup -n $vm --command-id RunShellScript --scripts $script --query "value[0].message" -o tsv
}

switch ($Command) {
    "create" {
        Invoke-Az group create -n $ResourceGroup -l $Location -o none
        $init = Join-Path $env:TEMP "swiftflow-cloud-init.yaml"
        (Get-Content (Join-Path $PSScriptRoot "cloud-init.yaml") -Raw).Replace("__FQDN__", $fqdn) | Set-Content $init -Encoding ascii
        Invoke-Az vm create -g $ResourceGroup -n $vm --image Ubuntu2404 --size $Size `
            --admin-username azureuser --generate-ssh-keys --public-ip-sku Standard --public-ip-address-dns-name $DnsLabel `
            --os-disk-size-gb 30 --storage-sku StandardSSD_LRS --nsg-rule NONE --custom-data $init -o none
        Invoke-Az vm open-port -g $ResourceGroup -n $vm --port "80,443" --priority 1000 -o none
        Write-Host "VM created. First boot takes about 10 minutes: https://$fqdn"
    }
    "update" { Invoke-OnVm "cd /opt/swiftflow && git pull --ff-only && bash deploy/azure/bootstrap.sh $fqdn 2>&1 | tail -n 15" }
    "status" {
        Invoke-Az vm get-instance-view -g $ResourceGroup -n $vm --query "instanceView.statuses[1].displayStatus" -o tsv
        Invoke-OnVm "tail -n 12 /var/log/swiftflow-bootstrap.log; docker ps --format '{{.Names}} {{.Status}}'"
    }
    "stop" { Invoke-Az vm deallocate -g $ResourceGroup -n $vm -o none; Write-Host "Deallocated." }
    "start" {
        Invoke-Az vm start -g $ResourceGroup -n $vm -o none
        # Commits pushed while the VM was stopped were not deployed, so catch up now.
        Invoke-OnVm "cd /opt/swiftflow && git pull --ff-only && bash deploy/azure/bootstrap.sh $fqdn 2>&1 | tail -n 5"
        Write-Host "Started: https://$fqdn"
    }
    "cd-setup" {
        # No password or key is created: GitHub proves its identity with a short-lived OIDC token,
        # and Azure accepts it only from this repository's "demo" environment.
        $name = "swiftflow-github-deploy"
        $appId = & $az ad app list --display-name $name --query "[0].appId" -o tsv
        if (-not $appId) { $appId = Invoke-Az ad app create --display-name $name --query appId -o tsv }
        $spId = & $az ad sp list --filter "appId eq '$appId'" --query "[0].id" -o tsv
        if (-not $spId) { $spId = Invoke-Az ad sp create --id $appId --query id -o tsv }
        $cred = Join-Path $env:TEMP "swiftflow-federated.json"
        @{ name = "github-demo"; issuer = "https://token.actions.githubusercontent.com"; subject = "repo:${Repo}:environment:demo"; audiences = @("api://AzureADTokenExchange") } |
            ConvertTo-Json | Set-Content $cred -Encoding ascii
        $have = & $az ad app federated-credential list --id $appId --query "[?name=='github-demo'] | length(@)" -o tsv
        if ($have -ne "1") { Invoke-Az ad app federated-credential create --id $appId --parameters "@$cred" -o none }
        # Allowed to manage this one VM only (run-command and reading its state).
        $vmId = Invoke-Az vm show -g $ResourceGroup -n $vm --query id -o tsv
        Invoke-Az role assignment create --assignee-object-id $spId --assignee-principal-type ServicePrincipal --role "Virtual Machine Contributor" --scope $vmId -o none
        $sub = Invoke-Az account show --query id -o tsv
        $tenant = Invoke-Az account show --query tenantId -o tsv
        foreach ($s in @(@("AZURE_CLIENT_ID", $appId), @("AZURE_TENANT_ID", $tenant), @("AZURE_SUBSCRIPTION_ID", $sub))) {
            & gh secret set $s[0] --repo $Repo --body $s[1]; if ($LASTEXITCODE -ne 0) { throw "gh secret set $($s[0]) failed" }
        }
        Write-Host "Done. The next push to main that passes CI is deployed to https://$fqdn (while the VM is running)."
    }
    "destroy" { Invoke-Az group delete -n $ResourceGroup --yes }
    default { Get-Content $PSCommandPath | Select-Object -Skip 1 -First 13 }
}
