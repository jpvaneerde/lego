"""Map a measured piece colour to the nearest Rebrickable colour."""
import cv2
import numpy as np

# Solid colours common in modern sets; used when the catalog has no usage counts.
COMMON_IDS = {0, 1, 2, 4, 5, 6, 7, 8, 14, 15, 19, 25, 26, 27, 28, 70, 71, 72, 73, 84, 85, 191,
              226, 272, 288, 308, 320, 321, 322, 326, 378, 484, 1050}


class Palette:
    def __init__(self, rows: list[dict]):
        solid = [r for r in rows if not r.get("is_trans") and r.get("rgb") and r["id"] >= 0]
        counted = [r for r in solid if r.get("num_parts")]
        if counted:
            pool = [r for r in counted if r["num_parts"] >= 1500] or counted
        else:
            pool = [r for r in solid if r["id"] in COMMON_IDS] or solid
        self.rows = pool
        rgb = np.array([[int(r["rgb"][i:i + 2], 16) for i in (4, 2, 0)] for r in pool], np.uint8)  # BGR
        self.lab = cv2.cvtColor(rgb.reshape(-1, 1, 3).astype(np.float32) / 255.0,
                                cv2.COLOR_BGR2LAB).reshape(-1, 3)

    def nearest(self, lab: tuple, n: int = 3) -> list[dict]:
        if not self.rows:
            return []
        L, a, b = lab
        # Lightness is the least reliable under room lighting; weight it lower.
        d = np.sqrt(((self.lab[:, 0] - L) * 0.7) ** 2 + (self.lab[:, 1] - a) ** 2 + (self.lab[:, 2] - b) ** 2)
        idx = np.argsort(d)[:n]
        return [{"id": self.rows[i]["id"], "name": self.rows[i]["name"], "rgb": self.rows[i]["rgb"],
                 "distance": round(float(d[i]), 1)} for i in idx]
