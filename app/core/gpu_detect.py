"""Runtime GPU inventory, used by gpu_preflight to fingerprint the
hardware and decide whether a hybrid-NVIDIA remediation is worth trying.

`lspci` is the primary source. Under snap strict confinement it may not
be on PATH (only present if the snap stages `pciutils`), so this falls
back to reading PCI vendor IDs straight out of sysfs, which
hardware-observe already grants read access to either way. Both paths
end up producing comparable, vendor-identifying strings so the hybrid
NVIDIA detection in gpu_preflight works regardless of which one fired.
"""
from __future__ import annotations

import os
import subprocess
from typing import List, Optional

# Well-known PCI-SIG vendor IDs (not something that can be discovered any
# other way; this is a stable industry identifier, not an app-specific
# hardcoded path or index).
_PCI_VENDOR_NAMES = {
    "0x10de": "NVIDIA",
    "0x8086": "Intel",
    "0x1002": "AMD",
    "0x1022": "AMD",
}


def _lspci_gpu_descriptions() -> Optional[List[str]]:
    try:
        out = subprocess.run(
            ["lspci"], capture_output=True, text=True, timeout=3, check=True,
        ).stdout
    except Exception:
        return None
    lines = [
        line.split(": ", 1)[-1].strip()
        for line in out.splitlines()
        if "VGA compatible controller" in line or "3D controller" in line
    ]
    return sorted(lines) if lines else None


def _read_pci_vendor(card_device_path: str) -> Optional[str]:
    try:
        with open(os.path.join(card_device_path, "vendor")) as f:
            return f.read().strip().lower()
    except Exception:
        return None


def _sysfs_gpu_descriptions() -> List[str]:
    base = "/sys/class/drm"
    try:
        cards = sorted(e for e in os.listdir(base) if e.startswith("card") and "-" not in e)
    except Exception:
        return []

    result = []
    for card in cards:
        vendor_id = _read_pci_vendor(os.path.join(base, card, "device"))
        name = _PCI_VENDOR_NAMES.get(vendor_id, vendor_id or "unknown")
        result.append(f"{card} ({name})")
    return result


def detect_gpu_descriptions() -> List[str]:
    """Sorted, human-readable GPU descriptions - no hardcoded device
    paths or indices. Used both to decide whether a multi-GPU (hybrid)
    remediation is worth trying, and as a hardware fingerprint to
    invalidate gpu_preflight's cache when the GPU configuration changes.
    """
    return _lspci_gpu_descriptions() or _sysfs_gpu_descriptions()
