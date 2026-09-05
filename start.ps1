# NTU Exchange Planner - local development launcher.
# First run bootstraps the venv and node_modules; later runs just start both servers.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Assert-RequiredProjectFiles {
  $required = @(
    "coursefinder.db",
    "backend/src/api/chat.py",
    "backend/requirements.txt",
    "frontend/package.json",
    "frontend/package-lock.json"
  )
  $missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_) })
  if ($missing.Count -gt 0) {
    Write-Host "Required project files are missing:" -ForegroundColor Red
    foreach ($path in $missing) { Write-Host "  - $path" -ForegroundColor Red }
    Write-Host "Copy the complete project folder, including coursefinder.db, and rerun start.bat." -ForegroundColor Yellow
    exit 1
  }
}

function Add-CommonToolPaths {
  $candidateDirs = @()
  if ($env:ProgramFiles) {
    $candidateDirs += (Join-Path $env:ProgramFiles "nodejs")
    $candidateDirs += (Join-Path $env:ProgramFiles "Python313")
    $candidateDirs += (Join-Path $env:ProgramFiles "Python314")
    $pythonRoot = Join-Path $env:ProgramFiles "Python"
    if (Test-Path -LiteralPath $pythonRoot -PathType Container) {
      $candidateDirs += Get-ChildItem -LiteralPath $pythonRoot -Directory -Filter "Python3*" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
    }
  }
  if ($env:LOCALAPPDATA) {
    $candidateDirs += (Join-Path $env:LOCALAPPDATA "Programs/nodejs")
    $pythonRoot = Join-Path $env:LOCALAPPDATA "Programs/Python"
    if (Test-Path -LiteralPath $pythonRoot -PathType Container) {
      $candidateDirs += Get-ChildItem -LiteralPath $pythonRoot -Directory -Filter "Python3*" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
    }
  }
  if ($env:APPDATA) {
    $candidateDirs += (Join-Path $env:APPDATA "npm")
  }
  foreach ($candidateDir in ($candidateDirs | Where-Object { $_ } | Select-Object -Unique)) {
    if ((Test-Path -LiteralPath $candidateDir -PathType Container) -and
        -not (($env:Path -split ';') -contains $candidateDir)) {
      $env:Path = "$candidateDir;$env:Path"
    }
  }
}

function Get-CommandSource([string[]]$names) {
  foreach ($name in $names) {
    $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command -and $command.Source -and (Test-Path -LiteralPath $command.Source)) {
      return $command.Source
    }
  }
  return $null
}

function Test-NodeRuntime([string]$path) {
  try {
    $versionText = (& $path --version 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $versionText -notmatch '^v(\d+)') {
      return $false
    }
    return ([int]$Matches[1] -ge 20)
  } catch {
    return $false
  }
}

function Test-NpmRuntime([string]$path) {
  try {
    & $path --version 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function Find-PythonRuntime {
  if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    foreach ($candidate in @("3.12", "3.13", "3.14")) {
      # Some Windows installations include py.exe but do not register the
      # user's Python install with it. Treat that as a failed probe and keep
      # looking for python.exe/python3.exe on PATH.
      try {
        & py.exe "-$candidate" --version 2>$null | Out-Null
        $candidateAvailable = ($LASTEXITCODE -eq 0)
      } catch {
        $candidateAvailable = $false
      }
      if ($candidateAvailable) {
        return @{ Mode = "launcher"; Version = $candidate; Command = $null }
      }
    }
  }
  foreach ($commandName in @("python.exe", "python3.exe")) {
    $pythonCommandInfo = Get-Command $commandName -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommandInfo) {
      try {
        & $pythonCommandInfo.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
          return @{ Mode = "command"; Version = $null; Command = $pythonCommandInfo.Source }
        }
      } catch {
        # Try the next command name or let the caller offer installation.
      }
    }
  }
  return $null
}

function Install-WingetPackage([string]$id, [string]$label) {
  $winget = Get-Command winget.exe -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($null -eq $winget) {
    return $false
  }
  Write-Host "Installing $label with Windows Package Manager..." -ForegroundColor Cyan
  try {
    & $winget.Source install --id $id --exact --source winget `
      --accept-source-agreements --accept-package-agreements --silent 2>&1 | Out-Host
    return ($LASTEXITCODE -eq 0)
  } catch {
    Write-Host "Automatic installation of $label failed: $($_.Exception.Message)" -ForegroundColor Yellow
    return $false
  }
}

Assert-RequiredProjectFiles

# Refresh PATH so installations made by winget, or standard per-user installs,
# are visible in this PowerShell process immediately.
Add-CommonToolPaths

$nodePath = Get-CommandSource @("node.exe")
$npmPath = Get-CommandSource @("npm.cmd", "npm.exe")
if ($null -eq $nodePath -or $null -eq $npmPath -or
    -not (Test-NodeRuntime $nodePath) -or -not (Test-NpmRuntime $npmPath)) {
  if (Install-WingetPackage "OpenJS.NodeJS.LTS" "Node.js LTS") {
    Add-CommonToolPaths
    $nodePath = Get-CommandSource @("node.exe")
    $npmPath = Get-CommandSource @("npm.cmd", "npm.exe")
  }
}
if ($null -eq $nodePath -or $null -eq $npmPath -or
    -not (Test-NodeRuntime $nodePath) -or -not (Test-NpmRuntime $npmPath)) {
  Write-Host "Node.js 20+ and npm are required, but were not found or are not working." -ForegroundColor Red
  Write-Host "Install Node.js LTS from https://nodejs.org/ and rerun start.bat." -ForegroundColor Yellow
  exit 1
}

$venvPython = Join-Path $root "backend/.venv/Scripts/python.exe"

if (-not (Test-Path "backend/.env")) {
  Copy-Item "backend/.env.example" "backend/.env"
  Write-Host "Created backend/.env - add your GROQ_API_KEY before chatting." -ForegroundColor Yellow
}

if (-not (Test-Path $venvPython)) {
  Write-Host "Creating backend virtual environment..." -ForegroundColor Cyan

  $pythonRuntime = Find-PythonRuntime
  if ($null -eq $pythonRuntime -and (Install-WingetPackage "Python.Python.3.13" "Python 3.13")) {
    Add-CommonToolPaths
    $pythonRuntime = Find-PythonRuntime
  }
  if ($null -eq $pythonRuntime) {
    Write-Host "No suitable Python runtime found. Install Python 3.12 or newer and rerun start.bat." -ForegroundColor Red
    exit 1
  }

  if ($pythonRuntime.Mode -eq "launcher") {
    & py.exe "-$($pythonRuntime.Version)" -m venv "backend/.venv"
  } else {
    & $pythonRuntime.Command -m venv "backend/.venv"
  }
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Could not create the backend virtual environment." -ForegroundColor Red
    exit 1
  }
  & $venvPython -m pip install --quiet --upgrade pip
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Could not upgrade pip. Check your internet connection and rerun start.bat." -ForegroundColor Red
    exit 1
  }
  & $venvPython -m pip install -r "backend/requirements-dev.txt"
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Backend dependency installation failed. Check your internet connection and rerun start.bat." -ForegroundColor Red
    exit 1
  }
}

# The NTU-student RAG lane needs ChromaDB at runtime. pypdf remains an offline
# ingestion-only dependency. Existing checkouts already have a venv, so the
# first-run branch above would otherwise never install newly added dependencies.
& $venvPython -c "import chromadb, fastapi, langgraph, langchain_groq" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Installing backend vector retrieval dependency..." -ForegroundColor Cyan
  & $venvPython -m pip install -r "backend/requirements-dev.txt"
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Backend dependency installation failed. Check your internet connection and rerun start.bat." -ForegroundColor Red
    exit 1
  }
}

if (-not (Test-Path "frontend/node_modules/.bin/next.cmd")) {
  Write-Host "Installing frontend dependencies..." -ForegroundColor Cyan
  Push-Location "frontend"
  npm.cmd install
  $npmExitCode = $LASTEXITCODE
  Pop-Location
  if ($npmExitCode -ne 0) {
    Write-Host "Frontend dependency installation failed. Check your internet connection and rerun start.bat." -ForegroundColor Red
    exit 1
  }
}

# The research lane uses the bundled OpenSERP sidecar. Start it before the API
# so the first weather or other web-research query does not get a misleading
# "no web search provider" response. Reuse it when it is already healthy.
$searchUrl = "http://127.0.0.1:7000/duckduckgo/search?text=ping&limit=1"
$searchReady = $false
try {
  $probe = Invoke-WebRequest -UseBasicParsing -Uri $searchUrl -TimeoutSec 3
  $searchReady = ($probe.StatusCode -ge 200 -and $probe.StatusCode -lt 500)
} catch {
  $searchReady = $false
}

if (-not $searchReady) {
  $searchExe = Join-Path $root "tools/openserp/openserp.exe"
  if (Test-Path $searchExe) {
    Write-Host "Starting bundled web-search sidecar on http://127.0.0.1:7000..." -ForegroundColor Cyan
    try {
      Start-Process -FilePath $searchExe `
        -ArgumentList "serve", "-a", "127.0.0.1", "-p", "7000" `
        -WorkingDirectory (Join-Path $root "tools/openserp") `
        -WindowStyle Hidden
    } catch {
      Write-Host "Could not start the bundled web-search sidecar: $($_.Exception.Message)" -ForegroundColor Yellow
    }

    for ($i = 0; $i -lt 30 -and -not $searchReady; $i++) {
      Start-Sleep -Milliseconds 500
      try {
        $probe = Invoke-WebRequest -UseBasicParsing -Uri $searchUrl -TimeoutSec 2
        $searchReady = ($probe.StatusCode -ge 200 -and $probe.StatusCode -lt 500)
      } catch {
        $searchReady = $false
      }
    }
  } else {
    Write-Host "Bundled web-search sidecar not found; research queries will be unavailable." -ForegroundColor Yellow
  }
}

if ($searchReady) {
  Write-Host "Web search ready on http://127.0.0.1:7000." -ForegroundColor Green
} else {
  Write-Host "Web search is not available. Install/start OpenSERP or set SEARCH_PROVIDER=null." -ForegroundColor Yellow
}

Write-Host "Starting API on http://127.0.0.1:8000 and UI on http://localhost:3000" -ForegroundColor Green

$existingApi = $null
try {
  $existingApi = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 2
} catch {
  $existingApi = $null
}
if ($null -ne $existingApi) {
  if ($null -eq $existingApi.general_questions_chroma_chunks) {
    Write-Host "The API on :8000 is an older project process. Restarting that exact API process..." -ForegroundColor Yellow
    try {
      $staleApi = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Stop |
        Select-Object -First 1 -ExpandProperty OwningProcess
      if ($staleApi) {
        Stop-Process -Id $staleApi -Force -ErrorAction Stop
        Start-Sleep -Milliseconds 500
      }
      $existingApi = $null
    } catch {
      Write-Host "Could not restart the old API automatically. Close the process using port 8000 and rerun start.ps1." -ForegroundColor Red
      exit 1
    }
  }
  if ($null -ne $existingApi.general_questions_chroma_chunks) {
    Write-Host ("Reusing the current API (Chroma chunks: {0})." -f $existingApi.general_questions_chroma_chunks) -ForegroundColor Green
  }
}
if ($null -eq $existingApi) {
  Start-Process -FilePath $venvPython `
    -ArgumentList "-m", "uvicorn", "api.chat:app", "--app-dir", "src", "--port", "8000", "--reload" `
    -WorkingDirectory (Join-Path $root "backend")
}

$apiReady = ($null -ne $existingApi)
Write-Host "Waiting for the API to become healthy..." -NoNewline
for ($i = 0; $i -lt 60 -and -not $apiReady; $i++) {
  Start-Sleep -Seconds 1
  try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 2
    if ($r.status -ne "ok" -or -not $r.available) {
      Write-Host ""
      Write-Host "The API responded, but coursefinder.db is unavailable." -ForegroundColor Red
      Write-Host "Copy coursefinder.db into the project root and rerun start.bat." -ForegroundColor Yellow
      exit 1
    }
    $apiReady = $true
    Write-Host ""
    Write-Host ("API ready: status={0} universities={1} mappings={2} llm_configured={3}" -f `
      $r.status, $r.universities, $r.mappings, $r.llm_configured) -ForegroundColor Green
    if (-not $r.llm_configured) {
      Write-Host "LLM credentials are not configured; deterministic features still work, but add GROQ_API_KEY for full AI responses." -ForegroundColor Yellow
    }
  } catch {
    Write-Host "." -NoNewline
  }
}
if (-not $apiReady) {
  Write-Host ""
  Write-Host "The API did not become ready. The UI was not started." -ForegroundColor Red
  Write-Host "Check the API window for the exact error, then rerun start.bat." -ForegroundColor Yellow
  exit 1
}

$existingUi = $false
try {
  $uiProbe = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:3000" -TimeoutSec 2
  $existingUi = ($uiProbe.StatusCode -ge 200 -and $uiProbe.StatusCode -lt 500)
} catch {
  $existingUi = $false
}
if ($existingUi) {
  Write-Host "Reusing the current UI on http://localhost:3000." -ForegroundColor Green
} else {
  Start-Process -FilePath "npm.cmd" -ArgumentList "run", "dev" `
    -WorkingDirectory (Join-Path $root "frontend")
}

$uiReady = $false
Write-Host "Waiting for the UI to become healthy..." -NoNewline
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Seconds 1
  try {
    $uiProbe = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:3000" -TimeoutSec 2
    if ($uiProbe.StatusCode -ge 200 -and $uiProbe.StatusCode -lt 500) {
      $uiReady = $true
      Write-Host ""
      Write-Host "UI ready: http://localhost:3000" -ForegroundColor Green
      break
    }
  } catch {
    Write-Host "." -NoNewline
  }
}
if (-not $uiReady) {
  Write-Host ""
  Write-Host "The UI did not become ready. Check the frontend window for the exact error." -ForegroundColor Red
  exit 1
}

try {
  Start-Process "http://localhost:3000"
} catch {
  Write-Host "The project is running, but Windows could not open the browser automatically." -ForegroundColor Yellow
  Write-Host "Open http://localhost:3000 manually." -ForegroundColor Yellow
}
