
import yaml
import subprocess
from pathlib import Path

def check_auth_status():
    config_path = Path.home() / ".ccproxy" / "ccproxy.yaml"
    if not config_path.exists():
        # check current directory
        config_path = Path("ccproxy.yaml")
    
    if not config_path.exists():
        print("ccproxy.yaml not found.")
        return

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
            ccproxy = data.get("ccproxy", {})
            oat_sources = ccproxy.get("oat_sources", {})
            
            if not oat_sources:
                print("No oat_sources found in ccproxy.yaml.")
                return

            print(f"Auth Status for {config_path}:")
            for provider, source in oat_sources.items():
                command = source
                if isinstance(source, dict):
                    command = source.get("command")
                
                if not command:
                    print(f"  {provider}: No command configured.")
                    continue
                
                try:
                    result = subprocess.run(command, shell=True, capture_output=True, text=True)
                    if result.returncode == 0:
                        token = result.stdout.strip()
                        if token:
                            print(f"  {provider}: [OK] (Token: {token[:8]}...)")
                        else:
                            print(f"  {provider}: [ERROR] Command returned empty output.")
                    else:
                        print(f"  {provider}: [ERROR] Command failed with code {result.returncode}.")
                        print(f"    {result.stderr.strip()}")
                except Exception as e:
                    print(f"  {provider}: [EXCEPTION] {str(e)}")
    except Exception as e:
        print(f"Error reading config: {str(e)}")

if __name__ == "__main__":
    check_auth_status()
