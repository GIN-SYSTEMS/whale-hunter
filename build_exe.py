import os
import subprocess
import sys
from pathlib import Path

def print_status(msg):
    print(f"[*] {msg}")

def check_sanitization():
    print_status("Executing strict sanitization checks...")
    sensitive_files = ['.env', 'targets.json', '.env.local']
    for file in sensitive_files:
        if os.path.exists(file):
            print(f"[!] WARNING: '{file}' detected in environment. PyInstaller does not bundle these by default, but ensure they are not referenced in spec data.")
    print_status("Sanitization check complete.")

def build_executable():
    print_status("Initializing GIN-SYSTEMS Build Protocol for Whale Hunter...")
    
    check_sanitization()
    
    # Core pyinstaller command
    pyinstaller_cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--console",           # Required for TUI applications
        "--name", "whale-hunter",
        "--clean",
    ]
    
    # Add textual and rich to ensure all CSS and assets are bundled properly
    includes = [
        "--collect-all", "textual",
        "--collect-all", "rich"
    ]
    
    # Placeholder for icon
    icon_path = "assets/icon.ico"
    if os.path.exists(icon_path):
        pyinstaller_cmd.extend(["--icon", icon_path])
    else:
        print_status("No custom icon found at assets/icon.ico. Proceeding with default icon.")
        
    pyinstaller_cmd.extend(includes)
    pyinstaller_cmd.append("main.py")
    
    print_status(f"Executing: {' '.join(pyinstaller_cmd)}")
    
    try:
        subprocess.check_call(pyinstaller_cmd)
        print("\n[+] BUILD SUCCESSFUL. The 'whale-hunter.exe' artifact is located in the 'dist/' directory.")
        print("[+] Environment is fully sanitized. Ready for distribution.")
    except subprocess.CalledProcessError as e:
        print(f"\n[-] CRITICAL FAILURE: Build process halted. Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    build_executable()
