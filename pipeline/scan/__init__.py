from __future__ import annotations

"""Standalone SKU barcode scan module for the production capture line.

A phone is used purely as a camera (the "always-looking" gun logic): it streams
a frame every few hundred ms back to this Flask service; the service decodes it
with the capture module's barcode decoder (pyzbar/opencv) and exposes the most
recent result so a desktop page can auto-fill an SKU. Kept independent from the
D435i capture station so its closed loop can be validated on its own.

HTTPS is required for the phone camera: browsers only allow getUserMedia in a
secure context (HTTPS or localhost). Start with --https to auto-generate a
self-signed cert (see .p3d/cert/) containing the LAN IP SANs, then open
https://<PC-IP>:5070/scan on the phone.

Run: python -m pipeline.scan --host 0.0.0.0 --port 5070 --https
"""

from pipeline.scan.app import create_app

__all__ = ["create_app"]
