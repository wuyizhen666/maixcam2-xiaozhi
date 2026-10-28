#!/usr/bin/env python3
"""Visible two-model MaixCAM2 diagnostic app; no audio or MCP registration."""
import os
import sys

# A launcher child must not inherit the desktop's overlay hooks.
STRIP_KEYS = ('LD_PRELOAD', 'MAIXCAM2_GF', 'MAIXCAM2_OVL', 'MAIXCAM2_GF_LOG')
if any(os.environ.get(k) for k in STRIP_KEYS):
    clean_env = {k: v for k, v in os.environ.items() if k not in STRIP_KEYS}
    os.execve('/usr/bin/python3', ['/usr/bin/python3', os.path.abspath(__file__)] + sys.argv[1:], clean_env)

import json
from pathlib import Path
import signal
import time
import traceback

from maix import app, camera, display, err, image, nn, touchscreen

ROOT = Path('/tmp/xiaozhi-visual-test')
MODEL_PATHS = {'pose': '/root/models/fall_yolo11.mud',
               'objects': '/root/models/security_yolo11.mud'}
MODES = {'both': ('pose', 'objects'), 'pose': ('pose',), 'objects': ('objects',)}
MODE_BUTTONS = [('both', (292, 8, 104, 40)), ('pose', (402, 8, 104, 40)),
                ('objects', (512, 8, 120, 40))]
BACK = (8, 8, 92, 44)
VIEW = (8, 80, 384, 384)
TARGET_FPS = 25.0
STOP = False


def color(r, g, b):
    return image.Color.from_rgb(r, g, b)


BG = color(10, 20, 31)
PANEL = color(22, 38, 52)
WHITE = color(228, 240, 248)
DIM = color(137, 164, 182)
BLUE = color(43, 104, 153)
COLORS = {'pose': color(35, 225, 150), 'objects': color(255, 164, 55)}


def inside(x, y, rect):
    return rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def text(canvas, x, y, value, ink=WHITE, scale=1.):
    # All test labels are ASCII; avoid FreeType rasterization on every frame.
    canvas.draw_string(int(x), int(y), str(value), ink, scale * 1.5,
                       thickness=1, wrap=False, font='hershey_plain')


def read_action(touch, previous_pressed):
    x, y, pressed = touch.read()
    action = None
    if pressed and not previous_pressed:
        if inside(x, y, BACK):
            action = 'back'
        else:
            action = next((mode for mode, box in MODE_BUTTONS if inside(x, y, box)), None)
    return action, bool(pressed)


def panel_static(canvas, key, top):
    canvas.draw_rect(404, top, 228, 162, PANEL, -1)
    text(canvas, 416, top + 9, 'POSE' if key == 'pose' else 'OBJECTS', COLORS[key], 1.1)
    text(canvas, 416, top + 34, Path(MODEL_PATHS[key]).stem, DIM, .8)


def panel_live(canvas, key, top, records, enabled):
    if enabled:
        text(canvas, 416, top + 58, '%d found' % len(records), WHITE, .9)
        if not records:
            text(canvas, 416, top + 86, 'No detections', DIM, .9)
        for index, record in enumerate(records[:3]):
            text(canvas, 416, top + 85 + index * 22,
                 '%s %.2f' % (record['label'][:17], record['score']), COLORS[key], .85)


class RenderCache:
    """Cache fixed chrome by mode; refresh only FPS/timing text at 2 Hz."""
    def __init__(self):
        self.templates = {}
        self.layer = None
        self.mode = None
        self.next_stats = 0.

    def refresh(self, mode, fps, inference, now):
        if self.mode == mode and now < self.next_stats:
            return 0.
        started = time.monotonic()
        if mode not in self.templates:
            base = image.Image(640, 480, image.Format.FMT_RGB888)
            base.draw_rect(0, 0, 640, 480, BG, -1)
            base.draw_rect(*BACK, BLUE, -1)
            text(base, 20, 20, '< Back', WHITE)
            text(base, 114, 9, 'VISION TEST', WHITE, 1.05)
            for key, box in MODE_BUTTONS:
                base.draw_rect(*box, BLUE if key == mode else PANEL, -1)
                text(base, box[0]+12, box[1]+12, key.upper(), WHITE, .9)
            for key, top in [('pose',80), ('objects',254)]:
                panel_static(base, key, top)
                if key not in MODES[mode]:
                    text(base, 416, top+58, 'Paused', WHITE, .9)
            text(base, 406, 429, 'Software timing only', DIM, .8)
            text(base, 406, 451, 'Preview saved every 5s', DIM, .75)
            self.templates[mode] = base
        self.layer = self.templates[mode].copy()
        text(self.layer, 114, 40, 'UI %.1f FPS / target 25' % fps, DIM, .75)
        for key, top in [('pose',80), ('objects',254)]:
            if key in MODES[mode]:
                text(self.layer, 500, top+58, '| %.1f ms' % inference.get(key,0), WHITE, .9)
        self.mode = mode
        self.next_stats = now + .5
        return (time.monotonic()-started)*1000


def compose(canvas, frame, detections, inference, mode, fps, frame_id, cache):
    cache_ms = cache.refresh(mode, fps, inference, time.monotonic())
    canvas.draw_image(0, 0, cache.layer)
    canvas.draw_image(VIEW[0], VIEW[1], frame.resize(VIEW[2], VIEW[3]))
    # Both detectors have already consumed the original image. Only the canvas
    # receives annotations, preserving the source for both model results.
    scale = VIEW[2] / 320.
    for key in MODES[mode]:
        for record in detections[key][:20]:
            x, y, w, h = record['box']
            x0 = VIEW[0] + round(max(0, min(319, x)) * scale)
            y0 = VIEW[1] + round(max(0, min(319, y)) * scale)
            x1 = VIEW[0] + round(max(0, min(320, x + w)) * scale)
            y1 = VIEW[1] + round(max(0, min(320, y + h)) * scale)
            if x1 <= x0 or y1 <= y0:
                continue
            canvas.draw_rect(x0, y0, x1-x0, y1-y0, COLORS[key], 2)
            label = ('P ' if key == 'pose' else 'O ') + record['label'][:16] + ' %.2f' % record['score']
            text(canvas, min(x0, VIEW[0]+150), max(VIEW[1], y0-19), label, COLORS[key], .8)
    text(canvas, 10, 58, 'LIVE CAMERA | frame %d | green=P / orange=O' % frame_id, DIM, .85)
    # Counts, class names and confidence values remain current-frame data.
    panel_live(canvas, 'pose', 80, detections['pose'], 'pose' in MODES[mode])
    panel_live(canvas, 'objects', 254, detections['objects'], 'objects' in MODES[mode])
    return cache_ms


def main():
    global STOP
    ROOT.mkdir(mode=0o700, exist_ok=True)
    run_id = '%d-%d' % (int(time.time()), os.getpid())
    log_path = ROOT / ('metrics-' + run_id + '.jsonl')
    disp = cam = touch = None
    models = {}
    frame_id = 0
    mode = 'both'
    previous_pressed = False
    outcome = 'stopped'
    error_message = None
    def stop(signum, frame):
        global STOP
        STOP = True
    with log_path.open('x', encoding='utf-8') as log:
        def emit(kind, **fields):
            entry = dict(kind=kind, run_id=run_id, pid=os.getpid(), monotonic_s=time.monotonic(), **fields)
            log.write(json.dumps(entry, allow_nan=False) + '\n')
            log.flush()
        try:
            # Preserve the device's required display-before-other-multimedia order.
            disp = display.Display()
            if (disp.width(), disp.height()) != (640, 480):
                raise RuntimeError('This diagnostic layout requires a 640x480 display')
            touch = touchscreen.TouchScreen()
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            canvas = image.Image(640, 480, image.Format.FMT_RGB888)
            render_cache = RenderCache()
            canvas.draw_rect(0, 0, 640, 480, BG, -1)
            text(canvas, 24, 30, 'Loading two offline models...', WHITE, 1.2)
            disp.show(canvas)
            for key, path in MODEL_PATHS.items():
                started = time.monotonic()
                models[key] = nn.YOLO11(model=path, dual_buff=False)
                model = models[key]
                if (model.input_width(), model.input_height(), model.input_format()) != (320, 320, image.Format.FMT_RGB888):
                    raise RuntimeError(key + ' input is not 320x320 RGB888')
                emit('model_loaded', model=key, path=path, load_ms=(time.monotonic()-started)*1000,
                     labels=list(model.labels))
            cam = camera.Camera(320, 320, image.Format.FMT_RGB888)
            cam.exp_mode(camera.AeMode.Auto)
            cam.awb_mode(camera.AwbMode.Auto)
            warm_start = time.monotonic()
            warm_count = 0
            while warm_count < 30 or time.monotonic()-warm_start < 2:
                if STOP or app.need_exit():
                    return
                if time.monotonic()-warm_start > 8:
                    raise RuntimeError('Camera warmup timeout')
                action, previous_pressed = read_action(touch, previous_pressed)
                if action == 'back':
                    return
                if cam.read(block_ms=1000) is None:
                    raise RuntimeError('No camera image during warmup')
                warm_count += 1
            emit('ready', models=MODEL_PATHS, target_fps=TARGET_FPS, input_size=[320,320],
                 display_size=[640,480], dual_buff=False,
                 render_font='hershey_plain', stats_refresh_s=.5,
                 latency_scope='camera API start to inference completion/display.show return; not optical latency')
            window_start = time.monotonic()
            rows = []
            next_tick = window_start
            preview_due = window_start
            fps = 0.
            while not STOP and not app.need_exit():
                action, previous_pressed = read_action(touch, previous_pressed)
                if action == 'back':
                    break
                if action in MODES and action != mode:
                    mode = action
                    emit('mode', mode=mode)
                now = time.monotonic()
                if now < next_tick:
                    time.sleep(next_tick-now)
                started = time.monotonic()
                frame = cam.read(block_ms=1000)
                captured = time.monotonic()
                if frame is None:
                    raise RuntimeError('Camera returned no image')
                frame_id += 1
                detections = {'pose': [], 'objects': []}
                inference = {}
                result_latency = {}
                for key in MODES[mode]:
                    infer_start = time.monotonic()
                    objects = models[key].detect(frame, conf_th=.5, iou_th=.45)
                    done = time.monotonic()
                    inference[key] = (done-infer_start)*1000
                    result_latency[key] = (done-started)*1000
                    labels = models[key].labels
                    detections[key] = [{'label': str(labels[obj.class_id]), 'class_id': int(obj.class_id),
                                        'score': float(obj.score), 'box': [int(obj.x),int(obj.y),int(obj.w),int(obj.h)]}
                                       for obj in objects]
                composed_start = time.monotonic()
                cache_ms = compose(canvas, frame, detections, inference, mode, fps, frame_id, render_cache)
                before_show = time.monotonic()
                disp.show(canvas)
                shown = time.monotonic()
                save_ms = 0.
                if shown >= preview_due:
                    save_start = time.monotonic()
                    temporary = ROOT/'preview.next.jpg'
                    if canvas.save(str(temporary), quality=80) != err.Err.ERR_NONE:
                        raise RuntimeError('Saving diagnostic preview failed')
                    os.replace(temporary, ROOT/'preview.jpg')
                    if frame_id == 1:
                        (ROOT/('first-frame-'+run_id+'.jpg')).write_bytes((ROOT/'preview.jpg').read_bytes())
                    save_ms = (time.monotonic()-save_start)*1000
                    preview_due = time.monotonic()+5
                rows.append({'frame_id':frame_id, 'mode':mode, 'capture_ms':(captured-started)*1000,
                             'inference_ms':inference, 'capture_to_result_ms':result_latency,
                             'draw_ms':(before_show-composed_start)*1000,
                             'cache_refresh_ms':cache_ms,
                             'display_show_ms':(shown-before_show)*1000,
                             'capture_to_display_submit_ms':(shown-started)*1000,
                             'preview_save_ms':save_ms, 'display_return_s':shown,
                             'counts':{key:len(value) for key,value in detections.items()}})
                now = time.monotonic()
                report_due = now-window_start >= 1
                if report_due:
                    elapsed = now-window_start
                    fps = len(rows)/elapsed if elapsed else 0
                    emit('window', mode=mode, elapsed_s=elapsed, ui_submit_fps=fps, samples=rows)
                    rows = []
                    # Keep log/state I/O in the next window's elapsed time.
                    window_start = now
                if report_due or frame_id == 1:
                    atomic_json(ROOT/'state.json', {'status':'running','ready':True,'pid':os.getpid(),
                                'run_id':run_id,'frame_id':frame_id,'mode':mode,'ui_submit_fps':fps,
                                'models':MODEL_PATHS,'counts':{k:len(v) for k,v in detections.items()},
                                'inference_ms':inference,'detections':{k:v[:20] for k,v in detections.items()},
                                'preview':str(ROOT/'preview.jpg'),'timestamp_unix':time.time()})
                next_tick += 1/TARGET_FPS
                if next_tick < time.monotonic():
                    next_tick = time.monotonic()
            if rows:
                emit('window_tail', samples=rows)
        except BaseException as exc:
            outcome = 'error'
            error_message = type(exc).__name__ + ': ' + str(exc)
            emit('error', message=error_message, traceback=traceback.format_exc())
            if disp is not None and touch is not None:
                canvas = image.Image(640, 480, image.Format.FMT_RGB888)
                canvas.draw_rect(0,0,640,480,BG,-1)
                canvas.draw_rect(*BACK,BLUE,-1)
                text(canvas,20,20,'< Back')
                text(canvas,24,90,'VISION TEST ERROR',COLORS['objects'],1.2)
                for index in range(0,min(len(error_message),240),54):
                    text(canvas,24,140+(index//54)*28,error_message[index:index+54],WHITE,.9)
                text(canvas,24,340,'Tap Back. Auto return in 20 seconds.',DIM,.9)
                disp.show(canvas)
                deadline = time.monotonic()+20
                while not STOP and not app.need_exit() and time.monotonic()<deadline:
                    action, previous_pressed = read_action(touch,previous_pressed)
                    if action == 'back':
                        break
                    time.sleep(.05)
        finally:
            if cam is not None:
                try:
                    cam.close()
                except Exception as exc:
                    emit('cleanup_error',resource='camera',message=str(exc))
            models.clear()
            atomic_json(ROOT/'state.json', {'status':outcome,'ready':False,'pid':os.getpid(),
                        'run_id':run_id,'frame_id':frame_id,'error':error_message,'timestamp_unix':time.time()})
            emit('stopped',outcome=outcome,frames=frame_id)
    return 0 if outcome != 'error' else 1


if __name__ == '__main__':
    sys.exit(main())
