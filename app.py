import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine import HandEngine
from controller import GestureController


def _base_dir() -> Path:
    """Same frozen-vs-source resolution as engine.py's _base_dir — kept as
    a separate copy (not a shared import) so each file works standalone if
    one is ever bundled without the other."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


BASE_DIR = _base_dir()
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Commander")

# This is a local desktop tool — the frontend is served by this same
# process, so cross-origin calls aren't needed. Origins are still listed
# (rather than "*") in case someone opens the UI from a different port
# during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8001",
        "http://localhost:8001",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

controller = GestureController()

# ---------------------------------------------------------------------------
# Engine lifecycle
#
# A HandEngine (camera + MediaPipe landmarker) is created fresh on every
# START and fully torn down on every STOP/ERROR, rather than living for the
# whole server process. That's what makes STOP -> START actually work: a
# MediaPipe landmarker and an OpenCV capture can't be reused once released.
#
# engine_state is the single source of truth the UI reads:
#   "offline"  - no camera/engine resources held
#   "starting" - thread launched, camera/model not confirmed ready yet
#   "active"   - frames are being processed
#   "stopping" - STOP requested, worker thread is unwinding
#   "error"    - camera or model failed; engine_error has the message
# ---------------------------------------------------------------------------
_engine_lock = threading.Lock()
_engine_thread: threading.Thread | None = None
_running_event = threading.Event()

engine: HandEngine | None = None
engine_state = "offline"
engine_error: str | None = None


class Settings(BaseModel):
    cursor_smoothing: float = 0.25
    cursor_margin: float = 0.35
    confirm_frames: int = 10  # size of the rolling window used to confirm a gesture


settings = Settings()

# Small ring buffer of recent events for the activity log panel.
activity_log: deque[dict] = deque(maxlen=50)


def _log(kind: str, value: str):
    activity_log.append({"time": time.strftime("%H:%M:%S"), "kind": kind, "value": value})


def _apply_settings_to_engine():
    if engine is not None:
        engine.cursor_smoothing = settings.cursor_smoothing
        engine.cursor_margin = settings.cursor_margin


class _GestureConfirmer:
    """Turns a noisy, frame-by-frame gesture reading into a stable,
    debounced signal using a rolling-window majority vote instead of
    "same value N frames in a row, reset hard on any mismatch".

    The old approach (a plain counter that reset to zero on a single
    differing frame, with a small fixed tolerance for "None" frames only)
    fell apart on real camera/lighting noise: a peace sign that reads as
    right_click/None/right_click/None/... in quick succession (as seen in
    testing) never accumulates enough consecutive matching frames to fire,
    even though right_click is clearly the dominant reading moment to
    moment.

    Instead, this keeps the last `window_size` raw readings and fires once
    a single gesture holds a strong majority of that window - so a scatter
    of dropped/misread frames no longer wipes out progress, only a
    genuinely different or genuinely absent gesture does. Once fired, the
    same gesture won't re-fire until the window's majority moves away from
    it first (so holding a pose doesn't spam the action every frame).
    """

    MAJORITY_RATIO = 0.7  # fraction of the window that must agree to confirm a gesture

    def __init__(self, window_size: int):
        self.window_size = max(window_size, 1)
        self._window: deque[str] = deque(maxlen=self.window_size)
        self.stable_gesture = "None"  # current debounced/confirmed gesture
        self._armed_gesture: str | None = None  # gesture already fired for this hold

    def resize(self, window_size: int):
        window_size = max(window_size, 1)
        if window_size != self.window_size:
            self.window_size = window_size
            self._window = deque(self._window, maxlen=window_size)

    def update(self, raw_gesture: str) -> tuple[str, str | None]:
        """Feed one new raw reading. Returns (stable_gesture, action_to_fire).
        action_to_fire is the gesture name to act on this frame, or None if
        nothing new should fire."""
        self._window.append(raw_gesture)

        # Don't judge a majority until the window has enough samples -
        # avoids firing prematurely right after a gesture switch or right
        # after the hold-window size changes.
        if len(self._window) < self.window_size:
            return self.stable_gesture, None

        counts: dict[str, int] = {}
        for g in self._window:
            counts[g] = counts.get(g, 0) + 1
        top_gesture, top_count = max(counts.items(), key=lambda kv: kv[1])

        if top_gesture == "None" or (top_count / len(self._window)) < self.MAJORITY_RATIO:
            # No gesture clearly dominates the recent window - treat as
            # released, and allow the next real gesture to fire fresh.
            self.stable_gesture = "None"
            self._armed_gesture = None
            return self.stable_gesture, None

        self.stable_gesture = top_gesture
        if self._armed_gesture == top_gesture:
            return self.stable_gesture, None  # already fired for this hold

        self._armed_gesture = top_gesture
        return self.stable_gesture, top_gesture


def _engine_worker():
    global engine, engine_state, engine_error

    try:
        engine = HandEngine()
    except Exception as e:
        engine_state = "error"
        engine_error = str(e)
        _log("ENGINE", f"error: {e}")
        _running_event.clear()
        return

    engine_state = "active"
    engine_error = None
    _apply_settings_to_engine()
    _log("ENGINE", "started")

    confirmer = _GestureConfirmer(settings.confirm_frames)
    last_logged_gesture = "None"
    camera_lost = False

    # Cooldown after any action fires. Folding the hand from open palm into
    # a peace sign (or any multi-finger gesture) passes *through* other
    # transient shapes on the way, so without this a fast fold could fire
    # one gesture immediately followed by another as the hand keeps
    # moving. This cooldown blocks a second action from firing too soon
    # after the first, regardless of what gesture caused it.
    ACTION_COOLDOWN_SECONDS = 0.6
    last_action_time = 0.0

    while _running_event.is_set():
        if not engine.update():
            camera_lost = True
            break

        # Cursor tracking runs every frame - needs to feel instant, not
        # gated behind the gesture-confirmation logic below.
        controller.move_mouse(engine.cursor_x, engine.cursor_y)

        # Settings can change live from the UI, so re-apply each frame
        # (cheap - a couple of attribute assignments plus a window resize
        # that's a no-op unless confirm_frames actually changed).
        _apply_settings_to_engine()
        confirmer.resize(settings.confirm_frames)

        stable_gesture, action = confirmer.update(engine.gesture)

        if stable_gesture != last_logged_gesture:
            _log("GESTURE", stable_gesture)
            last_logged_gesture = stable_gesture

        if action is not None:
            now = time.time()
            if now - last_action_time >= ACTION_COOLDOWN_SECONDS:
                controller.press_key(action)
                _log("ACTION", action)
                last_action_time = now
            else:
                _log("ACTION", f"{action} (skipped - cooldown)")

    engine.release()
    engine = None
    _running_event.clear()

    if camera_lost:
        engine_state = "error"
        engine_error = "Camera feed ended unexpectedly."
        _log("ENGINE", "error: camera lost")
    else:
        engine_state = "offline"
        _log("ENGINE", "stopped")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.post("/api/engine/start")
def start_engine():
    global _engine_thread, engine_state
    with _engine_lock:
        if engine_state in ("starting", "active"):
            return {"status": "already_running"}
        if engine_state == "stopping":
            return {"status": "stopping_in_progress"}
        engine_state = "starting"
        _running_event.set()
        _engine_thread = threading.Thread(target=_engine_worker, daemon=True)
        _engine_thread.start()
        return {"status": "starting"}


@app.post("/api/engine/stop")
def stop_engine():
    global engine_state
    with _engine_lock:
        if engine_state not in ("starting", "active"):
            return {"status": "already_stopped"}
        engine_state = "stopping"
        _running_event.clear()
        return {"status": "stopping"}


@app.get("/api/status")
def get_status():
    return {
        "engine_state": engine_state,
        "error": engine_error,
        "gesture": engine.gesture if engine else "None",
        "cursor_x": round(engine.cursor_x, 3) if engine else 0.5,
        "cursor_y": round(engine.cursor_y, 3) if engine else 0.5,
        "pointer_active": engine.pointer_active if engine else False,
        "hand_detected": engine.hand_detected if engine else False,
        "log": list(activity_log)[-20:],
    }


@app.post("/api/action/{key}")
def trigger_key(key: str):
    # Deliberately independent of engine state - a manual button press
    # should work whether or not the camera/engine is running.
    controller.press_key(key)
    _log("ACTION", f"{key} (manual)")
    return {"status": "success"}


@app.get("/api/settings")
def get_settings():
    return settings.model_dump()


@app.post("/api/settings")
def update_settings(new_settings: Settings):
    global settings
    settings = new_settings
    _apply_settings_to_engine()
    return settings.model_dump()


@app.get("/video_feed")
def video_feed():
    def generate():
        last_id = -1
        while True:
            current_engine = engine
            if current_engine is not None:
                frame, frame_id = current_engine.get_jpeg_frame_and_id()
                if frame is not None and frame_id != last_id:
                    last_id = frame_id
                    yield (b"--frame\r\n"
                           b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
            # Poll at 60Hz but only ever emit a genuinely new frame - avoids
            # re-sending an unchanged frame at a fixed rate, which was
            # previously wasting bandwidth and browser decode time whenever
            # detection ran slower than the stream rate.
            time.sleep(1 / 60)

    return StreamingResponse(generate(), media_type="multipart/x-mixed-replace; boundary=frame")


# ---------------------------------------------------------------------------
# Frontend - served by this same process so the user just visits
# http://127.0.0.1:8001 instead of opening commander.html via file://
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.on_event("shutdown")
def on_shutdown():
    # Make sure the camera is never left held open by an orphaned thread
    # when the server process exits (Ctrl+C, or the eventual .exe closing).
    global engine
    _running_event.clear()
    if _engine_thread is not None:
        _engine_thread.join(timeout=2)
    if engine is not None:
        engine.release()
        engine = None


def open_browser():
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:8001")


if __name__ == "__main__":
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8001)