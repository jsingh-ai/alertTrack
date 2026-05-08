param(
    [string]$PostgresHost = "localhost",
    [int]$PostgresPort = 5432,
    [string]$AdminUser = "postgres",
    [string]$DatabaseName = "andon_db",
    [string]$AppUser = "andon_user",
    [string]$AppPassword = "andon_password",
    [switch]$SkipSeed
)

$ErrorActionPreference = "Stop"

function Assert-SafeIdentifier {
    param([string]$Value, [string]$Name)
    if ($Value -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
        throw "$Name must use only letters, numbers, and underscores, and must not start with a number."
    }
}

function Escape-SqlLiteral {
    param([string]$Value)
    return $Value.Replace("'", "''")
}

function Invoke-Psql {
    param([string]$Database, [string]$Sql)
    psql -h $PostgresHost -p $PostgresPort -U $AdminUser -d $Database -v ON_ERROR_STOP=1 -c $Sql
    if ($LASTEXITCODE -ne 0) {
        throw "psql command failed."
    }
}

function Escape-DotEnvValue {
    param([string]$Value)
    return $Value.Replace("\", "\\").Replace('"', '\"')
}

function Get-DotEnvValue {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path $Path)) {
        return $null
    }

    foreach ($line in Get-Content $Path) {
        if ($line -match "^\s*$([regex]::Escape($Key))\s*=\s*(.*)\s*$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }

    return $null
}

function Set-DotEnvValue {
    param([string]$Path, [string]$Key, [string]$Value)
    $lineValue = "$Key=`"$(Escape-DotEnvValue $Value)`""

    if (Test-Path $Path) {
        $lines = Get-Content $Path
    } else {
        $lines = @()
    }

    $updated = $false
    $newLines = @(foreach ($line in $lines) {
        if ($line -match "^\s*$([regex]::Escape($Key))\s*=") {
            $updated = $true
            $lineValue
        } else {
            $line
        }
    })

    if (-not $updated) {
        $newLines += $lineValue
    }

    Set-Content -Path $Path -Value $newLines
}

Assert-SafeIdentifier -Value $DatabaseName -Name "DatabaseName"
Assert-SafeIdentifier -Value $AppUser -Name "AppUser"

if (-not (Get-Command psql -ErrorAction SilentlyContinue)) {
    throw "psql was not found. Add PostgreSQL bin folder to PATH, for example C:\Program Files\PostgreSQL\16\bin."
}

$escapedPassword = Escape-SqlLiteral $AppPassword
$roleSql = "DO `$$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$AppUser') THEN CREATE ROLE $AppUser LOGIN PASSWORD '$escapedPassword'; ELSE ALTER ROLE $AppUser WITH LOGIN PASSWORD '$escapedPassword'; END IF; END `$$;"
Invoke-Psql -Database "postgres" -Sql $roleSql

$dbExistsOutput = psql -h $PostgresHost -p $PostgresPort -U $AdminUser -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$DatabaseName';"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to check whether database exists."
}
$dbExists = ($dbExistsOutput | Out-String).Trim()

if (-not $dbExists) {
    Invoke-Psql -Database "postgres" -Sql "CREATE DATABASE $DatabaseName OWNER $AppUser;"
} else {
    Invoke-Psql -Database "postgres" -Sql "ALTER DATABASE $DatabaseName OWNER TO $AppUser;"
}

Invoke-Psql -Database "postgres" -Sql "GRANT ALL PRIVILEGES ON DATABASE $DatabaseName TO $AppUser;"
Invoke-Psql -Database $DatabaseName -Sql "ALTER SCHEMA public OWNER TO $AppUser; GRANT ALL ON SCHEMA public TO $AppUser;"

$encodedPassword = [uri]::EscapeDataString($AppPassword)
$env:DATABASE_URL = "postgresql+psycopg://${AppUser}:${encodedPassword}@${PostgresHost}:${PostgresPort}/${DatabaseName}"
$env:HOST = "0.0.0.0"
$env:PORT = "5001"
$env:SOCKETIO_ENABLED = "true"

$envPath = Join-Path (Get-Location) ".env"
$existingSecret = Get-DotEnvValue -Path $envPath -Key "SECRET_KEY"
if (-not $existingSecret -or $existingSecret -eq "change-this-secret" -or $existingSecret -eq "dev-andon-secret-key") {
    $existingSecret = "$([guid]::NewGuid().ToString("N"))$([guid]::NewGuid().ToString("N"))"
}

$env:SECRET_KEY = $existingSecret
Set-DotEnvValue -Path $envPath -Key "DATABASE_URL" -Value $env:DATABASE_URL
Set-DotEnvValue -Path $envPath -Key "SECRET_KEY" -Value $env:SECRET_KEY
Set-DotEnvValue -Path $envPath -Key "SOCKETIO_ENABLED" -Value $env:SOCKETIO_ENABLED
Set-DotEnvValue -Path $envPath -Key "HOST" -Value $env:HOST
Set-DotEnvValue -Path $envPath -Key "PORT" -Value $env:PORT

Write-Host "Using DATABASE_URL=$env:DATABASE_URL"
Write-Host "Wrote runtime settings to $envPath"
python scripts/init_andon_db.py

if (-not $SkipSeed) {
    python scripts/seed_andon_data.py
}

Write-Host ""
Write-Host "PostgreSQL setup complete."
Write-Host "Future app runs will read .env automatically."
Write-Host "Start the app with:"
Write-Host "python run_socketio.py"
