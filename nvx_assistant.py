# Copyright (c) 2026 NVX One (nvxone.com). All rights reserved. Proprietary - see LICENSE. No copying or reuse without written permission.
"""NVX Assistant: plug in an NVX aircraft and see its firmware, updates and flight logs.

The aircraft is found on USB automatically and its Service unlock is signed automatically.
Run the EXE, or: pythonw nvx_assistant.py   (--watch = no window, open when an aircraft is plugged in)
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

import nvx_link
import nvx_ui as ui

APP_NAME = 'NVX Assistant'
APP_VERSION = '0.4.0'
INSTANCE_PORT = 47631
DOWNLOAD_LABEL = '\u2193  Save'
SAVED_LABEL = 'Saved  \u2713'
FROZEN = getattr(sys, 'frozen', False)
# Folder of the EXE (or of this script when run from source).
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
PROJECT_KEY = os.environ.get('NVX_SIGNING_KEY', '')  # private key is never in this repo
CONFIG_DIR = os.path.join(os.environ.get('APPDATA', HERE), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, 'config.json')
DEFAULTS = {
    # The key is never built into the EXE: a copy next to the EXE wins, else the project's key file.
    'signing_key': next((path for path in (os.path.join(HERE, 'sam-signing-key.pem'), PROJECT_KEY)
                         if os.path.isfile(path)), PROJECT_KEY),
    'update_manifest': os.environ.get('NVX_UPDATE_MANIFEST', 'https://updates.example.com/v1/updates/manifest'),
    'logs_dir': os.path.join(os.path.expanduser('~'), 'Documents', APP_NAME, 'logs'),
    # Set to true to save the newest flight to this PC automatically each time an aircraft connects.
    'auto_save_newest': False,
}


def enable_dpi():
    """Sharp text and graphics on scaled Windows displays (must run before the window is created)."""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def load_config():
    config = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            config.update(json.load(f))
    except (OSError, ValueError):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
    return config


def resource(name):
    """Path of a bundled asset, inside the EXE or next to the source."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, 'assets', name)


def model_name(serial):
    return 'Saksham' if 'SAK' in (serial or '').upper() else 'NVX Aircraft'


class Worker(threading.Thread):
    """Owns the serial port. Talks to the window only through the events queue."""

    def __init__(self, config, events):
        super().__init__(daemon=True)
        self.config = config
        self.events = events
        self.commands = queue.Queue()
        self.cancel_download = threading.Event()
        self.signer = nvx_link.ChallengeSigner(config['signing_key'])

    def emit(self, kind, **data):
        self.events.put((kind, data))

    def run(self):
        while True:
            port = nvx_link.find_drone_port()
            if port is None:
                self.emit('status', text='Waiting for aircraft. Connect it with a USB cable.', state='idle')
                time.sleep(1.5)
                continue
            try:
                self.session(port)
            except Exception as error:  # serial errors when the cable is pulled, etc.
                text = str(error)
                if 'PermissionError' in text or 'Access is denied' in text:
                    text = f'{port} is in use by another program (QGroundControl?). Close it and I will connect.'
                else:
                    text = 'Disconnected: ' + text.splitlines()[0][:90]
                self.emit('status', text=text, state='bad')
            self.emit('disconnected')
            time.sleep(2)

    def session(self, port):
        self.emit('status', text=f'Aircraft found on {port}. Connecting...', state='busy')
        link = nvx_link.DroneLink(port, self.signer)
        try:
            self.emit('status', text='Signing in to the aircraft...', state='busy')
            link.unlock()
            self.emit('status', text='Reading firmware...', state='busy')
            firmware = link.firmware()
            self.emit('firmware', firmware=firmware, port=port)
            update = nvx_link.fetch_update_manifest(self.config['update_manifest'])
            self.emit('update', text=nvx_link.update_status(firmware, update), update=update)
            self.emit('status', text='Reading flight log list...', state='busy')
            logs = link.list_logs(lambda done, total: self.emit(
                'status', text=f'Reading flight log list... {done}/{total}', state='busy'))
            self.emit('logs', logs=logs)
            self.emit('status', text=f'Connected. {len(logs)} flight logs on the aircraft.', state='good')
            while True:
                link.keep_unlocked()
                try:
                    command, entry = self.commands.get(timeout=1.0)
                except queue.Empty:
                    if link.m.recv_match(type='HEARTBEAT', blocking=True, timeout=3) is None:
                        raise nvx_link.LinkError('aircraft stopped answering')
                    continue
                if command == 'download':
                    self.download(link, firmware, entry)
        finally:
            link.close()

    def download(self, link, firmware, entry):
        folder = os.path.join(self.config['logs_dir'], firmware.get('serial') or 'aircraft')
        name = time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime(entry['utc'])) + f'_log{entry["id"]}.ulg'
        path = os.path.join(folder, name)
        log_id = entry['id']
        self.cancel_download.clear()
        self.emit('status', text=f'Saving {name}', state='busy')
        finished = link.download_log(
            log_id, entry['size'], path,
            progress=lambda done, total, rate: self.emit('progress', log_id=log_id, done=done, total=total, rate=rate),
            cancelled=self.cancel_download.is_set)
        if not finished:
            self.emit('cancelled', log_id=log_id)
            self.emit('status', text='Download cancelled.', state='good')
            return
        try:
            summary = nvx_link.summarize_log(path)
        except Exception as error:
            summary = f'Saved, but the summary failed: {error}'
        self.emit('downloaded', path=path, summary=summary, log_id=log_id)
        self.emit('status', text=f'Saved to {folder}', state='good')


class App(tk.Tk):
    """Home screen with the connected aircraft, then a sidebar with Firmware, Log Export and Device Info."""

    PAGES = (('firmware', 'firmware', 'Firmware Update'), ('logs', 'logs', 'Log Export'), ('info', 'info', 'Device Info'))

    def __init__(self):
        enable_dpi()
        super().__init__()
        self.k = max(1.0, self.winfo_fpixels('1i') / 96.0)
        self.title(APP_NAME)
        self.geometry(f'{self.P(1180)}x{self.P(740)}')
        self.minsize(self.P(1020), self.P(640))
        self.configure(bg=ui.BG)
        try:
            self.iconbitmap(resource('app.ico'))
        except tk.TclError:
            pass
        self.config_data = load_config()
        self.events = queue.Queue()
        self.logs = []
        self.downloaded = {}
        self.queued = set()
        self.firmware = None
        self.port = ''
        self.auto_saved = False  # newest flight already queued for this connection
        self.current_page = 'firmware'
        self.keep = {}  # PhotoImage references
        self.drone = self.load_image('drone.png')
        self.build_ui()
        self.show_home()
        self.worker = Worker(self.config_data, self.events)
        self.worker.start()
        self.after(100, self.pump)

    # ---------- small helpers ----------

    def P(self, value):
        """Design pixels (at 100% display scale) to real pixels."""
        return int(round(value * self.k))

    @staticmethod
    def font(size, weight='regular'):
        family = {'regular': 'Segoe UI', 'semibold': 'Segoe UI Semibold', 'bold': 'Segoe UI'}[weight]
        return (family, size, 'bold') if weight == 'bold' else (family, size)

    def load_image(self, name):
        try:
            image = Image.open(resource(name)).convert('RGBA')
        except OSError:
            return None
        box = image.getchannel('A').point(lambda v: 255 if v > 10 else 0).getbbox()
        return image.crop(box) if box else image

    def photo(self, key, image):
        self.keep[key] = ImageTk.PhotoImage(image)
        return self.keep[key]

    def drone_photo(self, key, width, height, backdrop):
        """The aircraft picture fitted into a box, flattened onto a background colour."""
        canvas = Image.new('RGB', (width, height), backdrop)
        if self.drone is not None:
            scale = min(width / self.drone.width, height / self.drone.height)
            size = (max(1, int(self.drone.width * scale)), max(1, int(self.drone.height * scale)))
            fitted = self.drone.resize(size, Image.LANCZOS)
            canvas.paste(fitted, ((width - size[0]) // 2, (height - size[1]) // 2), fitted)
        return self.photo(key, canvas)

    def label(self, parent, text='', size=10, colour=None, weight='regular', **options):
        return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=colour or ui.TEXT,
                        font=self.font(size, weight), bd=0, **options)

    def caption(self, parent, text):
        return self.label(parent, text.upper(), 8, ui.FAINT, 'semibold')

    def hairline(self, parent, pady=10):
        tk.Frame(parent, bg=ui.LINE, height=1).pack(fill='x', pady=self.P(pady))

    # ---------- layout ----------

    def build_ui(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('NVX.Treeview', background=ui.SURFACE, fieldbackground=ui.SURFACE, foreground=ui.TEXT,
                        rowheight=self.P(36), borderwidth=0, font=self.font(10))
        style.layout('NVX.Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])
        style.configure('NVX.Treeview.Heading', background=ui.SURFACE, foreground=ui.FAINT, relief='flat',
                        borderwidth=0, font=self.font(8, 'semibold'), padding=(self.P(8), self.P(8)))
        style.map('NVX.Treeview.Heading', background=[('active', ui.SURFACE)])
        style.map('NVX.Treeview', background=[('selected', '#eef0f3')], foreground=[('selected', ui.TEXT)])
        style.configure('NVX.Horizontal.TProgressbar', troughcolor=ui.LINE, background=ui.ACCENT, borderwidth=0,
                        lightcolor=ui.ACCENT, darkcolor=ui.ACCENT, bordercolor=ui.LINE, thickness=self.P(6))
        # Slim scrollbar without arrow buttons.
        style.layout('NVX.Vertical.TScrollbar', [('Vertical.Scrollbar.trough', {
            'sticky': 'ns', 'children': [('Vertical.Scrollbar.thumb', {'expand': '1', 'sticky': 'nswe'})]})])
        style.configure('NVX.Vertical.TScrollbar', background='#cfd4dc', troughcolor=ui.SURFACE, borderwidth=0,
                        bordercolor=ui.SURFACE, lightcolor='#cfd4dc', darkcolor='#cfd4dc', relief='flat',
                        arrowsize=self.P(9))
        style.map('NVX.Vertical.TScrollbar', background=[('active', '#b5bcc7')])

        top = tk.Frame(self, bg=ui.SURFACE, height=self.P(58))
        top.pack(fill='x')
        top.pack_propagate(False)
        try:
            mark = Image.open(resource('app.ico')).convert('RGBA').resize((self.P(28), self.P(28)), Image.LANCZOS)
            tk.Label(top, image=self.photo('mark', ui.on_colour(mark, ui.SURFACE)), bg=ui.SURFACE, bd=0).pack(
                side='left', padx=(self.P(20), self.P(10)))
        except OSError:
            tk.Frame(top, bg=ui.SURFACE, width=self.P(20)).pack(side='left')
        self.label(top, 'NVX Assistant', 13, weight='semibold').pack(side='left')
        self.label(top, f'v{APP_VERSION}', 9, ui.FAINT).pack(side='left', padx=self.P(10), pady=(self.P(3), 0))
        self.status_dot = tk.Canvas(top, width=self.P(10), height=self.P(10), bg=ui.SURFACE, highlightthickness=0)
        self.status_dot.pack(side='right', padx=(self.P(8), self.P(22)))
        self.dot = self.status_dot.create_oval(1, 1, self.P(10) - 1, self.P(10) - 1, fill=ui.FAINT, outline='')
        self.status = self.label(top, 'Starting...', 9, ui.MUTED)
        self.status.pack(side='right')
        tk.Frame(self, bg=ui.LINE, height=1).pack(fill='x')

        self.home = tk.Frame(self, bg=ui.BG)
        self.device = tk.Frame(self, bg=ui.BG)
        self.build_home()
        self.build_device()

    def build_home(self):
        middle = tk.Frame(self.home, bg=ui.BG)
        middle.place(relx=0.5, rely=0.47, anchor='center')
        self.label(middle, 'Devices', 20, weight='semibold').pack()
        self.home_sub = self.label(middle, 'No aircraft connected', 10, ui.MUTED)
        self.home_sub.pack(pady=(self.P(2), self.P(22)))

        # Shown while nothing is connected.
        self.empty_card = ui.Card(middle, self.k, padx=36, pady=30)
        body = self.empty_card.body
        size = self.P(64)
        circle = ui.rounded(size, size, size // 2, ui.ACCENT_SOFT, None, ui.SURFACE)
        glyph = ui.icon('usb', self.P(30), ui.ACCENT)
        circle.paste(glyph, ((size - glyph.width) // 2, (size - glyph.height) // 2), glyph)
        tk.Label(body, image=self.photo('usb', circle), bg=ui.SURFACE, bd=0).pack()
        self.label(body, 'Connect your aircraft', 15, weight='semibold').pack(pady=(self.P(14), self.P(10)))
        for number, step in enumerate(('Power on the aircraft.',
                                       'Connect it to this computer with a USB cable.',
                                       'It is found and signed in automatically.'), 1):
            row = tk.Frame(body, bg=ui.SURFACE)
            row.pack(anchor='w', pady=self.P(3))
            self.label(row, str(number), 9, ui.ACCENT, 'semibold', width=2).pack(side='left')
            self.label(row, step, 10, ui.MUTED).pack(side='left')
        self.empty_card.pack()

        # Shown when an aircraft is connected.
        self.device_card = ui.Card(middle, self.k, padx=28, pady=22)
        body = self.device_card.body
        self.card_image = tk.Label(body, image=self.drone_photo('home_drone', self.P(330), self.P(190), ui.SURFACE),
                                   bg=ui.SURFACE, bd=0, cursor='hand2')
        self.card_image.pack()
        self.card_name = self.label(body, '', 16, weight='semibold')
        self.card_name.pack(pady=(self.P(8), 0))
        self.card_serial = self.label(body, '', 10, ui.MUTED)
        self.card_serial.pack()
        self.card_version = self.label(body, '', 9, ui.FAINT)
        self.card_version.pack(pady=(self.P(2), self.P(14)))
        ui.Button(body, self.k, 'Open', self.show_device, 'primary', min_width=220, height=38).pack()
        for widget in (self.card_image, self.card_name, self.card_serial):
            widget.bind('<Button-1>', lambda _e: self.show_device())

    def build_device(self):
        side = tk.Frame(self.device, bg=ui.SIDE, width=self.P(250))
        side.pack(side='left', fill='y')
        side.pack_propagate(False)
        tk.Frame(self.device, bg=ui.LINE, width=1).pack(side='left', fill='y')

        back = tk.Frame(side, bg=ui.SIDE, cursor='hand2')
        back.pack(fill='x', padx=self.P(14), pady=(self.P(14), self.P(8)))
        arrow = tk.Label(back, image=self.photo('back', ui.on_colour(ui.icon('back', self.P(16), ui.MUTED), ui.SIDE)),
                         bg=ui.SIDE, bd=0)
        arrow.pack(side='left')
        back_text = self.label(back, 'All devices', 10, ui.MUTED)
        back_text.pack(side='left', padx=self.P(4))
        for widget in (back, arrow, back_text):
            widget.bind('<Button-1>', lambda _e: self.show_home())

        holder = ui.Card(side, self.k, padx=10, pady=10)
        holder.pack(fill='x', padx=self.P(16), pady=(self.P(4), self.P(10)))
        tk.Label(holder.body, image=self.drone_photo('side_drone', self.P(190), self.P(104), ui.SURFACE),
                 bg=ui.SURFACE, bd=0).pack()
        self.side_name = self.label(side, '', 13, weight='semibold')
        self.side_name.pack(anchor='w', padx=self.P(20))
        self.side_serial = self.label(side, '', 9, ui.MUTED)
        self.side_serial.pack(anchor='w', padx=self.P(20), pady=(0, self.P(16)))

        self.nav = {}
        for key, icon_name, title in self.PAGES:
            item = ui.NavItem(side, self.k, icon_name, title, lambda k=key: self.show_page(k), self.font(11))
            item.pack(fill='x')
            self.nav[key] = item
        self.side_port = self.label(side, '', 9, ui.FAINT)
        self.side_port.pack(side='bottom', anchor='w', padx=self.P(20), pady=self.P(16))

        self.pages = {}
        body = tk.Frame(self.device, bg=ui.BG)
        body.pack(side='left', fill='both', expand=True)
        for key, _icon, _title in self.PAGES:
            self.pages[key] = tk.Frame(body, bg=ui.BG, padx=self.P(30), pady=self.P(24))
        self.build_firmware_page(self.pages['firmware'])
        self.build_logs_page(self.pages['logs'])
        self.build_info_page(self.pages['info'])

    def page_header(self, page, title, subtitle):
        head = tk.Frame(page, bg=ui.BG)
        head.pack(fill='x', pady=(0, self.P(16)))
        text = tk.Frame(head, bg=ui.BG)
        text.pack(side='left')
        self.label(text, title, 18, weight='semibold').pack(anchor='w')
        self.label(text, subtitle, 10, ui.MUTED).pack(anchor='w')
        return head

    def build_firmware_page(self, page):
        self.page_header(page, 'Firmware Update', 'What is installed on the aircraft and what the NVX server offers.')
        row = tk.Frame(page, bg=ui.BG)
        row.pack(fill='x')
        row.columnconfigure(0, weight=1, uniform='fw')
        row.columnconfigure(1, weight=1, uniform='fw')

        installed = ui.Card(row, self.k)
        installed.grid(row=0, column=0, sticky='nsew', padx=(0, self.P(8)))
        body = installed.body
        self.caption(body, 'Installed on aircraft').pack(anchor='w')
        self.fw_version = self.label(body, '--', 26, weight='semibold')
        self.fw_version.pack(anchor='w', pady=(self.P(4), 0))
        self.fw_build = self.label(body, '', 10, ui.MUTED)
        self.fw_build.pack(anchor='w')
        self.hairline(body, 12)
        self.fw_board = self.detail_row(body, 'Flight controller')
        self.fw_source = self.detail_row(body, 'Source revision')

        latest = ui.Card(row, self.k)
        latest.grid(row=0, column=1, sticky='nsew', padx=(self.P(8), 0))
        body = latest.body
        head = tk.Frame(body, bg=ui.SURFACE)
        head.pack(fill='x')
        self.caption(head, 'Latest release').pack(side='left')
        self.update_pill = tk.Label(head, bg=ui.SURFACE, bd=0)
        self.update_pill.pack(side='right')
        self.update_version = self.label(body, 'Checking the NVX server...', 16, weight='semibold')
        self.update_version.pack(anchor='w', pady=(self.P(8), 0))
        self.update_notes = self.label(body, '', 10, ui.MUTED, justify='left', wraplength=self.P(360), anchor='w')
        self.update_notes.pack(anchor='w', fill='x', pady=(self.P(6), 0))

        self.hint_card = ui.Card(page, self.k, fill=ui.ACCENT_SOFT, border='#ffd3a8', padx=18, pady=12)
        self.update_hint = tk.Label(self.hint_card.body, text='', bg=ui.ACCENT_SOFT, fg='#8a3f00', font=self.font(10),
                                    justify='left', anchor='w', wraplength=self.P(760))
        self.update_hint.pack(fill='x')

    def detail_row(self, parent, title):
        row = tk.Frame(parent, bg=ui.SURFACE)
        row.pack(fill='x', pady=self.P(3))
        self.label(row, title, 10, ui.MUTED).pack(side='left')
        value = self.label(row, '--', 10, weight='semibold')
        value.pack(side='right')
        return value

    def build_logs_page(self, page):
        head = self.page_header(page, 'Log Export', 'Flight logs stored on the aircraft, newest first. Save one to analyse it on this PC.')
        ui.Button(head, self.k, 'Open folder', self.open_folder, 'secondary', 'folder').pack(side='right')
        self.cancel_button = ui.Button(head, self.k, 'Cancel download', self.cancel_downloads, 'danger', 'x')

        footer = tk.Frame(page, bg=ui.BG)
        footer.pack(side='bottom', fill='x', pady=(self.P(12), 0))
        self.progress_label = self.label(footer, '', 9, ui.MUTED)
        self.progress_label.pack(anchor='w')
        self.progress = ttk.Progressbar(footer, mode='determinate', maximum=1000, style='NVX.Horizontal.TProgressbar')
        self.progress.pack(fill='x', pady=(self.P(4), 0))

        body = tk.Frame(page, bg=ui.BG)
        body.pack(fill='both', expand=True)
        summary_card = ui.Card(body, self.k, padx=16, pady=14)
        summary_card.pack(side='right', fill='y', padx=(self.P(14), 0))
        self.caption(summary_card.body, 'Flight summary').pack(anchor='w', pady=(0, self.P(6)))
        self.summary = tk.Text(summary_card.body, width=34, bg=ui.SURFACE, fg=ui.TEXT, relief='flat', wrap='word',
                               bd=0, highlightthickness=0, font=self.font(10), spacing1=2, spacing3=2)
        self.summary.pack(fill='both', expand=True)

        table_card = ui.Card(body, self.k, padx=6, pady=6)
        table_card.pack(side='left', fill='both', expand=True)
        self.tree = ttk.Treeview(table_card.body, columns=('date', 'size', 'state', 'action'), show='headings',
                                 selectmode='browse', style='NVX.Treeview')
        for column, title, width, anchor in (('date', 'RECORDED (LOCAL TIME)', 220, 'w'), ('size', 'SIZE', 90, 'w'),
                                             ('state', 'ON THIS PC', 130, 'w'), ('action', '', 90, 'center')):
            self.tree.heading(column, text=title, anchor=anchor)
            self.tree.column(column, width=self.P(width), anchor=anchor, stretch=column == 'date')
        self.tree.tag_configure('busy', foreground=ui.ACCENT)
        self.tree.tag_configure('saved', foreground=ui.MUTED)
        scroll = ttk.Scrollbar(table_card.body, orient='vertical', command=self.tree.yview,
                               style='NVX.Vertical.TScrollbar')
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(side='left', fill='both', expand=True)
        self.tree.bind('<Button-1>', self.on_tree_click)
        self.tree.bind('<Motion>', self.on_tree_motion)
        self.tree.bind('<<TreeviewSelect>>', self.on_select)
        self.set_summary(f'Click "{DOWNLOAD_LABEL}" on a flight. Its summary appears here once it is saved.')

    def build_info_page(self, page):
        head = self.page_header(page, 'Device Info', 'Identity of the connected aircraft.')
        ui.Button(head, self.k, 'Copy serial number', self.copy_serial, 'secondary', 'copy').pack(side='right')
        card = ui.Card(page, self.k, padx=22, pady=8)
        card.pack(fill='x')
        self.info_values = {}
        rows = (('model', 'Model'), ('serial', 'Serial number'), ('version', 'Firmware version'),
                ('build', 'Firmware build'), ('git', 'Source revision'), ('board', 'Flight controller'),
                ('port', 'USB port'), ('security', 'Sign-in'))
        for index, (key, title) in enumerate(rows):
            row = tk.Frame(card.body, bg=ui.SURFACE)
            row.pack(fill='x', pady=self.P(9))
            self.label(row, title, 10, ui.MUTED, width=22, anchor='w').pack(side='left')
            value = self.label(row, '--', 10, weight='semibold', anchor='w')
            value.pack(side='left')
            self.info_values[key] = value
            if index < len(rows) - 1:
                tk.Frame(card.body, bg=ui.LINE, height=1).pack(fill='x')

    # ---------- navigation ----------

    def show_home(self):
        self.device.pack_forget()
        self.home.pack(fill='both', expand=True)

    def show_device(self):
        if self.firmware is None:
            return
        self.home.pack_forget()
        self.device.pack(fill='both', expand=True)
        self.show_page(self.current_page)

    def show_page(self, key):
        self.current_page = key
        for name, page in self.pages.items():
            page.pack_forget()
            self.nav[name].set_selected(name == key)
        self.pages[key].pack(fill='both', expand=True)

    # ---------- flight log actions ----------

    def set_summary(self, text):
        self.summary.configure(state='normal')
        self.summary.delete('1.0', 'end')
        self.summary.insert('1.0', text)
        self.summary.configure(state='disabled')

    def on_tree_motion(self, event):
        over = self.tree.identify_column(event.x) == '#4' and bool(self.tree.identify_row(event.y))
        self.tree.configure(cursor='hand2' if over else '')

    def on_tree_click(self, event):
        """A click on a row's Save cell queues that flight."""
        row = self.tree.identify_row(event.y)
        if not row or self.tree.identify_column(event.x) != '#4':
            return
        self.queue_download(int(row))

    def queue_download(self, log_id):
        if log_id in self.queued or log_id in self.downloaded:
            return
        entry = next((e for e in self.logs if e['id'] == log_id), None)
        row = str(log_id)
        if entry is None or not self.tree.exists(row):
            return
        self.queued.add(log_id)
        self.tree.set(row, 'state', 'Waiting...')
        self.tree.set(row, 'action', '')
        self.tree.item(row, tags=('busy',))
        self.cancel_button.pack(side='right', padx=(0, self.P(10)))
        self.worker.commands.put(('download', entry))

    def saved_path(self, entry):
        """Path of this flight on the PC if it was saved completely before (same name and size)."""
        if not entry.get('utc'):
            return None
        folder = os.path.join(self.config_data['logs_dir'], (self.firmware or {}).get('serial') or 'aircraft')
        name = time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime(entry['utc'])) + f'_log{entry["id"]}.ulg'
        path = os.path.join(folder, name)
        try:
            return path if os.path.getsize(path) == entry['size'] else None
        except OSError:
            return None

    def cancel_downloads(self):
        """Stops the running download and drops the ones still waiting."""
        try:
            while True:
                self.worker.commands.get_nowait()
        except queue.Empty:
            pass
        self.worker.cancel_download.set()
        for log_id in list(self.queued):
            self.reset_row(log_id)
        self.cancel_button.pack_forget()
        self.on_progress(-1, 0, 1, 0)

    def reset_row(self, log_id):
        self.queued.discard(log_id)
        if self.tree.exists(str(log_id)):
            self.tree.set(str(log_id), 'state', '')
            self.tree.set(str(log_id), 'action', DOWNLOAD_LABEL)
            self.tree.item(str(log_id), tags=())

    def on_select(self, _event=None):
        selection = self.tree.selection()
        if selection and int(selection[0]) in self.downloaded:
            path, summary = self.downloaded[int(selection[0])]
            self.set_summary(f'{os.path.basename(path)}\n\n{summary}')

    def open_folder(self):
        os.makedirs(self.config_data['logs_dir'], exist_ok=True)
        os.startfile(self.config_data['logs_dir'])

    def copy_serial(self):
        serial = (self.firmware or {}).get('serial')
        if serial:
            self.clipboard_clear()
            self.clipboard_append(serial)
            self.on_status('Serial number copied.', 'good')

    def set_pill(self, text, kind):
        if not text:
            self.update_pill.configure(image='')
            return
        fg, fill, outline = {'update': ('#ffffff', ui.ACCENT, None),
                             'ok': (ui.TEXT, '#eef0f3', None)}[kind]
        self.update_pill.configure(image=self.photo('pill', ui.pill_image(text, fg, fill, ui.SURFACE, self.k, outline)))

    # ---------- worker events ----------

    def pump(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                getattr(self, 'on_' + kind)(**data)
        except queue.Empty:
            pass
        self.after(100, self.pump)

    def on_status(self, text, state):
        self.status.configure(text=text)
        self.status_dot.itemconfigure(self.dot, fill={'good': ui.TEXT, 'bad': ui.BAD, 'busy': ui.ACCENT}.get(state, ui.FAINT))

    def on_firmware(self, firmware, port):
        self.firmware = firmware
        self.port = port
        name = model_name(firmware.get('serial'))
        serial = firmware.get('serial') or 'serial not set'
        self.card_name.configure(text=name)
        self.card_serial.configure(text=serial)
        self.card_version.configure(text=f'Firmware {firmware.get("version") or "--"}')
        self.home_sub.configure(text='1 aircraft connected')
        self.empty_card.pack_forget()
        self.device_card.pack()
        self.side_name.configure(text=name)
        self.side_serial.configure(text=serial)
        self.side_port.configure(text=f'USB  {port}')
        self.fw_version.configure(text=firmware.get('version') or '--')
        self.fw_build.configure(text=f'Built {firmware.get("build") or "?"}')
        source = f'{firmware.get("branch") or ""}  {firmware.get("git") or ""}'.strip()
        self.fw_board.configure(text=firmware.get('board') or '--')
        self.fw_source.configure(text=(source[:34] + '...') if len(source) > 36 else (source or '--'))
        values = {'model': name, 'serial': serial, 'version': firmware.get('version'), 'build': firmware.get('build'),
                  'git': source, 'board': firmware.get('board'), 'port': port,
                  'security': 'Signed in automatically (Service, 15 min, renewed)'}
        for key, value in values.items():
            self.info_values[key].configure(text=value or '--')
        self.show_device()

    def on_update(self, text, update):
        available = text.startswith('Update available')
        self.hint_card.pack_forget()
        if update:
            self.update_version.configure(text=update.get('version', '--'))
            self.update_notes.configure(text=update.get('releaseNotes', ''))
            self.set_pill('Update available' if available else 'Up to date', 'update' if available else 'ok')
            if available:
                self.update_hint.configure(text='Install this update from NVX Fly on the controller: '
                                                'Software Updates > Update Aircraft.')
                self.hint_card.pack(fill='x', pady=(self.P(16), 0))
        else:
            self.update_version.configure(text='NVX server not reachable')
            self.update_notes.configure(text='Check the internet connection of this computer, then reconnect the aircraft.')
            self.set_pill('', 'ok')
        self.nav['firmware'].set_badge('●' if available else '', ui.ACCENT)

    def on_logs(self, logs):
        # The aircraft lists logs in storage order; show the newest flight first.
        logs = sorted(logs, key=lambda entry: entry.get('utc') or 0, reverse=True)
        self.logs = logs
        self.downloaded = {}
        for entry in logs:
            path = self.saved_path(entry)
            if path:
                self.downloaded[entry['id']] = (path, 'Saved on this PC earlier.\n' + path)
        self.tree.delete(*self.tree.get_children())
        for entry in logs:
            when = time.strftime('%d %b %Y   %H:%M:%S', time.localtime(entry['utc'])) if entry['utc'] else 'unknown'
            saved = entry['id'] in self.downloaded
            self.tree.insert('', 'end', iid=str(entry['id']), tags=('saved',) if saved else (),
                             values=(when, f'{entry["size"] / 1048576:.1f} MB', SAVED_LABEL if saved else '',
                                     '' if saved else DOWNLOAD_LABEL))
        self.nav['logs'].set_badge(str(len(logs)) if logs else '')
        if logs and not self.auto_saved and self.config_data.get('auto_save_newest', False):
            self.auto_saved = True
            if logs[0].get('utc'):
                self.queue_download(logs[0]['id'])

    def on_progress(self, log_id, done, total, rate):
        self.progress['value'] = 1000 * done / max(1, total)
        if log_id < 0:
            self.progress_label.configure(text='')
            return
        self.progress_label.configure(
            text=f'Saving flight   {done / 1048576:.1f} / {total / 1048576:.1f} MB   {rate / 1024:.0f} KB/s')
        if self.tree.exists(str(log_id)) and log_id in self.queued:
            self.tree.set(str(log_id), 'state', f'Saving {100 * done // max(1, total)}%')

    def on_downloaded(self, path, summary, log_id):
        self.downloaded[log_id] = (path, summary)
        self.queued.discard(log_id)
        if not self.queued:
            self.cancel_button.pack_forget()
            self.on_progress(-1, 0, 1, 0)
        if self.tree.exists(str(log_id)):
            self.tree.set(str(log_id), 'state', SAVED_LABEL)
            self.tree.set(str(log_id), 'action', '')
            self.tree.item(str(log_id), tags=('saved',))
            self.tree.selection_set(str(log_id))
        self.set_summary(f'{os.path.basename(path)}\n\n{summary}')

    def on_cancelled(self, log_id):
        self.reset_row(log_id)

    def on_disconnected(self):
        self.firmware = None
        self.logs = []
        self.auto_saved = False
        self.queued.clear()
        self.cancel_button.pack_forget()
        self.on_progress(-1, 0, 1, 0)
        self.tree.delete(*self.tree.get_children())
        self.device_card.pack_forget()
        self.empty_card.pack()
        self.home_sub.configure(text='No aircraft connected')
        self.nav['logs'].set_badge('')
        self.show_home()


def single_instance():
    """Holds a localhost port so the watcher (and a second launch) can tell the window is open."""
    import socket
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(('127.0.0.1', INSTANCE_PORT))
        lock.listen(1)
        return lock
    except OSError:
        return None


def assistant_running():
    import socket
    try:
        socket.create_connection(('127.0.0.1', INSTANCE_PORT), timeout=0.3).close()
        return True
    except OSError:
        return False


def watch():
    """No window: opens the Assistant once each time an aircraft is plugged in (run with --watch)."""
    import socket
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(('127.0.0.1', INSTANCE_PORT + 1))  # only one watcher
    except OSError:
        return
    launch = [sys.executable] if FROZEN else [sys.executable, os.path.abspath(__file__)]
    was_present = False
    while True:
        present = nvx_link.find_drone_port() is not None
        if present and not was_present and not assistant_running():
            subprocess.Popen(launch, cwd=HERE)
        was_present = present
        time.sleep(2)


if __name__ == '__main__':
    if '--watch' in sys.argv:
        watch()
    else:
        instance = single_instance()
        if instance is not None:
            App().mainloop()
