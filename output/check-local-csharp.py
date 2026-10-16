import os, sys, json, hashlib
from pathlib import Path
from dataclasses import asdict
root=Path(r'C:/Users/yuanchuan/Desktop/git/low_code/write')
sys.path.insert(0,str(root/'mcp'))
from gxp_editor.config import default_config_path, RuntimeReferenceConfig
# Select only public runtime-reference settings; never load a database secret.
runtime=RuntimeReferenceConfig.from_dict(json.loads(Path(r'C:/Users/yuanchuan/AppData/Roaming/GxpLowcodeEditor/database.json').read_text(encoding='utf-8-sig'))['runtime_references'])
plan=json.loads(Path(r'C:/Users/yuanchuan/AppData/Local/GxpLowcodeEditor/plans/43406ddc9c094ce2a8768eb1aafbdb29.json').read_text(encoding='utf-8'))
os.environ['LOCALAPPDATA']=str(root/'output'/'runtime-check')
os.environ.setdefault('APPDATA',r'C:/Users/yuanchuan/AppData/Roaming')
os.environ.setdefault('USERPROFILE',r'C:/Users/yuanchuan')
os.environ['DOTNET_CLI_TELEMETRY_OPTOUT']='1'
os.environ['DOTNET_SKIP_FIRST_TIME_EXPERIENCE']='1'
from gxp_editor.runtime_validation import RuntimeCsharpValidator
validator=object.__new__(RuntimeCsharpValidator);validator.runtime=runtime
try:
 print(json.dumps({'reference_settings':asdict(runtime),'status':validator.status()},ensure_ascii=True),flush=True)
 result=validator.validate('IsnSZoB9',plan['desired']['csharp_code'])
 print(json.dumps({'candidate_result':result},ensure_ascii=True),flush=True)
except Exception as exc:
 print(json.dumps({'failure_kind':getattr(exc,'code','unknown'),'error':str(exc),'diagnostics':getattr(exc,'diagnostics',[])},ensure_ascii=True),flush=True)
 sys.exit(1)
