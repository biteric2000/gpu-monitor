@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and $_.CommandLine -match '\\.venv\\Scripts\\python.*(main\.py|uvicorn.*main:app)' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Output ('stopped server PID ' + $_.ProcessId) }; Write-Output 'done'"
