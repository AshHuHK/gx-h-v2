import sys,subprocess
from pathlib import Path
work=Path(__file__).resolve().parent
stage=work/'release'
sys.path.insert(0,str(stage))
from acceptance.closure.profile import test_environment
env=test_environment(work/'inner-env',python=Path(sys.executable))
sys.exit(subprocess.run(sys.argv[1:],cwd=stage,env=env).returncode)
