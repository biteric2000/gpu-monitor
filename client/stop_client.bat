@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and $_.CommandLine -match '\\.venv\\Scripts\\python.*agent\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Output ('stopped client PID ' + $_.ProcessId) }; Write-Output 'done'"
