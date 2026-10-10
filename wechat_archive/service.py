"""User-session autostart integration, with no elevated service or key bootstrap."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from .runner import read_config

LABEL='org.julian.wechatarchive.collector'
TASK='WeChatArchive-Collector'


def launch_agent(config,python):
    return {'Label':LABEL,'ProgramArguments':[str(python),'-m','wechat_archive','watch','--config',str(config)],
            'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':30,'ProcessType':'Background'}


def windows_task(config,python,sid):
    namespace='http://schemas.microsoft.com/windows/2004/02/mit/task';ET.register_namespace('',namespace)
    def child(parent,name,text=None):
        node=ET.SubElement(parent,'{'+namespace+'}'+name)
        if text is not None:node.text=text
        return node
    root=ET.Element('{'+namespace+'}Task',{'version':'1.2'})
    triggers=child(root,'Triggers');trigger=child(triggers,'LogonTrigger');child(trigger,'Enabled','true');child(trigger,'UserId',sid)
    principals=child(root,'Principals');principal=child(principals,'Principal');principal.set('id','User')
    child(principal,'UserId',sid);child(principal,'LogonType','InteractiveToken');child(principal,'RunLevel','LeastPrivilege')
    settings=child(root,'Settings')
    for key,value in [('MultipleInstancesPolicy','IgnoreNew'),('DisallowStartIfOnBatteries','false'),('StopIfGoingOnBatteries','false'),
                      ('StartWhenAvailable','true'),('ExecutionTimeLimit','PT0S'),('Enabled','true')]:child(settings,key,value)
    restart=child(settings,'RestartOnFailure');child(restart,'Interval','PT1M');child(restart,'Count','3')
    actions=child(root,'Actions');actions.set('Context','User');action=child(actions,'Exec')
    child(action,'Command',str(python));child(action,'Arguments',subprocess.list2cmdline(['-m','wechat_archive','watch','--config',str(config)]))
    return ET.tostring(root,encoding='utf-16',xml_declaration=True)


def _run(arguments,check=True):
    return subprocess.run(arguments,capture_output=True,timeout=30,check=check)


def install(config_path):
    config=Path(config_path).expanduser().resolve(strict=True)
    read_config(config)
    python=Path(sys.executable).absolute()
    if not python.is_file():raise ValueError('service_python_unavailable')
    if sys.platform=='darwin':
        folder=Path.home()/'Library/LaunchAgents';folder.mkdir(parents=True,exist_ok=True)
        target=folder/(LABEL+'.plist');payload=launch_agent(config,python)
        if target.is_symlink():raise ValueError('service_target_symlink')
        if target.exists():
            old=plistlib.loads(target.read_bytes())
            if old.get('Label')!=LABEL or old.get('ProgramArguments',[None,None,None])[1:3]!=['-m','wechat_archive']:
                raise ValueError('unrelated_service_target')
        fd,name=tempfile.mkstemp(prefix='.wechat-agent-',dir=folder)
        try:
            with os.fdopen(fd,'wb') as stream:plistlib.dump(payload,stream)
            os.replace(name,target)
        finally:Path(name).unlink(missing_ok=True)
        domain=f'gui/{os.getuid()}'
        _run(['/bin/launchctl','bootout',domain+'/'+LABEL],check=False)
        _run(['/bin/launchctl','bootstrap',domain,str(target)])
        _run(['/bin/launchctl','kickstart',domain+'/'+LABEL])
        return {'state':'installed','platform':'macos','user_session':True}
    if sys.platform=='win32':
        sid=_run(['powershell.exe','-NoProfile','-Command','[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value']).stdout.decode().strip()
        import re
        if not re.fullmatch(r'S-1-(?:\d+-)+\d+',sid):raise ValueError('windows_identity_unavailable')
        existing=_run(['schtasks.exe','/Query','/TN',TASK,'/XML'],check=False)
        if existing.returncode==0:
            root=ET.fromstring(existing.stdout.decode('utf-16') if existing.stdout.startswith(b'\xff\xfe') else existing.stdout.decode())
            ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            args=root.findtext('.//t:Exec/t:Arguments',namespaces=ns) or ''
            if not args.startswith('-m wechat_archive watch '):raise ValueError('unrelated_service_target')
        fd,name=tempfile.mkstemp(suffix='.xml')
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(windows_task(config,python,sid))
            _run(['schtasks.exe','/Create','/TN',TASK,'/XML',name,'/F'])
            _run(['schtasks.exe','/Run','/TN',TASK])
        finally:Path(name).unlink(missing_ok=True)
        return {'state':'installed','platform':'windows','user_session':True}
    raise ValueError('autostart_requires_macos_or_windows')


def uninstall():
    if sys.platform=='darwin':
        target=Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
        if target.is_symlink():raise ValueError('service_target_symlink')
        if not target.exists():return {'state':'not_installed','data_retained':True}
        if target.exists():
            old=plistlib.loads(target.read_bytes())
            if old.get('Label')!=LABEL or old.get('ProgramArguments',[None,None,None])[1:3]!=['-m','wechat_archive']:
                raise ValueError('unrelated_service_target')
        _run(['/bin/launchctl','bootout',f'gui/{os.getuid()}/{LABEL}'],check=False)
        target.unlink(missing_ok=True)
    elif sys.platform=='win32':
        # Reuse the ownership gate before removing a task with this name.
        result=_run(['schtasks.exe','/Query','/TN',TASK,'/XML'],check=False)
        if result.returncode==0:
            raw=result.stdout.decode('utf-16') if result.stdout.startswith(b'\xff\xfe') else result.stdout.decode()
            root=ET.fromstring(raw);ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            args=root.findtext('.//t:Exec/t:Arguments',namespaces=ns) or ''
            if not args.startswith('-m wechat_archive watch '):raise ValueError('unrelated_service_target')
            _run(['schtasks.exe','/End','/TN',TASK],check=False);_run(['schtasks.exe','/Delete','/TN',TASK,'/F'])
    else:raise ValueError('autostart_requires_macos_or_windows')
    return {'state':'uninstalled','data_retained':True}
