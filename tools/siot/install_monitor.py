"""Install the prepared native monitor and its XiaoZhi UI ownership hooks.

Stage contains apps/siot_monitor, xiaozhi/service and tested source files.
Requires the desktop to be active; never runs while a camera App owns display.
"""
import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def run(*args, timeout=45):
    return subprocess.run(list(args),check=True,capture_output=True,text=True,timeout=timeout)


def desktop_ready():
    for pid in subprocess.run(['pgrep','-x','launcher'],capture_output=True,text=True).stdout.split():
        try:
            for fd in Path('/proc',pid,'fd').iterdir():
                try:
                    if os.readlink(fd)=='/dev/dri/card0':return True
                except OSError:pass
        except OSError:pass
    return False


def main():
    p=argparse.ArgumentParser();p.add_argument('stage',type=Path);p.add_argument('--expected-service-sha256',required=True);p.add_argument('--expected-hardware-sha256',required=True);a=p.parse_args()
    assert desktop_ready(),'Return to the app desktop before installation'
    assert Path('/etc/siot/monitor.json').is_file(),'SIoT connection profile is required'
    base=Path('/root/xiaozhi');app=Path('/maixapp/apps/siot_monitor');info=Path('/maixapp/apps/app.info')
    assert not app.exists(),'Existing monitor App needs an explicit upgrade procedure'
    source=base/'xiaozhi_service.py'
    normalized=source.read_bytes().replace(b'\r\n',b'\n')
    assert hashlib.sha256(normalized).hexdigest()==a.expected_service_sha256,'Installed XiaoZhi differs from the known baseline'
    hardware=base/'hardware_tools.py'
    assert hashlib.sha256(hardware.read_bytes().replace(b'\r\n',b'\n')).hexdigest()==a.expected_hardware_sha256,'Installed hardware adapter differs from the known baseline'
    files=['main.py','model.py','numeric_chart.py','mqtt_client.py','media_session.py','media_viewer.py',
           'media_publisher.py','lan_camera_server.py','camera_preview.py','camera_worker.py','launch.py','app.yaml']
    service_files=['xiaozhi_service.py','ui_ownership.py','camera_ownership.py','hardware_tools.py','vision_siot.py']
    for n in files:
        assert (a.stage/'apps/siot_monitor'/n).is_file(),n
        if n.endswith('.py'):compile((a.stage/'apps/siot_monitor'/n).read_bytes(),n,'exec')
    for n in service_files:compile((a.stage/'xiaozhi/service'/n).read_bytes(),n,'exec')
    old_info=info.read_text();parsed=configparser.ConfigParser(interpolation=None);parsed.read_string(old_info)
    assert not parsed.has_section('siot_monitor')
    backup=Path('/root/xiaozhi_backups')/time.strftime('siot-monitor-%Y%m%d-%H%M%S');backup.mkdir(parents=True,mode=0o700)
    (backup/'app.info').write_text(old_info)
    for n in service_files:
        if (base/n).exists():shutil.copy2(base/n,backup/n)
    entry='''[siot_monitor]
name=SIoT Monitor
name[zh]=SIoT监控
version=0.1.0
icon=/opt/siot/httpsvr/static/SIoT.png
author=Local project
desc=Topic monitor and touch message editor
exec=launch.py

'''
    try:
        run('systemctl','stop','xiaozhi.service');run('systemctl','stop','launcher.service')
        app.mkdir(mode=0o755)
        for n in files:shutil.copyfile(a.stage/'apps/siot_monitor'/n,app/n)
        shutil.copyfile(a.stage/'xiaozhi/service/ui_ownership.py',app/'ui_ownership.py')
        shutil.copyfile(a.stage/'xiaozhi/service/camera_ownership.py',app/'camera_ownership.py')
        for n in service_files:
            tmp=base/(n+'.siot-new');shutil.copyfile(a.stage/'xiaozhi/service'/n,tmp);os.replace(tmp,base/n)
        at=next((old_info.find(marker) for marker in ['[xiaozhi_vision_test]','[benchmark]'] if marker in old_info),len(old_info))
        tmp=info.with_name('app.info.siot-new');tmp.write_text(old_info[:at]+entry+old_info[at:]);os.replace(tmp,info)
        run('systemctl','start','launcher.service')
        deadline=time.monotonic()+30
        while time.monotonic()<deadline and not desktop_ready():time.sleep(.2)
        assert desktop_ready(),'Desktop did not initialize DRM'
        run('systemctl','start','xiaozhi.service');time.sleep(12)
        run('systemctl','is-active','--quiet','xiaozhi.service');run('systemctl','is-active','--quiet','siot.service')
        print(json.dumps({'installed':str(app),'backup':str(backup),'persistent_settings_changed':False}))
    except Exception:
        subprocess.run(['systemctl','stop','xiaozhi.service'],timeout=35)
        subprocess.run(['systemctl','stop','launcher.service'],timeout=35)
        for n in service_files:
            if (backup/n).exists():shutil.copy2(backup/n,base/n)
        info.write_text(old_info)
        run('systemctl','start','launcher.service');time.sleep(3);run('systemctl','start','xiaozhi.service')
        raise


if __name__=='__main__':main()
