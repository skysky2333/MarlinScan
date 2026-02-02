from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class QuickTabMixin:
    def _build_quick_tab(self, parent: ttk.Frame) -> None:
        info = ttk.LabelFrame(parent, text="Info / Status", padding=10)
        info.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(info, text="M115 (Info)", command=lambda: self._send("M115")).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(info, text="M105 (Temps)", command=lambda: self._send("M105")).grid(
            row=0, column=1, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(info, text="M114 (Pos)", command=lambda: self._send("M114")).grid(
            row=0, column=2, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(info, text="M119 (Endstops)", command=lambda: self._send("M119")).grid(
            row=0, column=3, sticky=tk.W, pady=(0, 6)
        )
        ttk.Button(info, text="M503 (Report)", command=lambda: self._send("M503")).grid(
            row=1, column=0, sticky=tk.W
        )

        motion = ttk.LabelFrame(parent, text="Motion / Safety", padding=10)
        motion.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        ttk.Button(motion, text="Motors On (M17)", command=lambda: self._send("M17")).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(motion, text="Motors Off (M84)", command=lambda: self._send("M84")).grid(
            row=0, column=1, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(motion, text="Home All (G28)", command=lambda: self.home(None)).grid(
            row=0, column=2, sticky=tk.W, padx=(0, 6), pady=(0, 6)
        )
        ttk.Button(motion, text="Auto Level (G29)", command=self.auto_level_confirmed).grid(
            row=0, column=3, sticky=tk.W, pady=(0, 6)
        )

        ttk.Button(motion, text="EMERGENCY STOP (M112)", command=self.estop_confirmed).grid(
            row=1, column=0, sticky=tk.W, padx=(0, 6)
        )
        ttk.Button(motion, text="Reset (M999)", command=lambda: self._send("M999")).grid(
            row=1, column=1, sticky=tk.W, padx=(0, 6)
        )
        ttk.Button(motion, text="Mesh On (M420 S1)", command=lambda: self._send("M420 S1")).grid(
            row=1, column=2, sticky=tk.W, padx=(0, 6)
        )
        ttk.Button(motion, text="Mesh Off (M420 S0)", command=lambda: self._send("M420 S0")).grid(
            row=1, column=3, sticky=tk.W
        )

