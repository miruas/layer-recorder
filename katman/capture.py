# SPDX-License-Identifier: GPL-3.0-only
"""Legacy Python client API. Capture/process/buffer work lives in Rust."""
from .common import request

class Recorder:
    def __init__(self):
        from .__main__ import ensure_daemon
        ensure_daemon()
    def status(self):return request('status')
    def set_capture_state(self,kind,enabled):return request('set-capture-state',kind=kind,enabled=enabled)
    def toggle_record(self):return request('toggle-record')
    def toggle_replay(self):return request('toggle-replay')
    def save_replay(self,seconds):return request('save-replay',seconds=seconds)
    def reload_settings(self):return request('reload-settings')
    def shutdown(self):return request('shutdown')
