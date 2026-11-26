# Запуск веб-интерфейса в фоне на 127.0.0.1:8010 (лог: data\runtime\server.log)
$root = Split-Path -Parent $PSScriptRoot
$py = "C:\Users\polyakova_av\venvs\kuznitsa\Scripts\python.exe"
New-Item -ItemType Directory -Force -Path (Join-Path $root "data\runtime") | Out-Null
$busy = Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction SilentlyContinue
if ($busy) { Write-Output "Порт 8010 уже занят (PID $($busy.OwningProcess)). Сначала scripts\ui_stop.ps1"; exit 1 }
$p = Start-Process -FilePath $py -WorkingDirectory $root -WindowStyle Hidden -PassThru `
  -ArgumentList "-m", "uvicorn", "app.main:app_factory", "--factory", "--host", "127.0.0.1", "--port", "8010" `
  -RedirectStandardOutput (Join-Path $root "data\runtime\server.out.log") `
  -RedirectStandardError (Join-Path $root "data\runtime\server.log")
Set-Content -Path (Join-Path $root "data\runtime\server.pid") -Value $p.Id
Write-Output "Запущено, PID $($p.Id): http://127.0.0.1:8010"
