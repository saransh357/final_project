"""
Dependency Installer for Double Pendulum Chaos Key Generator
=============================================================
Run this script FIRST before running pendulum_key_gen.py

Usage:
    python install_dependencies.py
"""

import subprocess
import sys
import os
import platform

# ─── Packages to install ─────────────────────────────────────────────────────

PACKAGES = [
    ("opencv-python",   "cv2",          "Webcam & optical flow tracking"),
    ("numpy",           "numpy",        "Numerical processing"),
    ("pyaudio",         "pyaudio",      "Microphone audio capture"),
    ("cryptography",    "cryptography", "HKDF key derivation"),
]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def banner():
    print("=" * 60)
    print("  Pendulum Key Gen — Dependency Installer")
    print("=" * 60)
    print(f"  Python  : {sys.version.split()[0]}")
    print(f"  Platform: {platform.system()} {platform.machine()}")
    print(f"  pip     : {get_pip_version()}")
    print("=" * 60 + "\n")

def get_pip_version():
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "--version"],
                           capture_output=True, text=True)
        return r.stdout.split()[1]
    except Exception:
        return "unknown"

def check_python_version():
    if sys.version_info < (3, 7):
        print("[ERROR] Python 3.7+ is required.")
        sys.exit(1)

def is_installed(import_name: str) -> bool:
    try:
        __import__(import_name)
        return True
    except ImportError:
        return False

def upgrade_pip():
    print("[*] Upgrading pip to latest version...")
    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"],
                   check=True)
    print("[✓] pip upgraded.\n")

def install_package(package_name: str):
    print(f"    Installing {package_name}...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", package_name],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"    [FAILED]\n{result.stderr.strip()}")
        return False
    print(f"    [✓] Done.")
    return True

def install_pyaudio_platform():
    """
    PyAudio needs special handling on some platforms because it
    depends on PortAudio (a C library).
    """
    system = platform.system()
    print(f"\n[*] Detected OS: {system}")

    if system == "Linux":
        print("[*] Installing PortAudio system library first...")
        # Try apt (Debian/Ubuntu)
        apt_result = subprocess.run(
            ["sudo", "apt-get", "install", "-y", "portaudio19-dev", "python3-pyaudio"],
            capture_output=True, text=True
        )
        if apt_result.returncode != 0:
            # Try dnf (Fedora/RHEL)
            print("[*] apt failed, trying dnf...")
            subprocess.run(
                ["sudo", "dnf", "install", "-y", "portaudio-devel"],
                capture_output=True, text=True
            )

    elif system == "Darwin":  # macOS
        print("[*] Installing PortAudio via Homebrew...")
        brew_result = subprocess.run(
            ["brew", "install", "portaudio"],
            capture_output=True, text=True
        )
        if brew_result.returncode != 0:
            print("    [!] Homebrew not found or brew install failed.")
            print("    Install Homebrew: https://brew.sh then re-run this script.")

    elif system == "Windows":
        print("[*] On Windows, using pipwin for pre-built PyAudio wheel...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pipwin"],
                       check=False)
        result = subprocess.run(
            [sys.executable, "-m", "pipwin", "install", "pyaudio"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print("    [✓] PyAudio installed via pipwin.")
            return True
        print("    [!] pipwin failed, trying direct pip...")

    # Finally, try plain pip regardless
    return install_package("pyaudio")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    banner()
    check_python_version()
    upgrade_pip()

    print("[*] Checking and installing required packages:\n")

    failed = []

    for pkg_name, import_name, description in PACKAGES:
        print(f"  [{description}]")

        if is_installed(import_name):
            print(f"    [✓] {pkg_name} already installed, skipping.\n")
            continue

        if pkg_name == "pyaudio":
            success = install_pyaudio_platform()
        else:
            success = install_package(pkg_name)

        print()
        if not success:
            failed.append(pkg_name)

    # ─── Final report ─────────────────────────────────────────────────────────

    print("=" * 60)
    if not failed:
        print("  [✓] All dependencies installed successfully!")
        print("\n  You can now run:")
        print("      python pendulum_key_gen.py")
    else:
        print("  [!] The following packages FAILED to install:")
        for p in failed:
            print(f"      - {p}")
        print("\n  Manual fix suggestions:")
        if "pyaudio" in failed:
            print("  PyAudio:")
            print("    Linux  : sudo apt-get install portaudio19-dev && pip install pyaudio")
            print("    macOS  : brew install portaudio && pip install pyaudio")
            print("    Windows: pip install pipwin && pipwin install pyaudio")
        print("\n  For other packages: pip install <package_name>")
    print("=" * 60)


if __name__ == "__main__":
    main()