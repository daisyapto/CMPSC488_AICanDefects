import tkinter as tk
import subprocess
import random

# Color Palette
BG_DARK = "#1e2430"
PANEL_BG = "#2a3242"
TEXT_WHITE = "#ffffff"
TEXT_MUTED = "#8e9aaf"

# Output configurations: (Key, Display Label, BCM GPIO, Active Color, Text Color)
OUTPUTS = [
    ("green", "GREEN LIGHT", 17, "#2ecc71", "#ffffff"),
    ("yellow", "YELLOW LIGHT", 27, "#f39c12", "#ffffff"),
    ("red", "RED LIGHT", 22, "#e74c3c", "#ffffff"),
    ("buzzer", "BUZZER ALARM", 23, "#9b59b6", "#ffffff"),
]

class IndustrialController:
    def __init__(self, root):
        self.root = root
        self.root.title("Can Inspection Control Panel")
        self.root.geometry("480x720")
        self.root.configure(bg=BG_DARK)
        self.root.resizable(False, False)

        self.states = {}
        self.buttons = {}
        self.indicators = {}

        # Top Header
        header_frame = tk.Frame(root, bg=BG_DARK)
        header_frame.pack(fill="x", pady=(20, 10))

        tk.Label(
            header_frame,
            text="LINE INSPECTION PANEL",
            font=("Helvetica", 18, "bold"),
            fg=TEXT_WHITE,
            bg=BG_DARK,
        ).pack()

        tk.Label(
            header_frame,
            text="Raspberry Pi GPIO Output Test",
            font=("Helvetica", 10),
            fg=TEXT_MUTED,
            bg=BG_DARK,
        ).pack(pady=(2, 0))

        # Main Status Banner
        self.status_banner = tk.Label(
            root,
            text="STATUS: READY / IDLE",
            font=("Helvetica", 13, "bold"),
            fg=TEXT_WHITE,
            bg="#34495e",
            pady=12,
            relief=tk.FLAT,
        )
        self.status_banner.pack(fill="x", padx=24, pady=10)

        # Output Cards Frame
        cards_frame = tk.Frame(root, bg=BG_DARK)
        cards_frame.pack(fill="both", expand=True, padx=24, pady=5)

        for key, label, pin, active_color, text_color in OUTPUTS:
            # Initialize hardware state: LOW (0V)
            subprocess.run(["pinctrl", "set", str(pin), "op", "dl"], check=False)
            self.states[key] = False

            card = tk.Frame(cards_frame, bg=PANEL_BG, bd=0, padx=12, pady=10)
            card.pack(fill="x", pady=6)

            # Left side: Indicator pill + Label
            left_sub = tk.Frame(card, bg=PANEL_BG)
            left_sub.pack(side="left")

            ind = tk.Label(
                left_sub,
                text="●",
                font=("Helvetica", 20),
                fg="#4f5b6e",
                bg=PANEL_BG,
                width=2,
            )
            ind.pack(side="left")
            self.indicators[key] = ind

            lbl_frame = tk.Frame(left_sub, bg=PANEL_BG)
            lbl_frame.pack(side="left", padx=8)

            tk.Label(
                lbl_frame,
                text=label,
                font=("Helvetica", 12, "bold"),
                fg=TEXT_WHITE,
                bg=PANEL_BG,
                anchor="w",
            ).pack(fill="x")

            tk.Label(
                lbl_frame,
                text=f"GPIO {pin}",
                font=("Helvetica", 9),
                fg=TEXT_MUTED,
                bg=PANEL_BG,
                anchor="w",
            ).pack(fill="x")

            # Right side: Large Toggle Button
            btn = tk.Button(
                card,
                text="OFF",
                font=("Helvetica", 11, "bold"),
                bg="#3b4455",
                fg=TEXT_MUTED,
                activebackground="#4a556b",
                activeforeground=TEXT_WHITE,
                width=9,
                height=2,
                relief=tk.FLAT,
                bd=0,
                cursor="hand2",
                command=lambda k=key, p=pin, c=active_color, tc=text_color: self.toggle(k, p, c, tc),
            )
            btn.pack(side="right")
            self.buttons[key] = btn

        # Simulation Footer Section
        sim_frame = tk.Frame(root, bg=BG_DARK)
        sim_frame.pack(fill="x", padx=24, pady=(10, 24))

        self.sim_btn = tk.Button(
            sim_frame,
            text="⚡ SIMULATE RANDOM CAN DETECTION",
            font=("Helvetica", 13, "bold"),
            bg="#007acc",
            fg=TEXT_WHITE,
            activebackground="#005999",
            activeforeground=TEXT_WHITE,
            height=2,
            relief=tk.FLAT,
            bd=0,
            cursor="hand2",
            command=self.simulate_inspection,
        )
        self.sim_btn.pack(fill="x")

    def set_hardware(self, key, pin, is_on, active_color, text_color):
        mode = "dh" if is_on else "dl"
        subprocess.run(["pinctrl", "set", str(pin), "op", mode], check=False)
        self.states[key] = is_on

        btn = self.buttons[key]
        ind = self.indicators[key]

        if is_on:
            btn.config(text="ON", bg=active_color, fg=text_color)
            ind.config(fg=active_color)
        else:
            btn.config(text="OFF", bg="#3b4455", fg=TEXT_MUTED)
            ind.config(fg="#4f5b6e")

    def toggle(self, key, pin, active_color, text_color):
        new_state = not self.states[key]
        self.set_hardware(key, pin, new_state, active_color,
