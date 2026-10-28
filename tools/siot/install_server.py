"""Initial SIoT ARM64 deployment. Run on the device with an explicit tar archive.

Does not run the UNIHIKER installer, reboot, install pip packages or overwrite
an existing SIoT installation. Enable boot startup after functional checks.
"""
import argparse
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import struct
import subprocess
import tarfile


def run(*args, timeout=30):
    return subprocess.run(list(args), check=True, capture_output=True, text=True, timeout=timeout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args()
    assert os.geteuid() == 0, 'Root required for service installation'
    assert os.uname().machine == 'aarch64', 'This package requires ARM64'
    assert hashlib.sha256(args.archive.read_bytes()).hexdigest() == args.sha256
    base, data = Path('/opt/siot'), Path('/var/lib/siot')
    unit = Path('/etc/systemd/system/siot.service')
    assert not any(p.exists() for p in (base, data, unit)), 'Existing deployment: abort without overwrite'
    listeners = run('ss', '-lnt').stdout
    assert not re.search(r':(?:1883|8080|1888|8883)\s', listeners), 'A requested port is already in use'
    with tarfile.open(args.archive, 'r:gz') as archive:
        members = archive.getmembers()
        assert sum(m.size for m in members) < 200 * 1024 * 1024
        selected = []
        for member in members:
            path = PurePosixPath(member.name)
            assert not path.is_absolute() and '..' not in path.parts
            assert path.parts and path.parts[0] == 'SIoT1.3'
            assert member.isfile() or member.isdir(), 'Links and special files are not accepted'
            if member.isdir():
                continue
            relative = Path(*path.parts[1:])
            if str(relative) == 'main' or relative.parts[0] in ('conf', 'httpsvr', 'data'):
                selected.append((member, relative))
        core = archive.extractfile('SIoT1.3/main').read(64)
        assert core[:4] == b'\x7fELF' and core[4] == 2 and struct.unpack_from('<H', core, 18)[0] == 183
        try:
            account = pwd.getpwnam('siot')
            assert account.pw_shell in ('/usr/sbin/nologin', '/sbin/nologin'), 'Existing interactive account'
        except KeyError:
            run('useradd', '--system', '--user-group', '--home-dir', str(data), '--no-create-home', '--shell', '/usr/sbin/nologin', 'siot')
            account = pwd.getpwnam('siot')
        base.mkdir(mode=0o755)
        data.mkdir(mode=0o700)
        os.chown(data, account.pw_uid, account.pw_gid)
        for member, relative in selected:
            target = data.joinpath(*relative.parts[1:]) if relative.parts[0] == 'data' else base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open('xb') as dest:
                shutil.copyfileobj(source, dest)
            target.chmod(0o755 if str(relative) == 'main' else 0o644)
            if relative.parts[0] == 'data':
                os.chown(target, account.pw_uid, account.pw_gid)
                target.chmod(0o600)
        (base/'data').symlink_to(data, target_is_directory=True)

    # Generate device-specific certificates instead of distributing the bundle's keys.
    ssl = base/'ssl'
    for name in ('ca','server','client'):
        p = ssl/name; p.mkdir(parents=True, mode=0o750); os.chown(p,0,account.pw_gid)
    host = os.uname().nodename
    assert re.fullmatch(r'[A-Za-z0-9.-]+',host)
    addresses = {'127.0.0.1'}
    for interface in json.loads(run('ip','-j','-4','addr','show').stdout):
        for item in interface.get('addr_info',[]):
            if item.get('family') == 'inet':addresses.add(str(ipaddress.ip_address(item['local'])))
    run('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','825','-sha256',
        '-subj','/CN=SIoT local CA','-keyout',str(ssl/'ca/ca.key'),'-out',str(ssl/'ca/ca.pem'),
        '-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign')
    for kind in ('server','client'):
        run('openssl','req','-new','-newkey','rsa:2048','-nodes','-subj','/CN='+ (host if kind=='server' else 'SIoT local client'),
            '-keyout',str(ssl/kind/'key.pem'),'-out',str(ssl/kind/'request.csr'))
        ext = ssl/kind/'extensions.cnf'
        value='basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage='+('serverAuth' if kind=='server' else 'clientAuth')+'\n'
        if kind=='server':value+='subjectAltName=DNS:'+host+',DNS:'+host+'.local,DNS:localhost,'+','.join('IP:'+a for a in sorted(addresses))+'\n'
        ext.write_text(value)
        run('openssl','x509','-req','-in',str(ssl/kind/'request.csr'),'-CA',str(ssl/'ca/ca.pem'),'-CAkey',str(ssl/'ca/ca.key'),
            '-CAcreateserial','-days','365','-sha256','-extfile',str(ext),'-out',str(ssl/kind/'cert.pem'))
    for p in ssl.rglob('*'):
        if p.is_file():
            os.chown(p,0,account.pw_gid)
            p.chmod(0o640 if p.name=='key.pem' else 0o644)
    (ssl/'ca/ca.key').chmod(0o600)
    cfg=base/'conf/config.json';settings=json.loads(cfg.read_text());settings['debug']=False
    cfg.write_text(json.dumps(settings,indent=2));os.chown(cfg,0,account.pw_gid);cfg.chmod(0o640)
    unit.write_text('''[Unit]
Description=SIoT MQTT server and web console
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=siot
Group=siot
WorkingDirectory=/opt/siot
ExecStart=/opt/siot/main -c /opt/siot/conf/config.json
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
''')
    run('systemd-analyze','verify',str(unit))
    run('systemctl','daemon-reload')
    try:
        run('systemctl','start','siot.service')
        run('systemctl','is-active','--quiet','siot.service')
    except Exception:
        subprocess.run(['systemctl','stop','siot.service'],timeout=20)
        raise
    print(json.dumps({'installed':str(base),'data':str(data),'unit':str(unit),'boot_enabled':False,
                      'binary_sha256':hashlib.sha256((base/'main').read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
