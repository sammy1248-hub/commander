import pyautogui
import numpy as np


class GestureController:
    def __init__(self):
        self.screen_w, self.screen_h = pyautogui.size()
        pyautogui.PAUSE = 0
        pyautogui.FAILSAFE = False

    def move_mouse(self, x, y):
        """x, y are normalized 0-1 coordinates from the camera frame."""
        target_x = np.interp(x, [0, 1], [0, self.screen_w])
        target_y = np.interp(y, [0, 1], [0, self.screen_h])
        pyautogui.moveTo(target_x, target_y)

    def press_key(self, key):
        if key == "left_click":
            pyautogui.leftClick()
            return
        if key == "right_click":
            pyautogui.rightClick()
            return

        # Maps internal names to pyautogui keys
        key_map = {
            "vol_up": "volumeup",
            "vol_down": "volumedown",
            "space": "space",
            "enter": "enter",
        }
        cmd = key_map.get(key, key)
        pyautogui.press(cmd)