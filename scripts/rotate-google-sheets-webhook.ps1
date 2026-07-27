param(
    [string]$Profile = $env:AWS_PROFILE,
    [string]$Region = "us-east-1",
    [string]$FunctionName = "f4f-lead-handler"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Profile)) {
    throw "Provide -Profile or set AWS_PROFILE before running this script."
}

function Remove-SensitiveTempFile {
    param(
        [Parameter(Mandatory)]
        [string]$LiteralPath
    )

    if (-not (Test-Path -LiteralPath $LiteralPath)) {
        return
    }

    for ($attempt = 1; $attempt -le 10; $attempt++) {
        try {
            Remove-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
            return
        }
        catch {
            if ($attempt -eq 10) {
                throw "Unable to remove the restricted temporary environment file after 10 attempts."
            }
            Start-Sleep -Milliseconds (100 * $attempt)
        }
    }
}

$secureWebhook = Read-Host "Paste the new Google Apps Script web-app URL" -AsSecureString
$webhookPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureWebhook)

try {
    $webhookUrl = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($webhookPointer)
    if ($webhookUrl -notmatch '^https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec$') {
        throw "The value is not a Google Apps Script web-app execution URL."
    }

    $configuration = aws lambda get-function-configuration `
        --profile $Profile `
        --region $Region `
        --function-name $FunctionName `
        --output json | ConvertFrom-Json

    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read the current Lambda configuration."
    }

    $variables = [ordered]@{}
    foreach ($property in $configuration.Environment.Variables.PSObject.Properties) {
        $variables[$property.Name] = $property.Value
    }
    $variables["GOOGLE_SHEETS_WEBHOOK_URL"] = $webhookUrl

    $environmentFile = Join-Path ([System.IO.Path]::GetTempPath()) `
        ("f4f-lambda-environment-" + [guid]::NewGuid().ToString("N") + ".json")
    $environmentJson = @{ Variables = $variables } | ConvertTo-Json -Depth 8 -Compress
    [System.IO.File]::WriteAllText(
        $environmentFile,
        $environmentJson,
        [System.Text.UTF8Encoding]::new($false)
    )

    try {
        aws lambda update-function-configuration `
            --profile $Profile `
            --region $Region `
            --function-name $FunctionName `
            --environment ("file://" + $environmentFile) `
            --query '{FunctionName:FunctionName,LastModified:LastModified,State:State,LastUpdateStatus:LastUpdateStatus}' `
            --output json

        if ($LASTEXITCODE -ne 0) {
            throw "Lambda rejected the environment update."
        }

        aws lambda wait function-updated `
            --profile $Profile `
            --region $Region `
            --function-name $FunctionName

        if ($LASTEXITCODE -ne 0) {
            throw "Lambda did not reach the updated state."
        }
    }
    finally {
        Remove-SensitiveTempFile -LiteralPath $environmentFile
    }

    Write-Output "Webhook environment variable updated without printing its value."
}
finally {
    if ($webhookPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($webhookPointer)
    }
    Remove-Variable webhookUrl -ErrorAction SilentlyContinue
    Remove-Variable secureWebhook -ErrorAction SilentlyContinue
}
