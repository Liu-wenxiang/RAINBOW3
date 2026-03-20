import json
import subprocess
import sys
import colorsys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from tkinter import colorchooser
from tkinter import ttk

from PIL import Image, ImageDraw
import pystray

import send_hid

# 自定义颜色报文（来自抓包推导）
# 一条报文会同时设置三种模式颜色，顺序为：Switch RGB + PS RGB + Xbox RGB + checksum
# 总格式：a50df5 [SwitchRGB][PSRGB][XboxRGB][校验] 00...00
# 校验算法： (9 个颜色字节之和 + 0xA7) & 0xff

MULTI_COLOR_PREFIX = "a50df5"
MULTI_COLOR_CHECKSUM_ADD = 0xA7

LED_PACKET_PREFIX = bytes([0xA5, 0x10, 0x70])
LED_COMMIT_PACKET = "a5047019" + ("00" * 60)
LED_MASKS = [
    0x00000001,
    0x00000002,
    0x00000004,
    0x00000008,
    0x00000010,
    0x00000020,
    0x00000040,
    0x00000080,
    0x00000100,
    0x00000200,
    0x00000400,
    0x00000800,
    0x00001000,
    0x00002000,
]


def _normalize_rgb_hex(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("#"):
        s = s[1:]
    s = s.replace(" ", "")
    if len(s) != 6:
        raise ValueError("颜色格式必须是 RRGGBB（6 位十六进制），例如 d1ffa3")
    try:
        int(s, 16)
    except ValueError as e:
        raise ValueError("颜色包含非十六进制字符") from e
    return s.lower()


def _rgb_sum(rgb_hex: str) -> int:
    rgb_hex = _normalize_rgb_hex(rgb_hex)
    r = int(rgb_hex[0:2], 16)
    g = int(rgb_hex[2:4], 16)
    b = int(rgb_hex[4:6], 16)
    return r + g + b


def build_multi_color_hex(switch_rgb_hex: str, ps_rgb_hex: str, xbox_rgb_hex: str) -> str:
    switch_rgb_hex = _normalize_rgb_hex(switch_rgb_hex)
    ps_rgb_hex = _normalize_rgb_hex(ps_rgb_hex)
    xbox_rgb_hex = _normalize_rgb_hex(xbox_rgb_hex)

    checksum = (_rgb_sum(switch_rgb_hex) + _rgb_sum(ps_rgb_hex) + _rgb_sum(xbox_rgb_hex) + MULTI_COLOR_CHECKSUM_ADD) & 0xFF
    base = MULTI_COLOR_PREFIX + switch_rgb_hex + ps_rgb_hex + xbox_rgb_hex + f"{checksum:02x}"
    byte_len = len(base) // 2
    if byte_len > 64:
        raise ValueError("三色自定义报文模板超过 64 字节")
    return base + ("00" * (64 - byte_len))


def _rgb_bytes(rgb_hex: str) -> tuple[int, int, int]:
    rgb_hex = _normalize_rgb_hex(rgb_hex)
    return int(rgb_hex[0:2], 16), int(rgb_hex[2:4], 16), int(rgb_hex[4:6], 16)


def _clamp_percent(v: int) -> int:
    return max(0, min(100, int(v)))


def _speed_to_byte(speed_percent: int) -> int:
    speed = _clamp_percent(speed_percent)
    return 0xFF - round(speed * 255 / 100)


def _derive_gradient_partner(rgb_hex: str) -> str:
    rgb_hex = _normalize_rgb_hex(rgb_hex)
    r = int(rgb_hex[0:2], 16) / 255
    g = int(rgb_hex[2:4], 16) / 255
    b = int(rgb_hex[4:6], 16) / 255
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    partner_h = (h + 0.18) % 1.0
    partner_s = max(0.55, s)
    partner_v = max(0.9, v)
    pr, pg, pb = colorsys.hsv_to_rgb(partner_h, partner_s, partner_v)
    return f"{int(pr*255):02x}{int(pg*255):02x}{int(pb*255):02x}"


def build_led_packet(mask: int, rgb_hex: str, effect: str = "static", speed_percent: int = 0) -> str:
    r, g, b = _rgb_bytes(rgb_hex)

    if effect == "static":
        mode_bytes = [0x00, 0x00, 0x00]
    elif effect == "breathing":
        speed_byte = _speed_to_byte(speed_percent)
        mode_bytes = [0x02, 0x00, speed_byte]
    else:
        raise ValueError("不支持的灯效模式")

    packet = bytearray()
    packet.extend(LED_PACKET_PREFIX)
    packet.extend(mask.to_bytes(4, "big"))
    packet.extend(mode_bytes)
    packet.extend([0xFF, 0xFF, r, g, b])
    packet.append(sum(packet) & 0xFF)

    if len(packet) > 64:
        raise ValueError("14 灯珠数据包超过 64 字节")

    packet.extend(b"\x00" * (64 - len(packet)))
    return packet.hex()


def collect_palette_colors(colors: list[str]) -> list[str]:
    normalized: list[str] = []
    for color in colors:
        color = (color or "").strip()
        if not color:
            continue
        else:
            normalized.append(_normalize_rgb_hex(color))

    if not normalized:
        raise ValueError("多色呼吸至少要填写 1 个颜色")

    return normalized[:5]


def build_led_multi_breathing_packet(mask: int, colors: list[str], speed_percent: int = 0, mode_byte: int = 0x02) -> str:
    colors = collect_palette_colors(colors)
    speed_byte = _speed_to_byte(speed_percent)
    packet_len = 0x10 + (len(colors) - 1) * 3

    packet = bytearray()
    packet.extend([0xA5, packet_len, 0x70])
    packet.extend(mask.to_bytes(4, "big"))
    packet.extend([mode_byte, 0x00, speed_byte, 0xFF, 0xFF])
    for color in colors:
        r, g, b = _rgb_bytes(color)
        packet.extend([r, g, b])
    packet.append(sum(packet) & 0xFF)

    if len(packet) > 64:
        raise ValueError("14 灯珠多色呼吸数据包超过 64 字节")

    packet.extend(b"\x00" * (64 - len(packet)))
    return packet.hex()


def run_send_hid(hex_payload: str) -> int:
    results = send_hid.send_hex_payloads([hex_payload], report_id=0, payload_len=64)
    return results[0] if results else 0


def run_send_hid_sequence(hex_payloads: list[str]) -> list[int]:
    return send_hid.send_hex_payloads(hex_payloads, report_id=0, payload_len=64)


class App(tk.Tk):
    FONT_FAMILY = "Microsoft YaHei"
    LED_HIGHLIGHT_COLOR = "#ff8a3d"
    BRIGHTNESS_BAR_WIDTH = 260
    COLOR_BAR_WIDTH = 430
    LED_PALETTE_DOT_OFFSETS_RIGHT = [(8, -14), (16, -9), (20, 0), (16, 9), (8, 14)]
    LED_PALETTE_DOT_OFFSETS_LEFT = [(-8, -14), (-16, -9), (-20, 0), (-16, 9), (-8, 14)]
    LED_PALETTE_DOT_OFFSETS_DOWN_LEFT = [(-14, 6), (-8, 13), (0, 17), (8, 13), (14, 6)]
    LED_PALETTE_DOT_OFFSETS_DOWN_RIGHT = [(-14, 6), (-8, 13), (0, 17), (8, 13), (14, 6)]
    LED_PALETTE_DOT_OFFSETS_TOP_LEFT = [(-14, -6), (-8, -13), (0, -17), (8, -13), (14, -6)]
    LED_PALETTE_DOT_OFFSETS_TOP_RIGHT = [(-14, -6), (-8, -13), (0, -17), (8, -13), (14, -6)]
    QUICK_COLORS = ["ff2020", "ff9f1c", "fff000", "18ff18", "19d9e8", "2b2bff", "9222ff"]
    EFFECT_LABELS = {
        "static": "常亮",
        "breathing": "呼吸",
        "multi_breathing": "交替",
        "gradient": "渐变",
    }
    MIRROR_LED_MAP = {
        0: 13,
        1: 12,
        2: 11,
        3: 10,
        4: 9,
        5: 8,
        6: 7,
        7: 6,
        8: 5,
        9: 4,
        10: 3,
        11: 2,
        12: 1,
        13: 0,
    }
    DISPLAY_TO_ACTUAL_LED_INDEX = [12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 13]
    LED_POSITIONS = [
        (202, 46),
        (48, 66),
        (48, 106),
        (48, 146),
        (92, 160),
        (134, 168),
        (202, 164),
        (250, 164),
        (318, 168),
        (360, 160),
        (404, 146),
        (404, 106),
        (404, 66),
        (250, 46),
    ]

    def __init__(self):
        super().__init__()
        self.title("RAINBOW3 灯效编辑")
        self.geometry("1040x480")
        self.minsize(960, 610)
        self.configure(bg="#f3f4f6")
        self.option_add("*Font", (self.FONT_FAMILY, 9))

        self.led_effect_var = tk.StringVar(value="static")
        self.led_speed_var = tk.IntVar(value=50)
        self.brightness_var = tk.IntVar(value=100)
        self.mirror_var = tk.BooleanVar(value=False)
        self.profile_name_var = tk.StringVar()
        self.profile_selected_var = tk.StringVar()
        self.profile_program_var = tk.StringVar()
        self.auto_profile_status_var = tk.StringVar(value="自动关联：未配置程序")
        self.current_color_var = tk.StringVar(value="00ffff")
        self.current_hex_var = tk.StringVar(value="00ffff")
        self.hue_var = tk.IntVar(value=180)
        self.sat_var = tk.IntVar(value=100)
        self.xbox_color_var = tk.StringVar(value="ffffff")
        self.switch_color_var = tk.StringVar(value="ff0000")
        self.ps_color_var = tk.StringVar(value="0000ff")
        self.mode_light_dots: dict[str, tuple[tk.Canvas, int]] = {}
        self.mirror_switch_canvas: tk.Canvas | None = None
        self.mirror_switch_thumb: int | None = None
        self.selected_leds: set[int] = set()
        self.led_color_vars: list[tk.StringVar] = []
        self.led_palette_vars: list[list[tk.StringVar]] = []
        self.led_palette_labels: list[tk.Label] = []
        self.led_items: list[tuple[int, int]] = []
        self.led_palette_preview_items: list[list[int]] = []
        self.palette_brush_active = False
        self.palette_brush_source_index: int | None = None
        self.palette_brush_colors: list[str] = []
        self.palette_brush_button: tk.Button | None = None
        self.palette_brush_status_var = tk.StringVar(value="")
        self.profile_combo: ttk.Combobox | None = None
        self.profiles = self._load_profiles_from_disk()
        self._auto_profile_last_match = ""
        self._auto_profile_poll_ms = 4000
        self._auto_profile_job: str | None = None
        self._auto_profile_worker_running = False
        self._updating_picker = False
        self._tray_icon: pystray.Icon | None = None
        self._tray_visible = False
        self._tray_notification_shown = False
        self._is_exiting = False
        self._build_led_state()

        outer = tk.Frame(self, bg="#f3f4f6")
        outer.pack(fill="both", expand=True, padx=10, pady=6)

        card = tk.Frame(outer, bg="#ffffff", bd=0, highlightthickness=0)
        card.pack(fill="both", expand=True)

        left_col = tk.Frame(card, bg="#ffffff", width=440)
        left_col.pack(side="left", fill="both", expand=True)
        left_col.pack_propagate(False)

        right_col = tk.Frame(card, bg="#ffffff", width=300)
        right_col.pack(side="right", fill="y", padx=(8, 0))
        right_col.pack_propagate(False)

        self._build_main_designer(left_col)
        self._build_profile_section(right_col)
        self._build_mode_light_section(right_col)
        self._build_log_area(right_col)

        self.refresh_led_palette_labels()
        self._sync_picker_from_hex(self.current_color_var.get())
        self.mirror_var.trace_add("write", self._refresh_mirror_switch)
        self._bind_mode_light_traces()
        self._refresh_profile_selector()
        self._load_initial_profile()
        self._refresh_mode_light_dots()
        self._refresh_led_canvas()
        self._update_effect_buttons()
        self.bind("<Escape>", self._on_escape)
        self.bind("<Unmap>", self._on_window_unmap)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._schedule_auto_profile_poll(1000)

    def _profiles_file_path(self) -> Path:
        if getattr(sys, "frozen", False):
            base_dir = Path(sys.executable).resolve().parent
        else:
            base_dir = Path(__file__).resolve().parent
        return base_dir / "hid_light_gui_profiles.json"

    def _load_profiles_from_disk(self) -> dict[str, dict]:
        file_path = self._profiles_file_path()
        if not file_path.exists():
            return {}
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        profiles = data.get("profiles", {})
        if not isinstance(profiles, dict):
            return {}
        return {str(name): profile for name, profile in profiles.items() if isinstance(profile, dict)}

    def _save_profiles_to_disk(self):
        file_path = self._profiles_file_path()
        payload = {"profiles": self.profiles}
        file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _collect_current_profile(self) -> dict:
        return {
            "effect": self.led_effect_var.get(),
            "speed": self.led_speed_var.get(),
            "brightness": self.brightness_var.get(),
            "mirror": self.mirror_var.get(),
            "current_color": self.current_color_var.get(),
            "program": self.profile_program_var.get().strip(),
            "mode_lights": {
                "switch": self.switch_color_var.get(),
                "ps": self.ps_color_var.get(),
                "xbox": self.xbox_color_var.get(),
            },
            "led_colors": [var.get() for var in self.led_color_vars],
            "led_palettes": [[slot.get() for slot in palette] for palette in self.led_palette_vars],
        }

    def _apply_profile(self, profile: dict):
        effect = str(profile.get("effect", "static"))
        self.led_effect_var.set(effect if effect in self.EFFECT_LABELS else "static")
        self.led_speed_var.set(_clamp_percent(profile.get("speed", 50)))
        self.brightness_var.set(_clamp_percent(profile.get("brightness", 100)))
        self.mirror_var.set(bool(profile.get("mirror", False)))

        try:
            self._sync_picker_from_hex(str(profile.get("current_color", "00ffff")))
        except Exception:
            self._sync_picker_from_hex("00ffff")

        self.profile_program_var.set(str(profile.get("program", "")).strip())

        mode_lights = profile.get("mode_lights", {})
        self.switch_color_var.set(str(mode_lights.get("switch", self.switch_color_var.get())))
        self.ps_color_var.set(str(mode_lights.get("ps", self.ps_color_var.get())))
        self.xbox_color_var.set(str(mode_lights.get("xbox", self.xbox_color_var.get())))

        led_colors = profile.get("led_colors", [])
        for idx, var in enumerate(self.led_color_vars):
            if idx < len(led_colors):
                var.set(str(led_colors[idx]))

        led_palettes = profile.get("led_palettes", [])
        for idx, palette_vars in enumerate(self.led_palette_vars):
            loaded_palette = led_palettes[idx] if idx < len(led_palettes) and isinstance(led_palettes[idx], list) else []
            for slot_idx, slot_var in enumerate(palette_vars):
                slot_var.set(str(loaded_palette[slot_idx]) if slot_idx < len(loaded_palette) else "")

        self.refresh_led_palette_labels()
        self._on_brightness_change()
        self._on_speed_change()
        self._refresh_mode_light_dots()
        self._refresh_led_canvas()
        self._update_effect_buttons()

    def _refresh_profile_selector(self, preferred_name: str = ""):
        if self.profile_combo is None:
            return
        names = sorted(self.profiles)
        self.profile_combo.configure(values=names)
        target_name = preferred_name or self.profile_selected_var.get().strip()
        if target_name in self.profiles:
            self.profile_selected_var.set(target_name)
        elif names:
            self.profile_selected_var.set(names[0])
        else:
            self.profile_selected_var.set("")

    def _load_initial_profile(self):
        name = self.profile_selected_var.get().strip()
        if not name:
            return
        profile = self.profiles.get(name)
        if profile is None:
            return
        self.profile_name_var.set(name)
        self._apply_profile(profile)
        self._append(f"\n[配置] 启动时已加载：{name}\n")

    def _save_current_profile(self):
        name = self.profile_name_var.get().strip()
        if not name:
            messagebox.showerror("名称为空", "先输入一个配置名称。")
            return
        self.profiles[name] = self._collect_current_profile()
        self._save_profiles_to_disk()
        self._refresh_profile_selector(name)
        self._append(f"\n[配置] 已保存：{name}\n")

    def _load_selected_profile(self, _event=None):
        name = self.profile_selected_var.get().strip()
        if not name:
            messagebox.showerror("未选择配置", "先选择要加载的配置。")
            return
        profile = self.profiles.get(name)
        if profile is None:
            messagebox.showerror("配置不存在", f"找不到配置：{name}")
            return
        self.profile_name_var.set(name)
        self._apply_profile(profile)
        self._append(f"\n[配置] 已加载：{name}\n")

    def _delete_selected_profile(self):
        name = self.profile_selected_var.get().strip() or self.profile_name_var.get().strip()
        if not name:
            messagebox.showerror("未选择配置", "先选择要删除的配置。")
            return
        if name not in self.profiles:
            messagebox.showerror("配置不存在", f"找不到配置：{name}")
            return
        del self.profiles[name]
        self._save_profiles_to_disk()
        self._refresh_profile_selector()
        if self._auto_profile_last_match == name:
            self._auto_profile_last_match = ""
        if self.profile_name_var.get().strip() == name:
            self.profile_name_var.set("")
        self._append(f"\n[配置] 已删除：{name}\n")

    def _bind_mode_light_traces(self):
        for color_var in [self.switch_color_var, self.ps_color_var, self.xbox_color_var]:
            color_var.trace_add("write", self._refresh_mode_light_dots)

    def _build_profile_section(self, parent: tk.Widget):
        section = tk.Frame(parent, bg="#ffffff", padx=12, pady=6)
        section.pack(fill="x")
        tk.Label(section, text="配置方案", bg="#ffffff", font=(self.FONT_FAMILY, 10, "bold")).pack(anchor="w")

        name_row = tk.Frame(section, bg="#ffffff")
        name_row.pack(fill="x", pady=(8, 4))
        tk.Entry(name_row, textvariable=self.profile_name_var, bd=0, bg="#f1f1f1", font=(self.FONT_FAMILY, 9)).pack(side="left", fill="x", expand=True)
        tk.Button(name_row, text="保存", command=self._save_current_profile, bd=0, bg="#111111", fg="#ffffff", padx=10, pady=5, font=(self.FONT_FAMILY, 8, "bold")).pack(side="left", padx=(6, 0))

        program_row = tk.Frame(section, bg="#ffffff")
        program_row.pack(fill="x", pady=(0, 4))
        tk.Label(program_row, text="关联程序", bg="#ffffff", font=(self.FONT_FAMILY, 9)).pack(side="left")
        tk.Entry(program_row, textvariable=self.profile_program_var, bd=0, bg="#f1f1f1", font=(self.FONT_FAMILY, 9)).pack(side="left", fill="x", expand=True, padx=(8, 0))

        tk.Label(section, text="填写 EXE 名称，如 game.exe；检测到运行时会自动应用该配置", bg="#ffffff", fg="#666666", font=(self.FONT_FAMILY, 8), justify="left").pack(anchor="w", pady=(0, 6))

        select_row = tk.Frame(section, bg="#ffffff")
        select_row.pack(fill="x")
        self.profile_combo = ttk.Combobox(section, textvariable=self.profile_selected_var, state="readonly", font=(self.FONT_FAMILY, 9))
        self.profile_combo.pack(fill="x")
        self.profile_combo.bind("<<ComboboxSelected>>", self._load_selected_profile)

        action_row = tk.Frame(section, bg="#ffffff")
        action_row.pack(fill="x", pady=(6, 0))
        tk.Button(action_row, text="加载", command=self._load_selected_profile, bd=0, bg="#ececec", padx=10, pady=5, font=(self.FONT_FAMILY, 8)).pack(side="left")
        tk.Button(action_row, text="删除", command=self._delete_selected_profile, bd=0, bg="#ececec", padx=10, pady=5, font=(self.FONT_FAMILY, 8)).pack(side="left", padx=(6, 0))
        tk.Button(action_row, text="立即检测", command=self._trigger_auto_profile_check, bd=0, bg="#ececec", padx=10, pady=5, font=(self.FONT_FAMILY, 8)).pack(side="right")
        tk.Label(section, textvariable=self.auto_profile_status_var, bg="#ffffff", fg="#666666", font=(self.FONT_FAMILY, 8), justify="left", wraplength=260).pack(anchor="w", pady=(6, 0))

    def _refresh_mode_light_dots(self, *_args):
        for key, color_var in {
            "switch": self.switch_color_var,
            "ps": self.ps_color_var,
            "xbox": self.xbox_color_var,
        }.items():
            dot_info = self.mode_light_dots.get(key)
            if not dot_info:
                continue
            dot_canvas, dot_item = dot_info
            try:
                dot_canvas.itemconfigure(dot_item, fill=f"#{_normalize_rgb_hex(color_var.get())}")
            except Exception:
                dot_canvas.itemconfigure(dot_item, fill="#c7c7c7")

    def _toggle_mirror(self, _event=None):
        self.mirror_var.set(not self.mirror_var.get())

    def _refresh_mirror_switch(self, *_args):
        if self.mirror_switch_canvas is None or self.mirror_switch_thumb is None:
            return
        enabled = self.mirror_var.get()
        track_color = "#111111" if enabled else "#d8dde6"
        thumb_left = 21 if enabled else 3
        thumb_right = thumb_left + 18
        self.mirror_switch_canvas.itemconfigure("mirror_track", fill=track_color, outline=track_color)
        self.mirror_switch_canvas.coords(self.mirror_switch_thumb, thumb_left, 2, thumb_right, 20)

    def _build_led_state(self):
        for idx in range(14):
            default_color = "ff3030" if idx < 7 else "2040ff"
            self.led_color_vars.append(tk.StringVar(value=default_color))
            self.led_palette_vars.append([
                tk.StringVar(value=default_color),
                tk.StringVar(),
                tk.StringVar(),
                tk.StringVar(),
                tk.StringVar(),
            ])

    def _palette_offsets_for_led(self, led_index: int) -> list[tuple[int, int]]:
        if led_index in {1, 2, 3, 4, 5}:
            return self.LED_PALETTE_DOT_OFFSETS_LEFT
        if led_index in {8, 9, 10, 11, 12}:
            return self.LED_PALETTE_DOT_OFFSETS_RIGHT
        if led_index == 0:
            return self.LED_PALETTE_DOT_OFFSETS_TOP_LEFT
        if led_index == 13:
            return self.LED_PALETTE_DOT_OFFSETS_TOP_RIGHT
        if led_index == 6:
            return self.LED_PALETTE_DOT_OFFSETS_DOWN_LEFT
        if led_index == 7:
            return self.LED_PALETTE_DOT_OFFSETS_DOWN_RIGHT
        return self.LED_PALETTE_DOT_OFFSETS_RIGHT

    def _build_main_designer(self, parent: tk.Widget):
        section = tk.Frame(parent, bg="#ffffff", padx=10, pady=6)
        section.pack(fill="x")

        effect_shell = tk.Frame(section, bg="#f3f4f6", padx=3, pady=2)
        effect_shell.pack(fill="x")
        self.effect_buttons: dict[str, tk.Button] = {}
        for effect in ["static", "breathing", "multi_breathing", "gradient"]:
            btn = tk.Button(
                effect_shell,
                text=self.EFFECT_LABELS[effect],
                bd=0,
                relief="flat",
                padx=10,
                pady=5,
                font=(self.FONT_FAMILY, 9, "bold"),
                command=lambda e=effect: self.set_effect(e),
            )
            btn.pack(side="left", expand=True, fill="x", padx=2)
            self.effect_buttons[effect] = btn

        brightness = tk.Frame(section, bg="#ffffff", pady=2)
        brightness.pack(fill="x")
        tk.Label(brightness, text="亮度", bg="#ffffff", font=(self.FONT_FAMILY, 10, "bold")).pack(side="left")
        self.brightness_value_label = tk.Label(brightness, text="100", width=3, anchor="e", bg="#ffffff", font=(self.FONT_FAMILY, 10, "bold"))
        self.brightness_value_label.pack(side="left", padx=(10, 6))
        self.brightness_canvas = tk.Canvas(
            brightness,
            width=self.BRIGHTNESS_BAR_WIDTH,
            height=16,
            bg="#ffffff",
            highlightthickness=0,
        )
        self.brightness_canvas.pack(side="left", fill="x", expand=True)
        self._draw_brightness_bar()
        self.brightness_canvas.bind("<Button-1>", self._on_brightness_click)
        self.brightness_canvas.bind("<B1-Motion>", self._on_brightness_click)

        speed = tk.Frame(section, bg="#ffffff", pady=2)
        speed.pack(fill="x")
        tk.Label(speed, text="动效速度", bg="#ffffff", font=(self.FONT_FAMILY, 10, "bold")).pack(side="left")
        self.speed_value_label = tk.Label(speed, text="50", width=3, anchor="e", bg="#ffffff", font=(self.FONT_FAMILY, 10, "bold"))
        self.speed_value_label.pack(side="left", padx=(10, 6))
        self.speed_canvas = tk.Canvas(
            speed,
            width=self.BRIGHTNESS_BAR_WIDTH,
            height=16,
            bg="#ffffff",
            highlightthickness=0,
        )
        self.speed_canvas.pack(side="left", fill="x", expand=True)
        self._draw_speed_bar()
        self.speed_canvas.bind("<Button-1>", self._on_speed_click)
        self.speed_canvas.bind("<B1-Motion>", self._on_speed_click)

        quick = tk.Frame(section, bg="#ffffff", pady=1)
        quick.pack(fill="x")
        tk.Label(quick, text="快捷选色", bg="#ffffff", font=(self.FONT_FAMILY, 10, "bold")).pack(side="left")
        for color in self.QUICK_COLORS:
            swatch = tk.Label(
                quick,
                text="●",
                fg=f"#{color}",
                bg="#ffffff",
                font=("Segoe UI Symbol", 18),
                cursor="hand2",
            )
            swatch.pack(side="left", padx=5)
            swatch.bind("<Button-1>", lambda _e, c=color: self.set_quick_color(c))

        mirror = tk.Frame(section, bg="#ffffff", pady=2)
        mirror.pack(fill="x")
        tk.Label(mirror, text="左右同色", bg="#ffffff", font=(self.FONT_FAMILY, 10, "bold")).pack(side="left")
        self.mirror_switch_canvas = tk.Canvas(mirror, width=42, height=22, bg="#ffffff", highlightthickness=0, bd=0, cursor="hand2")
        self.mirror_switch_canvas.pack(side="right")
        self.mirror_switch_canvas.create_oval(1, 1, 21, 21, tags="mirror_track", fill="#d8dde6", outline="#d8dde6")
        self.mirror_switch_canvas.create_rectangle(11, 1, 31, 21, tags="mirror_track", fill="#d8dde6", outline="#d8dde6")
        self.mirror_switch_canvas.create_oval(21, 1, 41, 21, tags="mirror_track", fill="#d8dde6", outline="#d8dde6")
        self.mirror_switch_thumb = self.mirror_switch_canvas.create_oval(3, 2, 21, 20, fill="#ffffff", outline="#ffffff")
        self.mirror_switch_canvas.bind("<Button-1>", self._toggle_mirror)
        self._refresh_mirror_switch()

        tk.Label(section, text="点击灯珠可填充颜色，点空白处可取消选择", bg="#ffffff", fg="#666666", font=(self.FONT_FAMILY, 10)).pack(pady=(2, 2))

        canvas_wrap = tk.Frame(section, bg="#ffffff")
        canvas_wrap.pack()
        self.controller_canvas = tk.Canvas(canvas_wrap, width=450, height=228, bg="#f8fafc", highlightthickness=0)
        self.controller_canvas.pack()
        self.controller_canvas.bind("<Button-1>", self._on_led_canvas_click)
        self._draw_led_nodes()

        select_row = tk.Frame(section, bg="#ffffff", pady=1)
        select_row.pack()
        tk.Button(select_row, text="全部选择", width=9, command=self.select_all_leds, bd=0, bg="#ececec", relief="flat", font=(self.FONT_FAMILY, 9)).pack(side="left", padx=6)
        tk.Button(select_row, text="编辑多色", width=9, command=self.open_selected_palette_editor, bd=0, bg="#ececec", relief="flat", font=(self.FONT_FAMILY, 9)).pack(side="left", padx=6)
        self.palette_brush_button = tk.Button(select_row, text="多色格式刷", width=10, command=self.toggle_palette_brush, bd=0, bg="#ececec", relief="flat", font=(self.FONT_FAMILY, 9))
        self.palette_brush_button.pack(side="left", padx=6)
        tk.Label(section, textvariable=self.palette_brush_status_var, bg="#ffffff", fg="#666666", font=(self.FONT_FAMILY, 8)).pack(pady=(2, 0))
        self._update_palette_brush_ui()

        current = tk.Frame(section, bg="#ffffff", pady=2)
        current.pack(fill="x")
        tk.Label(current, text="当前颜色", bg="#ffffff", font=(self.FONT_FAMILY, 10, "bold")).pack(side="left")
        self.current_color_swatch = tk.Canvas(current, width=26, height=26, bg="#ffffff", highlightthickness=0)
        self.current_color_swatch.pack(side="right", padx=(8, 0))
        self.current_color_swatch.create_oval(2, 2, 24, 24, fill="#00ffff", outline="")
        tk.Label(current, text="#", bg="#ffffff", fg="#666666", font=(self.FONT_FAMILY, 10, "bold")).pack(side="right", padx=(0, 2))
        self.current_hex_entry = tk.Entry(
            current,
            textvariable=self.current_hex_var,
            width=8,
            justify="center",
            bd=0,
            bg="#f3f4f6",
            fg="#111111",
            insertbackground="#111111",
            relief="flat",
            font=(self.FONT_FAMILY, 10),
        )
        self.current_hex_entry.pack(side="right")
        self.current_hex_entry.bind("<Return>", self._on_hex_entry_commit)
        self.current_hex_entry.bind("<FocusOut>", self._on_hex_entry_commit)

        self.hue_canvas = tk.Canvas(section, width=self.COLOR_BAR_WIDTH, height=16, bg="#ffffff", highlightthickness=0)
        self.hue_canvas.pack(pady=(2, 1))
        self._draw_hue_bar()
        self.hue_canvas.bind("<Button-1>", self._on_hue_click)
        self.hue_canvas.bind("<B1-Motion>", self._on_hue_click)

        self.sat_canvas = tk.Canvas(section, width=self.COLOR_BAR_WIDTH, height=16, bg="#ffffff", highlightthickness=0)
        self.sat_canvas.pack(pady=(2, 2))
        self.sat_canvas.bind("<Button-1>", self._on_sat_click)
        self.sat_canvas.bind("<B1-Motion>", self._on_sat_click)
        self._draw_sat_bar()

        actions = tk.Frame(section, bg="#ffffff", pady=4)
        actions.pack(fill="x")
        tk.Button(actions, text="全部关闭", command=self.set_leds_all_off, bd=0, bg="#ededed", padx=11, pady=5, font=(self.FONT_FAMILY, 9)).pack(side="left")
        tk.Button(actions, text="应用", command=self.apply_14_led_effects, bd=0, bg="#111111", fg="#ffffff", padx=15, pady=7, font=(self.FONT_FAMILY, 9, "bold")).pack(side="right")

    def _build_mode_light_section(self, parent: tk.Widget):
        section = tk.Frame(parent, bg="#ffffff", padx=12, pady=6)
        section.pack(fill="x")
        tk.Label(section, text="模式灯颜色", bg="#ffffff", font=(self.FONT_FAMILY, 10, "bold")).grid(row=0, column=0, columnspan=4, sticky="w")
        tk.Label(section, text="Switch", bg="#ffffff", font=(self.FONT_FAMILY, 9)).grid(row=1, column=0, sticky="w", pady=(6, 0))
        tk.Label(section, text="PS", bg="#ffffff", font=(self.FONT_FAMILY, 9)).grid(row=2, column=0, sticky="w", pady=(4, 0))
        tk.Label(section, text="Xbox", bg="#ffffff", font=(self.FONT_FAMILY, 9)).grid(row=3, column=0, sticky="w", pady=(4, 0))
        for key, row, color_var, pady in [
            ("switch", 1, self.switch_color_var, (6, 0)),
            ("ps", 2, self.ps_color_var, (4, 0)),
            ("xbox", 3, self.xbox_color_var, (4, 0)),
        ]:
            dot_canvas = tk.Canvas(section, width=18, height=18, bg="#ffffff", highlightthickness=0, bd=0, cursor="hand2")
            dot_canvas.grid(row=row, column=1, padx=(2, 6), pady=pady)
            dot_item = dot_canvas.create_oval(2, 2, 16, 16, fill="#c7c7c7", outline="#111111", width=1)
            dot_canvas.bind("<Button-1>", lambda _e, v=color_var: self.pick_color_into(v))
            dot_canvas.tag_bind(dot_item, "<Button-1>", lambda _e, v=color_var: self.pick_color_into(v))
            self.mode_light_dots[key] = (dot_canvas, dot_item)
        tk.Entry(section, textvariable=self.switch_color_var, width=8, bd=0, bg="#f1f1f1", justify="center", font=(self.FONT_FAMILY, 9)).grid(row=1, column=2, padx=(0, 6), pady=(6, 0))
        tk.Entry(section, textvariable=self.ps_color_var, width=8, bd=0, bg="#f1f1f1", justify="center", font=(self.FONT_FAMILY, 9)).grid(row=2, column=2, padx=(0, 6), pady=(4, 0))
        tk.Entry(section, textvariable=self.xbox_color_var, width=8, bd=0, bg="#f1f1f1", justify="center", font=(self.FONT_FAMILY, 9)).grid(row=3, column=2, padx=(0, 6), pady=(4, 0))
        section.grid_columnconfigure(3, weight=1)
        tk.Button(section, text="应用", command=self.apply_all_custom_colors, bd=0, bg="#111111", fg="#ffffff", padx=11, pady=6, font=(self.FONT_FAMILY, 8, "bold")).grid(row=1, column=3, rowspan=3, padx=(10, 0), sticky="e")

    def _build_log_area(self, parent: tk.Widget):
        section = tk.Frame(parent, bg="#ffffff", padx=12, pady=4)
        section.pack(fill="x", expand=False)
        tk.Label(section, text="发送日志", bg="#ffffff", font=(self.FONT_FAMILY, 10, "bold")).pack(anchor="w")
        self.output = tk.Text(section, width=26, height=10, wrap="word", bd=0, bg="#f7f7f7", font=(self.FONT_FAMILY, 8))
        self.output.pack(fill="x", expand=False, pady=(4, 0))
        self.output.configure(state="disabled")

    def _draw_led_nodes(self):
        self.led_items.clear()
        self.led_palette_preview_items.clear()
        for idx, (x, y) in enumerate(self.LED_POSITIONS):
            border_item = self.controller_canvas.create_text(
                x,
                y,
                text="●",
                fill="#111111",
                font=("Segoe UI Symbol", 21),
                state="hidden",
            )
            fill_item = self.controller_canvas.create_text(
                x,
                y,
                text="●",
                fill="#cccccc",
                font=("Segoe UI Symbol", 17),
            )
            self.controller_canvas.tag_bind(border_item, "<Button-1>", lambda _e, i=idx: self.on_led_click(i))
            self.controller_canvas.tag_bind(border_item, "<Double-Button-1>", lambda _e, i=idx: self.open_led_palette_editor(i))
            self.controller_canvas.tag_bind(fill_item, "<Button-1>", lambda _e, i=idx: self.on_led_click(i))
            self.controller_canvas.tag_bind(fill_item, "<Double-Button-1>", lambda _e, i=idx: self.open_led_palette_editor(i))
            self.led_items.append((border_item, fill_item))

            preview_items: list[int] = []
            for dx, dy in self._palette_offsets_for_led(idx):
                preview_item = self.controller_canvas.create_text(
                    x + dx,
                    y + dy,
                    text="●",
                    fill="#f8fafc",
                    font=("Segoe UI Symbol", 7),
                    state="hidden",
                )
                self.controller_canvas.tag_bind(preview_item, "<Button-1>", lambda _e, i=idx: self.on_led_click(i))
                self.controller_canvas.tag_bind(preview_item, "<Double-Button-1>", lambda _e, i=idx: self.open_led_palette_editor(i))
                preview_items.append(preview_item)
            self.led_palette_preview_items.append(preview_items)

    def _draw_hue_bar(self):
        self.hue_canvas.delete("all")
        width = self.COLOR_BAR_WIDTH
        for i in range(width):
            hue = i / width
            r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
            color = f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"
            self.hue_canvas.create_line(i, 2, i, 14, fill=color)
        self.hue_marker = self.hue_canvas.create_rectangle(0, 0, 8, 16, outline="#4b5563", width=2)

    def _draw_brightness_bar(self):
        self.brightness_canvas.delete("all")
        width = self.BRIGHTNESS_BAR_WIDTH
        start_rgb = (44, 48, 58)
        end_rgb = (255, 138, 61)
        for i in range(width):
            ratio = i / max(width - 1, 1)
            r = round(start_rgb[0] + (end_rgb[0] - start_rgb[0]) * ratio)
            g = round(start_rgb[1] + (end_rgb[1] - start_rgb[1]) * ratio)
            b = round(start_rgb[2] + (end_rgb[2] - start_rgb[2]) * ratio)
            color = f"#{r:02x}{g:02x}{b:02x}"
            self.brightness_canvas.create_line(i, 2, i, 14, fill=color)
        self.brightness_marker = self.brightness_canvas.create_rectangle(0, 0, 8, 16, outline="#4b5563", width=2)
        self._update_brightness_marker()

    def _draw_speed_bar(self):
        self.speed_canvas.delete("all")
        width = self.BRIGHTNESS_BAR_WIDTH
        start_rgb = (34, 76, 122)
        end_rgb = (55, 210, 255)
        for i in range(width):
            ratio = i / max(width - 1, 1)
            r = round(start_rgb[0] + (end_rgb[0] - start_rgb[0]) * ratio)
            g = round(start_rgb[1] + (end_rgb[1] - start_rgb[1]) * ratio)
            b = round(start_rgb[2] + (end_rgb[2] - start_rgb[2]) * ratio)
            color = f"#{r:02x}{g:02x}{b:02x}"
            self.speed_canvas.create_line(i, 2, i, 14, fill=color)
        self.speed_marker = self.speed_canvas.create_rectangle(0, 0, 8, 16, outline="#4b5563", width=2)
        self._update_speed_marker()

    def _draw_sat_bar(self):
        self.sat_canvas.delete("all")
        width = self.COLOR_BAR_WIDTH
        hue = self.hue_var.get() / 360
        for i in range(width):
            sat = i / width
            r, g, b = colorsys.hsv_to_rgb(hue, sat, 1.0)
            color = f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"
            self.sat_canvas.create_line(i, 2, i, 14, fill=color)
        self.sat_marker = self.sat_canvas.create_rectangle(0, 0, 8, 16, outline="#4b5563", width=2)

    def _update_picker_markers(self):
        x = round(self.hue_var.get() / 360 * self.COLOR_BAR_WIDTH)
        self.hue_canvas.coords(self.hue_marker, x - 4, 0, x + 4, 16)
        sx = round(self.sat_var.get() / 100 * self.COLOR_BAR_WIDTH)
        self.sat_canvas.coords(self.sat_marker, sx - 4, 0, sx + 4, 16)

    def _update_brightness_marker(self):
        x = round(self.brightness_var.get() / 100 * self.BRIGHTNESS_BAR_WIDTH)
        self.brightness_canvas.coords(self.brightness_marker, x - 4, 0, x + 4, 16)

    def _update_speed_marker(self):
        x = round(self.led_speed_var.get() / 100 * self.BRIGHTNESS_BAR_WIDTH)
        self.speed_canvas.coords(self.speed_marker, x - 4, 0, x + 4, 16)

    def _sync_picker_from_hex(self, color_hex: str):
        color_hex = _normalize_rgb_hex(color_hex)
        r = int(color_hex[0:2], 16) / 255
        g = int(color_hex[2:4], 16) / 255
        b = int(color_hex[4:6], 16) / 255
        h, s, _v = colorsys.rgb_to_hsv(r, g, b)
        self._updating_picker = True
        self.hue_var.set(round(h * 360) % 360)
        self.sat_var.set(round(s * 100))
        self.current_color_var.set(color_hex)
        self.current_hex_var.set(color_hex)
        self.current_color_swatch.itemconfigure(1, fill=f"#{color_hex}")
        self._draw_sat_bar()
        self._update_picker_markers()
        self._updating_picker = False

    def _compute_picker_color(self) -> str:
        h = self.hue_var.get() / 360
        s = self.sat_var.get() / 100
        r, g, b = colorsys.hsv_to_rgb(h, s, 1.0)
        return f"{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

    def _set_current_color(self, color_hex: str, apply_selection: bool = True):
        color_hex = _normalize_rgb_hex(color_hex)
        self._sync_picker_from_hex(color_hex)
        if apply_selection and self.selected_leds:
            self.apply_color_to_leds(self.selected_leds, color_hex)

    def _on_hue_click(self, event):
        self.hue_var.set(_clamp_percent(event.x / self.COLOR_BAR_WIDTH * 100) * 360 // 100)
        color = self._compute_picker_color()
        self._set_current_color(color)

    def _on_sat_click(self, event):
        self.sat_var.set(max(0, min(100, round(event.x / self.COLOR_BAR_WIDTH * 100))))
        color = self._compute_picker_color()
        self._set_current_color(color)

    def _on_brightness_click(self, event):
        self.brightness_var.set(max(0, min(100, round(event.x / self.BRIGHTNESS_BAR_WIDTH * 100))))
        self._on_brightness_change()

    def _on_speed_click(self, event):
        self.led_speed_var.set(max(0, min(100, round(event.x / self.BRIGHTNESS_BAR_WIDTH * 100))))
        self._on_speed_change()

    def _on_hex_entry_commit(self, _event=None):
        try:
            self._set_current_color(self.current_hex_var.get(), apply_selection=True)
        except Exception as e:
            messagebox.showerror("颜色无效", str(e))

    def _on_brightness_change(self, _value=None):
        self.brightness_value_label.configure(text=str(self.brightness_var.get()))
        self._update_brightness_marker()

    def _on_speed_change(self, _value=None):
        self.speed_value_label.configure(text=str(self.led_speed_var.get()))
        self._update_speed_marker()

    def set_effect(self, effect: str):
        self.led_effect_var.set(effect)
        self._update_effect_buttons()

    def _update_effect_buttons(self):
        for effect, btn in self.effect_buttons.items():
            active = self.led_effect_var.get() == effect
            btn.configure(
                bg="#262626" if active else "#f1f1f1",
                fg="#ffffff" if active else "#4b5563",
                activebackground="#262626" if active else "#f1f1f1",
                activeforeground="#ffffff" if active else "#4b5563",
            )

    def _append(self, s: str):
        self.output.configure(state="normal")
        self.output.insert("end", s)
        self.output.see("end")
        self.output.configure(state="disabled")

    def _run(self, label: str, hex_payload: str, show_error_dialog: bool = True) -> bool:
        self._append(f"\n[{label}] 发送中...\n")
        try:
            written = run_send_hid(hex_payload)
        except Exception as e:
            if show_error_dialog:
                messagebox.showerror("执行失败", str(e))
            self._append(f"错误：{e}\n")
            return False
        self._append(f"write bytes: {written}\n")
        return True

    def _send_sequence(self, items: list[tuple[str, str]], success_message: str = "", show_success_dialog: bool = True, show_error_dialog: bool = True) -> bool:
        for label, _payload in items:
            self._append(f"\n[{label}] 排队发送\n")
        try:
            results = run_send_hid_sequence([payload for _label, payload in items])
        except Exception as e:
            if show_error_dialog:
                messagebox.showerror("执行失败", str(e))
            self._append(f"错误：{e}\n")
            return False
        for (label, _payload), written in zip(items, results):
            self._append(f"[{label}] write bytes: {written}\n")
        if show_success_dialog and success_message:
            messagebox.showinfo("成功", success_message)
        return True

    def _normalize_program_name(self, value: str) -> str:
        text = (value or "").strip().strip('"').lower()
        if not text:
            return ""
        return Path(text).name.lower() if any(sep in text for sep in ("/", "\\")) else text

    def _iter_profile_program_names(self, value: str) -> list[str]:
        raw = (value or "").replace("\n", ";").replace(",", ";")
        names: list[str] = []
        for part in raw.split(";"):
            normalized = self._normalize_program_name(part)
            if normalized and normalized not in names:
                names.append(normalized)
        return names

    def _get_running_process_names(self) -> set[str]:
        startupinfo = None
        creationflags = 0
        if hasattr(subprocess, "STARTUPINFO") and hasattr(subprocess, "STARTF_USESHOWWINDOW"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW

        completed = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="ignore",
            check=False,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "tasklist 执行失败")
        process_names: set[str] = set()
        for line in completed.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith('"') and '","' in line:
                image_name = line.split('","', 1)[0].strip('"')
            else:
                image_name = line.split(",", 1)[0].strip().strip('"')
            normalized = self._normalize_program_name(image_name)
            if normalized:
                process_names.add(normalized)
        return process_names

    def _find_matching_profile_name(self, running_processes: set[str]) -> str:
        for name, profile in self.profiles.items():
            program_names = self._iter_profile_program_names(str(profile.get("program", "")))
            if program_names and any(program_name in running_processes for program_name in program_names):
                return name
        return ""

    def _apply_profile_to_device(self, source_name: str = "") -> bool:
        mode_ok = self.apply_all_custom_colors(show_error_dialog=False)
        led_ok = self.apply_14_led_effects(show_success_dialog=False, show_error_dialog=False)
        if mode_ok and led_ok:
            label = f"（{source_name}）" if source_name else ""
            self._append(f"\n[自动关联] 已自动应用配置{label}\n")
            return True
        self._append(f"\n[自动关联] 自动应用失败：{source_name or '未命名配置'}\n")
        return False

    def _schedule_auto_profile_poll(self, delay_ms: int | None = None):
        if self._auto_profile_job is not None:
            try:
                self.after_cancel(self._auto_profile_job)
            except Exception:
                pass
        self._auto_profile_job = self.after(delay_ms or self._auto_profile_poll_ms, self._poll_auto_profile)

    def _cancel_auto_profile_poll(self):
        if self._auto_profile_job is None:
            return
        try:
            self.after_cancel(self._auto_profile_job)
        except Exception:
            pass
        self._auto_profile_job = None

    def _trigger_auto_profile_check(self):
        self._schedule_auto_profile_poll(1)

    def _poll_auto_profile(self):
        self._auto_profile_job = None
        if self._auto_profile_worker_running:
            self._schedule_auto_profile_poll()
            return
        self._auto_profile_worker_running = True
        threading.Thread(target=self._poll_auto_profile_worker, daemon=True).start()

    def _poll_auto_profile_worker(self):
        try:
            matched_name = self._find_matching_profile_name(self._get_running_process_names())
            error = None
        except Exception as e:
            matched_name = ""
            error = str(e)

        self.after(0, lambda: self._finish_auto_profile_poll(matched_name, error))

    def _finish_auto_profile_poll(self, matched_name: str, error: str | None):
        self._auto_profile_worker_running = False
        if error:
            self.auto_profile_status_var.set(f"自动关联：检测失败，{error}")
            self._schedule_auto_profile_poll()
            return

        if matched_name:
            self.auto_profile_status_var.set(f"自动关联：已检测到 {matched_name} 对应程序，运行中")
            if matched_name != self._auto_profile_last_match:
                profile = self.profiles.get(matched_name)
                if profile is not None:
                    self.profile_selected_var.set(matched_name)
                    self.profile_name_var.set(matched_name)
                    self._apply_profile(profile)
                    if self._apply_profile_to_device(matched_name):
                        self._auto_profile_last_match = matched_name
        else:
            has_binding = any(self._iter_profile_program_names(str(profile.get("program", ""))) for profile in self.profiles.values())
            self.auto_profile_status_var.set("自动关联：未检测到关联程序" if has_binding else "自动关联：未配置程序")
            self._auto_profile_last_match = ""

        self._schedule_auto_profile_poll()

    def pick_color_into(self, var: tk.StringVar, parent: tk.Misc | None = None):
        _rgb, hex_color = colorchooser.askcolor(title="选择灯颜色", parent=parent)
        if not hex_color:
            if parent is not None and parent.winfo_exists():
                parent.lift()
                parent.focus_force()
            return
        var.set(hex_color.lstrip("#"))
        if parent is not None and parent.winfo_exists():
            parent.lift()
            parent.focus_force()

    def set_quick_color(self, color_hex: str):
        self._set_current_color(color_hex, apply_selection=True)

    def _on_escape(self, _event=None):
        if self.palette_brush_active:
            self.cancel_palette_brush()

    def _build_tray_icon_image(self) -> Image.Image:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((6, 6, 58, 58), radius=15, fill=(17, 17, 17, 255))
        draw.rounded_rectangle((14, 16, 50, 48), radius=12, fill=(255, 138, 61, 255))
        draw.ellipse((20, 22, 44, 46), fill=(24, 255, 220, 255))
        draw.ellipse((28, 30, 36, 38), fill=(255, 255, 255, 255))
        return image

    def _show_tray_icon(self):
        if self._tray_visible:
            return

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", lambda _icon, _item: self.after(0, self._restore_from_tray), default=True),
            pystray.MenuItem("退出", lambda _icon, _item: self.after(0, self._exit_from_tray)),
        )
        tray_icon = pystray.Icon(
            "rainbow3_gui",
            self._build_tray_icon_image(),
            "RAINBOW3 灯效编辑",
            menu,
        )

        def setup(icon: pystray.Icon):
            icon.visible = True
            if self._tray_notification_shown:
                return
            try:
                icon.notify("程序已最小化到托盘，右键图标可恢复或退出。", "RAINBOW3 灯效编辑")
                self._tray_notification_shown = True
            except Exception:
                pass

        tray_icon.run_detached(setup=setup)
        self._tray_icon = tray_icon
        self._tray_visible = True

    def _hide_tray_icon(self):
        tray_icon = self._tray_icon
        self._tray_icon = None
        self._tray_visible = False
        if tray_icon is None:
            return
        try:
            tray_icon.stop()
        except Exception:
            pass

    def _minimize_to_tray(self):
        if self._is_exiting or self._tray_visible:
            return
        try:
            self._show_tray_icon()
        except Exception as e:
            self._append(f"\n[托盘] 初始化失败：{e}\n")
            self.deiconify()
            self.state("normal")
            self.lift()
            return
        self.withdraw()
        self._append("\n[托盘] 已最小化到系统托盘\n")

    def _restore_from_tray(self):
        if self._is_exiting:
            return
        self._hide_tray_icon()
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()
        self._append("[托盘] 主窗口已恢复\n")

    def _exit_from_tray(self):
        self._on_close()

    def _on_window_unmap(self, _event=None):
        if self._is_exiting or self._tray_visible:
            return
        try:
            if self.state() == "iconic":
                self.after(0, self._minimize_to_tray)
        except tk.TclError:
            return

    def _on_close(self):
        if self._is_exiting:
            return
        self._is_exiting = True
        self._cancel_auto_profile_poll()
        self._hide_tray_icon()
        self.destroy()

    def _mirrored_indices(self, indices: set[int]) -> set[int]:
        if not self.mirror_var.get():
            return set(indices)
        mirrored = set(indices)
        for idx in list(indices):
            partner = self.MIRROR_LED_MAP.get(idx)
            if partner is not None:
                mirrored.add(partner)
        return mirrored

    def apply_color_to_leds(self, indices: set[int], color_hex: str):
        targets = self._mirrored_indices(indices)
        for idx in targets:
            self.led_color_vars[idx].set(color_hex)
            self.led_palette_vars[idx][0].set(color_hex)
        self.refresh_led_palette_labels()
        self._refresh_led_canvas()

    def _palette_values_for_led(self, led_index: int) -> list[str]:
        return [var.get().strip() for var in self.led_palette_vars[led_index] if var.get().strip()]

    def _apply_palette_to_leds(self, indices: set[int], colors: list[str]):
        targets = self._mirrored_indices(indices)
        for idx in targets:
            for slot, palette_var in enumerate(self.led_palette_vars[idx]):
                palette_var.set(colors[slot] if slot < len(colors) else "")
            self.led_color_vars[idx].set(colors[0])
        self.refresh_led_palette_labels()
        self._refresh_led_canvas()

    def _update_palette_brush_ui(self):
        if self.palette_brush_button is None:
            return
        if self.palette_brush_active and self.palette_brush_source_index is not None:
            color_count = len(self.palette_brush_colors)
            self.palette_brush_button.configure(text="退出格式刷", bg="#111111", fg="#ffffff", activebackground="#111111", activeforeground="#ffffff")
            self.palette_brush_status_var.set(f"格式刷已就绪：源灯珠 {self.palette_brush_source_index + 1}，共 {color_count} 色，单击目标灯珠即可复制，Esc 可退出")
        else:
            self.palette_brush_button.configure(text="多色格式刷", bg="#ececec", fg="#111111", activebackground="#ececec", activeforeground="#111111")
            self.palette_brush_status_var.set("选中 1 颗灯珠后可启动格式刷，连续复制它的多色配置")

    def cancel_palette_brush(self):
        self.palette_brush_active = False
        self.palette_brush_source_index = None
        self.palette_brush_colors = []
        self._update_palette_brush_ui()

    def toggle_palette_brush(self):
        if self.palette_brush_active:
            self.cancel_palette_brush()
            return
        if len(self.selected_leds) != 1:
            messagebox.showinfo("提示", "先选中 1 颗源灯珠，再启动多色格式刷。")
            return
        source_index = next(iter(self.selected_leds))
        try:
            colors = collect_palette_colors(self._palette_values_for_led(source_index))
        except Exception as e:
            messagebox.showerror("无法启动格式刷", str(e))
            return
        self.palette_brush_active = True
        self.palette_brush_source_index = source_index
        self.palette_brush_colors = colors
        self._update_palette_brush_ui()
        self._append(f"[格式刷] 已复制灯珠 {source_index + 1} 的 {len(colors)} 色配置，点击目标灯珠即可粘贴。\n")

    def on_led_click(self, led_index: int):
        self.selected_leds = {led_index}
        if self.palette_brush_active and self.palette_brush_colors:
            self._apply_palette_to_leds({led_index}, self.palette_brush_colors)
            self._append(f"[格式刷] 已刷到灯珠 {led_index + 1}。\n")
            return
        self.apply_color_to_leds({led_index}, self.current_color_var.get())

    def _on_led_canvas_click(self, _event=None):
        if self.controller_canvas.find_withtag("current"):
            return
        self.clear_selection()

    def select_all_leds(self):
        self.selected_leds = set(range(14))
        self._refresh_led_canvas()

    def clear_selection(self):
        self.selected_leds.clear()
        self._refresh_led_canvas()

    def _refresh_led_canvas(self):
        highlighted = self._mirrored_indices(self.selected_leds)
        for idx, (border_item, fill_item) in enumerate(self.led_items):
            color = self.led_color_vars[idx].get()
            self.controller_canvas.itemconfigure(border_item, state="normal" if idx in highlighted else "hidden")
            self.controller_canvas.itemconfigure(fill_item, fill=f"#{_normalize_rgb_hex(color)}")

        for palette_vars, preview_items in zip(self.led_palette_vars, self.led_palette_preview_items):
            active_colors = []
            for palette_var in palette_vars:
                value = palette_var.get().strip()
                if not value:
                    continue
                try:
                    active_colors.append(_normalize_rgb_hex(value))
                except Exception:
                    continue

            for preview_item, color_hex in zip(preview_items, active_colors):
                self.controller_canvas.itemconfigure(preview_item, fill=f"#{color_hex}", state="normal")

            for preview_item in preview_items[len(active_colors):]:
                self.controller_canvas.itemconfigure(preview_item, state="hidden")

    def _scale_color(self, color_hex: str) -> str:
        color_hex = _normalize_rgb_hex(color_hex)
        brightness = self.brightness_var.get() / 100
        r = round(int(color_hex[0:2], 16) * brightness)
        g = round(int(color_hex[2:4], 16) * brightness)
        b = round(int(color_hex[4:6], 16) * brightness)
        return f"{r:02x}{g:02x}{b:02x}"

    def set_leds_all_off(self):
        for idx in range(14):
            self.led_color_vars[idx].set("000000")
            self.led_palette_vars[idx][0].set("000000")
            for palette_var in self.led_palette_vars[idx][1:]:
                palette_var.set("")
        self.refresh_led_palette_labels()
        self._refresh_led_canvas()

    def refresh_led_palette_labels(self):
        for palette_vars, label in zip(self.led_palette_vars, self.led_palette_labels):
            count = sum(1 for v in palette_vars if v.get().strip())
            label.configure(text=f"{max(count, 1)} 色")

    def open_led_palette_editor(self, led_index: int):
        self.open_palette_editor_for_indices({led_index})

    def open_selected_palette_editor(self):
        if not self.selected_leds:
            messagebox.showinfo("提示", "先点击手柄上的灯珠，再编辑多色。")
            return
        self.open_palette_editor_for_indices(self.selected_leds)

    def open_palette_editor_for_indices(self, led_indices: set[int]):
        base_index = min(led_indices)
        win = tk.Toplevel(self)
        win.title(f"LED 多色编辑 ({len(led_indices)} 颗)")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()
        frame = tk.Frame(win, padx=12, pady=12)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="填写 1-5 个颜色，只会按实际填写数量发包。", fg="#666666", font=(self.FONT_FAMILY, 9)).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        source_palette = self.led_palette_vars[base_index]
        edit_vars = [tk.StringVar(value=v.get()) for v in source_palette]
        for i, var in enumerate(edit_vars, start=1):
            tk.Label(frame, text=f"颜色 {i}：", font=(self.FONT_FAMILY, 9)).grid(row=i, column=0, sticky="w", pady=2)
            tk.Entry(frame, textvariable=var, width=12, font=(self.FONT_FAMILY, 9)).grid(row=i, column=1, sticky="w", pady=2)
            tk.Button(frame, text="选色", width=8, command=lambda v=var: self.pick_color_into(v, parent=win), font=(self.FONT_FAMILY, 9)).grid(row=i, column=2, padx=(6, 0), pady=2)

        def save_palette():
            try:
                colors = collect_palette_colors([v.get() for v in edit_vars])
            except Exception as e:
                messagebox.showerror("颜色无效", str(e), parent=win)
                return
            targets = self._mirrored_indices(led_indices)
            for idx in targets:
                for slot, var in enumerate(self.led_palette_vars[idx]):
                    var.set(colors[slot] if slot < len(colors) else "")
                self.led_color_vars[idx].set(colors[0])
            self.refresh_led_palette_labels()
            self._refresh_led_canvas()
            win.destroy()

        btns = tk.Frame(frame)
        btns.grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))
        tk.Button(btns, text="保存", command=save_palette, bd=0, bg="#111111", fg="#ffffff", padx=14, pady=6, font=(self.FONT_FAMILY, 9)).pack(side="left")

    def sync_main_colors_to_palette(self):
        for main_var, palette_vars in zip(self.led_color_vars, self.led_palette_vars):
            main = main_var.get().strip()
            if main and not palette_vars[0].get().strip():
                palette_vars[0].set(main)

    def apply_all_custom_colors(self, show_error_dialog: bool = True) -> bool:
        try:
            switch_norm = _normalize_rgb_hex(self.switch_color_var.get())
            ps_norm = _normalize_rgb_hex(self.ps_color_var.get())
            xbox_norm = _normalize_rgb_hex(self.xbox_color_var.get())
            payload = build_multi_color_hex(switch_norm, ps_norm, xbox_norm)
        except Exception as e:
            if show_error_dialog:
                messagebox.showerror("颜色无效", str(e))
            else:
                self._append(f"错误：{e}\n")
            return False
        self._append(f"\n[三色自定义] Switch={switch_norm} PS={ps_norm} Xbox={xbox_norm}\n")
        return self._run("三种模式自定义颜色", payload, show_error_dialog=show_error_dialog)

    def apply_14_led_effects(self, show_success_dialog: bool = True, show_error_dialog: bool = True) -> bool:
        try:
            colors = [_normalize_rgb_hex(var.get()) for var in self.led_color_vars]
            effect = self.led_effect_var.get()
            speed = _clamp_percent(self.led_speed_var.get())
            self.sync_main_colors_to_palette()
        except Exception as e:
            if show_error_dialog:
                messagebox.showerror("参数无效", str(e))
            else:
                self._append(f"错误：{e}\n")
            return False

        packets: list[tuple[str, str]] = []
        for display_idx, (color, palette_vars) in enumerate(zip(colors, self.led_palette_vars), start=1):
            actual_idx = self.DISPLAY_TO_ACTUAL_LED_INDEX[display_idx - 1]
            actual_led_no = actual_idx + 1
            mask = LED_MASKS[actual_idx]
            if effect == "breathing":
                palette = [self._scale_color(v.get()) for v in palette_vars if v.get().strip()]
                payload = build_led_multi_breathing_packet(mask, palette, speed_percent=speed)
                packets.append((f"14 灯 位置{display_idx} -> 实际{actual_led_no} 多色呼吸", payload))
            elif effect == "multi_breathing":
                palette = [self._scale_color(v.get()) for v in palette_vars if v.get().strip()]
                payload = build_led_multi_breathing_packet(mask, palette, speed_percent=speed, mode_byte=0x01)
                packets.append((f"14 灯 位置{display_idx} -> 实际{actual_led_no} 多色交替(模式1)", payload))
            elif effect == "gradient":
                palette = [self._scale_color(v.get()) for v in palette_vars if v.get().strip()]
                payload = build_led_multi_breathing_packet(mask, palette, speed_percent=speed, mode_byte=0x03)
                packets.append((f"14 灯 位置{display_idx} -> 实际{actual_led_no} 渐变(模式3)", payload))
            else:
                payload = build_led_packet(mask, self._scale_color(color), effect=effect, speed_percent=speed)
                packets.append((f"14 灯 位置{display_idx} -> 实际{actual_led_no} {self.EFFECT_LABELS[effect]}", payload))

        packets.append(("14 灯提交", LED_COMMIT_PACKET))
        self._append(f"\n[14 灯] 模式={self.EFFECT_LABELS[effect]} 速度={speed} 亮度={self.brightness_var.get()}\n")
        return self._send_sequence(packets, "14 颗灯珠效果已发送", show_success_dialog=show_success_dialog, show_error_dialog=show_error_dialog)


if __name__ == "__main__":
    App().mainloop()
