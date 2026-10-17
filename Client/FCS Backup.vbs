Set WshShell = CreateObject("WScript.Shell")
Set FileSystem = CreateObject("Scripting.FileSystemObject")
AppFolder = FileSystem.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = AppFolder
WshShell.Run "pythonw """ & AppFolder & "\client.py""", 0, False
