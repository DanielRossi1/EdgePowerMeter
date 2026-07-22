"""GPU inventory detection tests.

Covers both the primary path (lspci) and the fallback that matters most
under snap strict confinement: lspci isn't guaranteed to be on PATH
there unless the snap stages pciutils, so detect_gpu_descriptions() must
still be able to recognize an NVIDIA card via sysfs PCI vendor IDs -
otherwise gpu_preflight would never even attempt the NVIDIA PRIME
offload remediation on exactly the hybrid laptops it exists for.
"""
from __future__ import annotations

import subprocess

from app.core import gpu_detect


def test_lspci_path_parses_vga_and_3d_controllers(monkeypatch):
    lspci_output = (
        "00:02.0 VGA compatible controller: Intel Corporation UHD Graphics\n"
        "01:00.0 3D controller: NVIDIA Corporation TU104M [GeForce RTX 2070 Mobile]\n"
        "00:1f.3 Audio device: Intel Corporation Comet Lake PCH cAVS\n"
    )
    monkeypatch.setattr(
        gpu_detect.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=lspci_output),
    )

    result = gpu_detect.detect_gpu_descriptions()

    assert len(result) == 2
    assert any("NVIDIA" in r for r in result)
    assert any("Intel" in r for r in result)


def test_falls_back_to_sysfs_when_lspci_missing(monkeypatch):
    def _raise(*a, **kw):
        raise FileNotFoundError("lspci not found")

    monkeypatch.setattr(gpu_detect.subprocess, "run", _raise)
    monkeypatch.setattr(gpu_detect.os, "listdir", lambda base: ["card0", "card0-DP-1", "card1", "renderD128"])
    monkeypatch.setattr(gpu_detect, "_read_pci_vendor", lambda path: {"card0": "0x8086", "card1": "0x10de"}[path.split("/")[-2]])

    result = gpu_detect.detect_gpu_descriptions()

    assert result == ["card0 (Intel)", "card1 (NVIDIA)"]


def test_sysfs_fallback_identifies_nvidia_by_vendor_id_without_lspci(monkeypatch):
    """The scenario that matters most: pciutils isn't staged in the snap,
    so lspci isn't available at all, but hardware-observe still grants
    sysfs access - the NVIDIA card must still be identifiable so
    gpu_preflight's hybrid-NVIDIA detection isn't silently blind."""
    def _raise(*a, **kw):
        raise FileNotFoundError("lspci: command not found")

    monkeypatch.setattr(gpu_detect.subprocess, "run", _raise)
    monkeypatch.setattr(gpu_detect.os, "listdir", lambda base: ["card0", "card1"])
    monkeypatch.setattr(gpu_detect, "_read_pci_vendor", lambda path: {"card0": "0x8086", "card1": "0x10de"}[path.split("/")[-2]])

    result = gpu_detect.detect_gpu_descriptions()

    assert any("nvidia" in r.lower() for r in result)


def test_sysfs_fallback_handles_unreadable_vendor_file(monkeypatch):
    def _raise(*a, **kw):
        raise FileNotFoundError("no lspci")

    monkeypatch.setattr(gpu_detect.subprocess, "run", _raise)
    monkeypatch.setattr(gpu_detect.os, "listdir", lambda base: ["card0"])
    monkeypatch.setattr(gpu_detect, "_read_pci_vendor", lambda path: None)

    result = gpu_detect.detect_gpu_descriptions()

    assert result == ["card0 (unknown)"]


def test_returns_empty_when_everything_fails(monkeypatch):
    def _raise(*a, **kw):
        raise FileNotFoundError("no lspci")

    monkeypatch.setattr(gpu_detect.subprocess, "run", _raise)
    monkeypatch.setattr(gpu_detect.os, "listdir", lambda base: (_ for _ in ()).throw(FileNotFoundError()))

    assert gpu_detect.detect_gpu_descriptions() == []
