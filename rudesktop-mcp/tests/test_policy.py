import pytest

from rudesktop_mcp.policy import Mode, is_read_only, needs_confirmation

READ_ONLY = [
    "hostname; whoami; Get-Date",
    "Get-ChildItem C:\\Scimark | Select-Object -First 5 Name, Length",
    "Get-Content C:\\logs\\app.log -Tail 200 | Select-String 'ERROR'",
    "Get-Service | ? Status -eq 'Running' | sort Name",
    "gci -Recurse -Filter *.log 2>$null | measure",
    "cd C:\\Windows; dir",
    "$x = Get-Process | Sort-Object CPU -Descending; $x | Select -First 10",
    "Get-EventLog -LogName Application -Newest 50 | ft -AutoSize",
    "Test-NetConnection 10.0.0.5 -Port 5432",
    "ipconfig /all",
    "netstat -ano | findstr 8080",
    "Write-Output 'Remove-Item C:\\ dont care, это строка'",
    "Get-ChildItem | % { $_.Name }",
    "tasklist /fi \"imagename eq java.exe\"",
]

WRITES = [
    "Remove-Item C:\\temp\\x.txt",
    "Get-ChildItem C:\\temp | Remove-Item",
    "Get-ChildItem | % { Remove-Item $_ }",
    "Stop-Service MSSQLSERVER",
    "Restart-Computer -Force",
    "Get-Process java | Stop-Process",
    "Set-Content C:\\a.txt 'x'",
    "Get-Date > C:\\a.txt",
    "iex (Get-Content C:\\script.ps1 -Raw)",
    "Invoke-Expression 'Get-Date'",
    "powershell -Command Get-Date",
    "(Get-Item C:\\a.txt).Delete()",
    "[IO.File]::Delete('C:\\a.txt')",
    "sc.exe stop MSSQLSERVER",
    "net stop MSSQLSERVER",
    "shutdown /r /t 0",
    "del C:\\a.txt",
    "rm -r C:\\temp",
    "taskkill /f /im java.exe",
    "Start-Process notepad",
    "start notepad",
    "wmic process where name='java.exe' call terminate",
    "Write-Output \"$(Remove-Item C:\\a.txt)\"",
    "C:\\tools\\something.exe --do-stuff",
    "java -jar app.jar",
    "New-Item C:\\temp\\x -ItemType Directory",
    "Copy-Item a b",
    "route add 10.0.0.0 mask 255.0.0.0 10.0.0.1",
]


@pytest.mark.parametrize("cmd", READ_ONLY)
def test_read_only(cmd):
    assert is_read_only(cmd), cmd


@pytest.mark.parametrize("cmd", WRITES)
def test_writes(cmd):
    assert not is_read_only(cmd), cmd


def test_modes():
    assert needs_confirmation(Mode.CONFIRM, "hostname")
    assert not needs_confirmation(Mode.SMART, "hostname")
    assert needs_confirmation(Mode.SMART, "Remove-Item x")
    assert not needs_confirmation(Mode.AUTO, "Remove-Item x")
