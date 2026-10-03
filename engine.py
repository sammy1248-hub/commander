import cv2
import os
import sys
import threading
import urllib.request
import urllib.error
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision


def _base_dir():
    """Directory to resolve bundled resources (model file) against. Under a
    PyInstaller-frozen build, bundled data lives in sys._MEIPASS, not next
    to this source file — checking sys.frozen is the standard way to
    support both a normal `python app.py` run and a packaged .exe."""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


MODEL_PATH = os.path.join(_base_dir(), "hand_landmarker.task")
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"

# Standard 21-point hand skeleton connections (fixed structure, not model-specific)
HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),        # thumb
    (0, 5), (5, 6), (6, 7), (7, 8),        # index
    (0, 9), (9, 10), (10, 11), (11, 12),   # middle
    (0, 13), (13, 14), (14, 15), (15, 16),  # ring
    (0, 17), (17, 18), (18, 19), (19, 20),  # pinky
    (5, 9), (9, 13), (13, 17),             # palm
]

FINGER_TIPS = [8, 12, 16, 20]   # index, middle, ring, pinky
FINGER_PIPS = [6, 10, 14, 18]
THUMB_TIP, THUMB_MCP, WRIST = 4, 2, 0
INDEX_TIP = 8

# Default fraction in from each camera-frame edge that counts as "fully
# reached the screen edge" for cursor mapping. Exposed as a runtime setting
# (HandEngine.cursor_margin) so the UI can adjust it live.
DEFAULT_CURSOR_MARGIN = 0.35
DEFAULT_CURSOR_SMOOTHING = 0.25  # higher = snappier, lower = smoother/laggier

# JPEG quality for the streamed preview frame (not the detection input).
STREAM_JPEG_QUALITY = 70

# Per-finger curl-detection leniency. A finger counts as "curled" once its
# tip distance to the wrist drops below (pip distance * ratio). Index and
# middle use a strict 1.0 (tip must genuinely pass the pip). Ring and pinky
# share tendons and rarely fold in as tightly as index/middle during a real
# peace sign, so they get a ~12% tolerance — this is where that leniency
# belongs (detection), not in classify_gesture, so gestures stay mutually
# exclusive instead of overlapping.
FINGER_CURL_RATIO = {
    FINGER_TIPS[0]: 1.00,  # index
    FINGER_TIPS[1]: 1.00,  # middle
    FINGER_TIPS[2]: 1.12,  # ring
    FINGER_TIPS[3]: 1.12,  # pinky
}


def _dist(a, b):
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


def _finger_curled(lm, tip_idx, pip_idx):
    # A finger is "curled" if its tip sits closer to the wrist than its
    # middle knuckle does (i.e. it has folded back toward the palm), with
    # a per-finger tolerance ratio (see FINGER_CURL_RATIO) applied to the
    # pip-distance side of the comparison.
    wrist = lm[WRIST]
    ratio = FINGER_CURL_RATIO.get(tip_idx, 1.00)
    return _dist(lm[tip_idx], wrist) < _dist(lm[pip_idx], wrist) * ratio


def _curls(lm):
    """True/False per finger (index, middle, ring, pinky): curled or not."""
    return [_finger_curled(lm, t, p) for t, p in zip(FINGER_TIPS, FINGER_PIPS)]


def is_pointer_pose(curls):
    """Index finger extended, everything else curled — this is the pose
    that moves the cursor. All three click/gesture shapes (left_click,
    right_click, and the fist-based volume gestures) require at least one
    of middle/ring/pinky to be extended, so requiring ALL THREE curled
    here keeps pointer mode in a completely separate bucket from every
    other gesture — not just usually different, but structurally
    impossible to overlap. That's what stops the cursor from continuing
    to track (or drifting) while you're holding a peace sign or a
    three-finger click.
    """
    index_curled, middle_curled, ring_curled, pinky_curled = curls
    return (not index_curled) and middle_curled and ring_curled and pinky_curled


def classify_gesture(lm, curls):
    """Classify a MediaPipe hand landmark list into one of our 6 gestures:
    open palm -> space, thumb up (on a fist) -> vol_up, thumb down -> vol_down,
    peace sign -> right_click, index+middle+ring extended (pinky curled)
    -> left_click.
    """
    index_curled, middle_curled, ring_curled, pinky_curled = curls
    curled_count = curls.count(True)

    if curled_count == 0:
        return "space"  # open palm — all four fingers extended

    if curled_count == 1:
        if pinky_curled:
            # Left click: index + middle + ring extended, pinky curled -
            # the only single-finger-curled combo mapped to anything.
            # Curling index or middle alone (with the rest extended) is
            # deliberately left unmapped ("None") rather than also firing
            # left_click, since those shapes aren't the gesture you're
            # going for here.
            return "left_click"
        return "None"  # index or middle curled alone — not mapped to anything

    if curled_count == 2:
        if not index_curled and not middle_curled and ring_curled and pinky_curled:
            return "right_click"  # peace sign — index + middle extended, ring + pinky curled
        return "None"  # some other two-finger combo — ambiguous, ignore

    if curled_count == 3:
        return "None"  # only one finger extended — not mapped to anything

    # All 4 fingers curled into a fist. Check the thumb to see if it's
    # pointing up or down, measured relative to the fist's own center and
    # size so it stays reliable regardless of hand distance/tilt.
    fist_center = lm[9]  # middle-finger knuckle
    hand_scale = _dist(lm[WRIST], fist_center)
    if hand_scale < 1e-6:
        return "None"

    thumb_offset = _dist(lm[THUMB_TIP], fist_center) / hand_scale
    if thumb_offset < 0.9:
        return "None"  # thumb tucked in — plain fist, not mapped to anything

    dy = (lm[THUMB_TIP].y - fist_center.y) / hand_scale
    if dy < -0.35:
        return "vol_up"    # thumb clearly above the fist
    elif dy > 0.35:
        return "vol_down"  # thumb clearly below the fist
    return "None"           # thumb out but direction unclear — ignore


class HandEngine:
    def __init__(self):
        self.cap = cv2.VideoCapture(0)
        if not self.cap.isOpened():
            self.cap.release()
            raise RuntimeError(
                "Could not access the camera (device 0). It may be in use "
                "by another application, or no camera is connected."
            )

        self.gesture = "None"
        self.pointer_active = False
        self.hand_detected = False

        # Runtime-adjustable settings (exposed via the API / settings panel).
        self.cursor_smoothing = DEFAULT_CURSOR_SMOOTHING
        self.cursor_margin = DEFAULT_CURSOR_MARGIN

        if not os.path.exists(MODEL_PATH):
            print("Downloading hand landmark model (first run only)...")
            try:
                urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
            except (urllib.error.URLError, OSError) as e:
                self.cap.release()
                raise RuntimeError(f"Could not download hand-tracking model: {e}") from e
            print("Model downloaded.")

        base_options = mp_python.BaseOptions(model_asset_path=MODEL_PATH)
        options = vision.HandLandmarkerOptions(
            base_options=base_options,
            num_hands=1,
            running_mode=vision.RunningMode.VIDEO,
            min_hand_detection_confidence=0.6,
            min_hand_presence_confidence=0.6,
            min_tracking_confidence=0.6,
        )
        self.landmarker = vision.HandLandmarker.create_from_options(options)
        self._frame_ts = 0

        # Cursor position, smoothed to avoid jitter. Starts centered.
        self.cursor_x = 0.5
        self.cursor_y = 0.5

        # Latest encoded preview frame, guarded by a lock since it's written
        # by the engine loop thread and read by the streaming endpoint.
        # frame_id increments on every new frame so /video_feed can tell a
        # genuinely new frame apart from "nothing changed yet" without
        # re-sending identical JPEG bytes.
        self._frame_lock = threading.Lock()
        self._latest_jpeg = None
        self._frame_id = 0

    def _remap_with_margin(self, v):
        margin = self.cursor_margin
        v = (v - margin) / (1 - 2 * margin)
        return min(max(v, 0.0), 1.0)

    def update(self):
        """Read + process one camera frame. Returns False when the camera
        feed ends (e.g. device disconnected); True otherwise. No GUI window
        is opened — the annotated frame is JPEG-encoded and stashed for the
        web UI's /video_feed stream instead."""
        success, frame = self.cap.read()
        if not success:
            return False
        frame = cv2.flip(frame, 1)
        h, w, _ = frame.shape

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

        self._frame_ts += 1
        result = self.landmarker.detect_for_video(mp_image, self._frame_ts)

        self.gesture = "None"
        self.pointer_active = False
        self.hand_detected = bool(result.hand_landmarks)

        if result.hand_landmarks:
            hand = result.hand_landmarks[0]

            # Draw skeleton
            pts = [(int(p.x * w), int(p.y * h)) for p in hand]
            for a, b in HAND_CONNECTIONS:
                cv2.line(frame, pts[a], pts[b], (0, 200, 255), 2)
            for x, y in pts:
                cv2.circle(frame, (x, y), 4, (0, 255, 0), -1)

            curls = _curls(hand)
            self.gesture = classify_gesture(hand, curls)

            fx, fy = int(hand[INDEX_TIP].x * w), int(hand[INDEX_TIP].y * h)

            if is_pointer_pose(curls):
                # Only update the cursor while in the dedicated pointer pose
                # (index finger alone, everything else curled). This keeps
                # it from drifting while you're holding a click/volume/space
                # gesture, so aiming and triggering never fight each other.
                self.pointer_active = True
                raw_x = self._remap_with_margin(hand[INDEX_TIP].x)
                raw_y = self._remap_with_margin(hand[INDEX_TIP].y)
                s = self.cursor_smoothing
                self.cursor_x += s * (raw_x - self.cursor_x)
                self.cursor_y += s * (raw_y - self.cursor_y)
                cv2.drawMarker(frame, (fx, fy), (255, 0, 255), cv2.MARKER_CROSS, 20, 2)
                cv2.putText(frame, "POINTER TRACKING", (10, h - 15),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 255), 2)
            else:
                # Cursor is frozen — show a dimmer marker so it's clear
                # tracking is paused, not broken.
                cv2.drawMarker(frame, (fx, fy), (120, 120, 120), cv2.MARKER_CROSS, 14, 1)
                cv2.putText(frame, "CURSOR LOCKED", (10, h - 15),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (120, 120, 120), 2)

            # Debug overlay: raw curl state (index, middle, ring, pinky) so
            # gesture-detection issues can be diagnosed straight from the
            # video feed without instrumenting anything else. Remove or
            # comment out once gestures are confirmed reliable.
            cv2.putText(frame, f"curls: {curls}", (10, h - 45),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

        label_color = {
            "space": (0, 200, 255),
            "vol_up": (0, 255, 0),
            "vol_down": (0, 0, 255),
            "left_click": (255, 165, 0),
            "right_click": (255, 0, 255),
            "None": (150, 150, 150),
        }[self.gesture]

        cv2.putText(
            frame,
            f"GESTURE: {self.gesture}",
            (10, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            label_color,
            2,
        )

        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, STREAM_JPEG_QUALITY])
        if ok:
            with self._frame_lock:
                self._latest_jpeg = buf.tobytes()
                self._frame_id += 1

        return True

    def get_jpeg_frame_and_id(self):
        """Returns (jpeg_bytes_or_None, frame_id). Callers can skip
        re-sending a frame whose id they've already seen."""
        with self._frame_lock:
            return self._latest_jpeg, self._frame_id

    def release(self):
        # Called once per start/stop cycle now (a fresh HandEngine is
        # created on every START), so this needs to be safe even if a
        # resource never finished initializing — never raise out of here,
        # since it always runs from a `finally` block.
        try:
            self.landmarker.close()
        except Exception:
            pass
        try:
            self.cap.release()
        except Exception:
            pass
        with self._frame_lock:
            self._latest_jpeg = None