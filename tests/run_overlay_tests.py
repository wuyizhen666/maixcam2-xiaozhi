"""Linux native overlay regressions, without loading hooks into any device app."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    if os.name != 'posix':
        raise SystemExit('Run this native regression on Linux with gcc and objcopy')
    for tool in ['gcc', 'objcopy']:
        if shutil.which(tool) is None:
            raise SystemExit('Required build tool missing: ' + tool)
    with tempfile.TemporaryDirectory(prefix='xz-overlay-build-') as directory:
        root = Path(directory)
        for name in ['overlay_reload_test.c', 'touch_filter_test.c']:
            source = Path(__file__).with_name(name)
            subprocess.run(['gcc', '-O2', '-c', str(source), '-o', str(root/'test.o')], check=True)
            # Omit only this object's constructor array; production code is unchanged.
            subprocess.run(['objcopy', '--remove-section=.init_array', str(root/'test.o')], check=True)
            subprocess.run(['gcc', str(root/'test.o'), '-ldl', '-lpthread', '-o', str(root/'test')], check=True)
            subprocess.run([str(root/'test')], check=True, timeout=5)


if __name__ == '__main__': main()
