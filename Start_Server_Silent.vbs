Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")

' Get the folder where this VBS file lives
strDir = objFSO.GetParentFolderName(WScript.ScriptFullName)

' Run bambu2obs.py silently (window style 0 = hidden)
' bambu2obs.py is the correct entry point - it starts the MQTT writer
' and launches progressbarServer.py automatically as a subprocess.
objShell.Run "python """ & strDir & "\src\bambu2obs.py""", 0, False
