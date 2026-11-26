# Остановка веб-интерфейса, который слушает 127.0.0.1:8010
$conns = Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction SilentlyContinue
if (-not $conns) { Write-Output "На порту 8010 ничего не запущено"; exit 0 }
foreach ($c in $conns) { Stop-Process -Id $c.OwningProcess -Force; Write-Output "Остановлен PID $($c.OwningProcess)" }
