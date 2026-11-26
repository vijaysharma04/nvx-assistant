# NVX Assistant

> **© 2026 NVX One. All rights reserved.** Proprietary. This code is published for viewing only. Copying, modifying, redistributing or using it to build products is not allowed without written permission. See [LICENSE](LICENSE).

Windows desktop tool for NVX aircraft. Plug an NVX drone in over USB and it shows the flight controller's firmware version, available updates and flight logs, and lets you save logs to the PC.

- `nvx_assistant.py`: app window and workflow (Tkinter)
- `nvx_link.py`: MAVLink-over-USB link to the PX4-based NVX flight controller
- `nvx_ui.py`: shared UI styling
- `NVX Assistant.spec`: PyInstaller build to a single EXE

Signing keys used for the service unlock are **not** included in this repository.

Made in India by [NVX One](https://nvxone.com).
