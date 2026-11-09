import tkinter as tk
import subprocess

# Color Palette
BG_DARK = "#1e2430"
PANEL_BG = "#2a3242"
TEXT_WHITE = "#ffffff"
TEXT_MUTED = "#8e9aaf"

# Output definitions: (Key, Display Label, BCM GPIO, Active Color, Text Color)
OUTPUTS = [
    ("green", "GREEN LIGHT", 17, "#2ecc71", "#ffffff"),
    ("yellow", "YELLOW LIGHT", 27, "#f39c12", "#ffffff"),
    ("red", "RED LIGHT", 22, "#e74c3c", "#ffffff"),
    ("buzzer", "BUZZER ALARM", 23, "#9b59b6", "#ffffff"),
]

class OutputControllerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Inspection Output Controller")
        self.root.geometry("480x560")
        self.root.configure(bg=BG_DARK)
        self.root.resizable(False, False)

        self.states = {}
        self.buttons = {}
        self.indicators = {}

        # Header Section
        header_frame = tk.Frame(root, bg=BG_DARK)
        header_frame.pack(fill="x", pady=(24, 12))

        tk.Label(
            header_frame,
            text="LINE OUTPUT CONTROL",
            font=("Helvetica", 18, "bold"),
            fg=TEXT_WHITE,
            bg=BG_DARK,
        ).pack()

        tk.Label(
            header_frame,
            text="Raspberry Pi Output Override Panel",
            font=("Helvetica", 10),
            fg=TEXT_MUTED,
            bg=BG_DARK,
        ).pack(pady=(2, 0))

        # Main Status Banner
        self.status_banner = tk.Label(
            root,
            text="STATUS: ALL CHANNELS OFF",
            font=("Helvetica", 13, "bold"),
            fg=TEXT_WHITE,
            bg="#34495e",
            pady=12,
            relief=tk.FLAT,
        )
        self.status_banner.pack(fill="x", padx=24, pady=10)

        # Output Cards Frame
        cards_frame = tk.Frame(root, bg=BG_DARK)
        cards_frame.pack(fill="both", expand=True, padx=24, pady=10)

        for key, label, pin, active_color, text_color in OUTPUTS:
            self.states[key] = False

            card = tk.Frame(cards_frame, bg=PANEL_BG, bd=0, padx=14, pady=12)
            card.pack(fill="x", pady=6)

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

        # Initialize all pins to physical OFF state
        self.all_off()

    def set_hardware(self, key, pin, is_on, active_color, text_color):
        # Inverted logic: 'dl' (Ground) turns ON, 'dh' (3.3V) turns OFF
        mode = "dl" if is_on else "dh"
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
        self.set_hardware(key, pin, new_state, active_color, text_color)
        
        # Update banner based on active outputs
        any_active = any(self.states.values())
        if any_active:
            active_names = [name for k, name, p, c, tc in OUTPUTS if self.states[k]]
            self.status_banner.config(
                text=f"ACTIVE: {', '.join(active_names)}",
                bg="#2980b9",
            )
        else:
            self.status_banner.config(
                text="STATUS: ALL CHANNELS OFF",
                bg="#34495e",
            )

    def all_off(self):
        for key, label, pin, active_color, text_color in OUTPUTS:
            self.set_hardware(key, pin, False, active_color, text_color)

if __name__ == "__main__":
    root = tk.Tk()
    app = OutputControllerApp(root)
    root.mainloop()
