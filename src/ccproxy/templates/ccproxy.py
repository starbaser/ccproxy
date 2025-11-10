import sys
from pathlib import Path

from ccproxy.handler import CCProxyHandler

_config_dir = Path(__file__).parent.resolve()
if str(_config_dir) not in sys.path:
    sys.path.insert(0, str(_config_dir))

# Create the instance that LiteLLM will use
handler = CCProxyHandler()
