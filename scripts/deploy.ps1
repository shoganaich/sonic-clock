<#
.SYNOPSIS
  Copies the weather clock to the Sonic Pad and installs it (Windows).

.EXAMPLE
  .\scripts\deploy.ps1 -PadHost 192.168.1.50
  .\scripts\deploy.ps1 -PadHost 192.168.1.50 -Screenshots   # also refresh docs/screenshots

  You will be asked for the Pad's SSH password (and sudo password) a few times.
  Set up an SSH key to avoid that: see README.md.
#>
param(
    [Parameter(Mandatory = $true)][string]$PadHost,
    [string]$User = "sonic",
    [switch]$Screenshots
)

$root = Split-Path -Parent $PSScriptRoot
$target = "$User@$PadHost"

Write-Host "Copying files to $target ..."
scp -r "$root\weatherclock" "${target}:/tmp/"
if ($LASTEXITCODE -ne 0) { Write-Error "Copy failed"; exit 1 }

Write-Host "Checking and installing on the Pad ..."
ssh -t $target "sed -i 's/\r$//' /tmp/weatherclock/*.sh /tmp/weatherclock/*.py /tmp/weatherclock/*.service && python3 -m py_compile /tmp/weatherclock/weatherclock.py && sudo bash /tmp/weatherclock/install.sh"
if ($LASTEXITCODE -ne 0) { Write-Error "Install failed"; exit 1 }

if ($Screenshots) {
    Write-Host "Rendering screenshots on the Pad ..."
    ssh $target "rm -rf /tmp/wcui && python3 /tmp/weatherclock/weatherclock.py --preview-ui /tmp/wcui"
    if ($LASTEXITCODE -ne 0) { Write-Error "Screenshots failed"; exit 1 }
    scp "${target}:/tmp/wcui/*.png" "$root\docs\screenshots\"
}

Write-Host "Done."
