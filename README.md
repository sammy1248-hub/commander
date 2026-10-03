# Commander

Control your PC with hand gestures. Commander uses your webcam and MediaPipe hand tracking to move the mouse, click, play/pause media and change the volume, with a live local web dashboard.

## Features

- Cursor control with your index finger
- Five gestures mapped to mouse and keyboard actions
- Live camera preview with the hand skeleton drawn on it, streamed to the browser
- Dashboard with status indicators, an activity log and live-adjustable settings
- Manual buttons for every action, so you can test without the camera

## Gestures

| Gesture | Action |
|---|---|
| ☝️ Index finger only (others curled) | Move the cursor |
| ✋ Open palm | Play / Pause (space) |
| 👍 Thumbs up (on a fist) | Volume up |
| 👎 Thumbs down (on a fist) | Volume down |
| 🤟 Index + middle + ring up, pinky curled | Left click |
| ✌️ Peace sign (index + middle up) | Right click |

The cursor only moves in the pointer pose, so it stays still while you trigger a gesture.

## Requirements

- Windows, macOS or Linux with a webcam
- Python 3.10 or newer

## Quick start

**Windows:** double-click `Run_Commander.bat`. It checks for Python (and installs 3.12 if missing), installs the dependencies on first run, and starts the app.

**Any OS:**

```bash
pip install -r requirements.txt
python app.py
```

Your browser opens `http://127.0.0.1:8001`. If it doesn't, open that address manually.

The hand-tracking model (`hand_landmarker.task`) is downloaded automatically on first run, so you need an internet connection the first time.

## Usage

1. Click **START ENGINE** and allow camera access if asked.
2. Point with your index finger (other fingers curled) to move the cursor.
3. Hold a gesture steady for a moment to trigger it.
4. Tune **Cursor Smoothing**, **Cursor Margin** and **Gesture Hold** in the Settings panel. Changes apply live.
5. Click **STOP ENGINE** to release the camera.

## Project structure

```
commander/
├── app.py              # FastAPI backend: engine lifecycle, API, gesture debouncing
├── engine.py           # MediaPipe hand tracking and gesture classification
├── controller.py       # pyautogui mouse/keyboard control
├── requirements.txt
├── Run_Commander.bat   # Windows launcher
└── static/             # Dashboard frontend
    ├── index.html
    ├── style.css
    └── app.js
```

## How it works

- `engine.py` reads webcam frames, finds 21 hand landmarks, works out which fingers are curled, and classifies the gesture.
- `app.py` confirms a gesture with a rolling majority vote over the last N frames (plus a short cooldown) so noisy frames don't trigger false actions.
- `controller.py` turns confirmed gestures into real mouse and keyboard input with pyautogui.

## Troubleshooting

- **Camera error:** another app may be using the webcam. Close it and press RETRY.
- **Gestures misfire:** raise **Gesture Hold** in Settings, and make sure your hand is well lit.
- **Cursor can't reach the screen edges:** lower **Cursor Margin**.

## Tech stack

Python, FastAPI, OpenCV, MediaPipe, pyautogui, vanilla JavaScript.

## Safety note

Commander controls your real mouse and keyboard. Stop the engine when you're not using it.
