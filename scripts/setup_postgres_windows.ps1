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

Write-Host "Using DATABASE_URL=$env:DATABASE_URL"
python scripts/init_andon_db.py

if (-not $SkipSeed) {
    python scripts/seed_andon_data.py
}

Write-Host ""
Write-Host "PostgreSQL setup complete."
Write-Host "For this PowerShell session, DATABASE_URL is already set."
Write-Host "For future sessions, run:"
Write-Host "`$env:DATABASE_URL = `"$env:DATABASE_URL`""
