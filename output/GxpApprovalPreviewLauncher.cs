using System;
using System.Diagnostics;
using System.Windows.Forms;
class GxpApprovalPreviewLauncher {
 [STAThread] static void Main() {
  try {
   var info = new ProcessStartInfo {
    FileName = @"C:\Users\yuanchuan\AppData\Local\GxpLowcodeEditor\.venv\Scripts\pythonw.exe",
    Arguments = @"-X utf8 ""C:\Users\yuanchuan\Desktop\git\low_code\write\scripts\preview_saved_approval.py"" 2e8242fd957c42a9b3bbad5a3b2269c7",
    WorkingDirectory = @"C:\Users\yuanchuan\Desktop\git\low_code\write",
    UseShellExecute = false, CreateNoWindow = true
   };
   Process.Start(info);
  } catch (Exception e) { MessageBox.Show(e.Message, "GXP Approval Preview"); }
 }
}
