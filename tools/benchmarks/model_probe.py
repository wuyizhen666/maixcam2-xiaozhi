#!/usr/bin/env python3
"""Temporary on-device YOLO11 capacity probe. Does not open display/audio.

Measures camera read call start/return through sequential same-frame results.
These are software intervals, NOT optical sensor-to-screen latency.
Run as a fresh process while the supervisor monitors the rest of the system.
"""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import statistics
import sys
import time
import traceback

MODELS = {
    'general': '/root/models/yolo11n.mud',
    'general_s': '/root/models/yolo11s.mud',
    'fall_current': '/root/models/fall_yolo11.mud',
    'danger_current': '/root/models/security_yolo11.mud',
    'fall': '/root/models/easyyolo/shuaidao/model.mud',
    'danger': '/root/models/easyyolo/weixianwupin/model.mud',
}
STOP = False


def stop(_signum, _frame):
    global STOP
    STOP = True


def distribution(values):
    if not values:
        return {'count': 0, 'p50': None, 'p95': None, 'max': None}
    values = sorted(values)
    return {'count': len(values), 'p50': statistics.median(values),
            'p95': values[max(0, math.ceil(.95 * len(values)) - 1)],
            'max': values[-1]}


def summarize(rows, aliases, elapsed):
    result = {'frames': len(rows), 'elapsed_s': elapsed,
              'frame_results_per_s': len(rows) / elapsed if elapsed > 0 else 0,
              'camera_read_ms': distribution([r['read_ms'] for r in rows]),
              'same_frame_all_results_ms': distribution([r['all_done_ms'] for r in rows]),
              'models': {}}
    for alias in aliases:
        model_rows = [r['models'][alias] for r in rows]
        result['models'][alias] = {
            'results': len(model_rows),
            'results_per_s': len(model_rows) / elapsed if elapsed > 0 else 0,
            'prep_ms': distribution([r['prep_ms'] for r in model_rows]),
            'detect_ms': distribution([r['detect_ms'] for r in model_rows]),
            'prep_and_detect_ms': distribution([r['prep_ms'] + r['detect_ms'] for r in model_rows]),
            'read_start_to_result_ms': distribution([r['read_start_to_result_ms'] for r in model_rows]),
            'read_return_to_result_ms': distribution([r['read_return_to_result_ms'] for r in model_rows]),
            'result_interval_ms': distribution([r['result_interval_ms'] for r in model_rows
                                                 if r['result_interval_ms'] is not None]),
            'read_start_latency_over_100ms': sum(r['read_start_to_result_ms'] > 100 for r in model_rows),
            'result_intervals_over_100ms': sum(r['result_interval_ms'] is not None and
                                              r['result_interval_ms'] > 100 for r in model_rows),
            'objects_total': sum(r['objects'] for r in model_rows),
        }
    return result


def main():
    probe_started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', nargs='+', choices=sorted(MODELS), required=True)
    parser.add_argument('--seconds', type=float, default=45)
    parser.add_argument('--fps-limit', type=float, default=0,
                        help='Shared frame rate cap, therefore cap for EACH model; 0 means uncapped')
    parser.add_argument('--out', required=True, help='JSONL output path, created exclusively')
    parser.add_argument('--warmup-seconds', type=float, default=2)
    args = parser.parse_args()
    if len(set(args.models)) != len(args.models):
        parser.error('Choose distinct model aliases')
    if not 1 <= args.seconds <= 1800 or not 0 <= args.fps_limit <= 120:
        parser.error('seconds must be 1..1800; fps-limit must be 0..120')
    if not 2 <= args.warmup_seconds <= 10:
        parser.error('warmup-seconds must be 2..10')
    if os.environ.get('LD_PRELOAD') or os.environ.get('MAIXCAM2_GF'):
        parser.error('Start in a clean environment without overlay injection')
    for alias in args.models:
        if not Path(MODELS[alias]).is_file():
            parser.error('Missing model: ' + MODELS[alias])
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    cam = None
    detectors = []
    rows = []
    errors = []
    measure_started = None
    elapsed = 0
    exit_code = 0
    with output.open('x', encoding='utf-8') as handle:
        def emit(kind, **fields):
            record = dict(kind=kind, monotonic_s=time.monotonic(), unix_s=time.time(), **fields)
            handle.write(json.dumps(record, allow_nan=False) + '\n')
            handle.flush()

        emit('start', pid=os.getpid(), models={a: MODELS[a] for a in args.models},
             duration_s=args.seconds, fps_limit=args.fps_limit, dual_buff=False,
             latency_scope='camera API read call start/return to inference completion; excludes sensor exposure/ISP age and display',
             note='Maix SDK import may initialize its default communication/key facilities; no app/display/audio is opened by this probe')
        try:
            imported = time.monotonic()
            # Main-thread initialization is required by this SDK's signal hooks.
            from maix import app, camera, image, nn
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            emit('sdk_imported', elapsed_s=time.monotonic() - imported)
            specs = []
            for alias in args.models:
                if STOP:
                    raise InterruptedError('Stopped during model loading')
                loaded = time.monotonic()
                detector = nn.YOLO11(model=MODELS[alias], dual_buff=False)
                detectors.append((alias, detector))
                spec = (detector.input_width(), detector.input_height(), detector.input_format())
                specs.append(spec)
                emit('model_loaded', model=alias, load_ms=(time.monotonic() - loaded) * 1000,
                     width=spec[0], height=spec[1], input_format=str(spec[2]), labels=list(detector.labels))
            if any(spec[2] != image.Format.FMT_RGB888 for spec in specs):
                raise RuntimeError('Shared-frame resize probe currently supports RGB888 models only')
            camera_spec = (max(spec[0] for spec in specs), max(spec[1] for spec in specs), specs[0][2])
            model_specs = dict(zip(args.models, specs))
            transforms = {}
            for alias, spec in model_specs.items():
                transforms[alias] = {
                    'source_size': list(camera_spec[:2]), 'input_size': list(spec[:2]),
                    'fit': 'FIT_CONTAIN', 'resize_method': 'BILINEAR',
                    'content_rect_input_px': list(image.resize_map_pos(
                        camera_spec[0], camera_spec[1], spec[0], spec[1],
                        image.Fit.FIT_CONTAIN, 0, 0, camera_spec[0], camera_spec[1])),
                    'result_coordinates': 'model-input pixels, including centered black letterbox padding',
                }
            emit('shared_input', width=camera_spec[0], height=camera_spec[1],
                 input_format=str(camera_spec[2]), transforms=transforms,
                 accuracy_scope='throughput only; per-model resized inputs share the same source frame; no detection accuracy assertion')

            def prepare_input(frame, alias):
                width, height, _ = model_specs[alias]
                if frame.width() == width and frame.height() == height:
                    return frame
                # Official Image.resize creates a new image; FIT_CONTAIN preserves
                # aspect ratio and fills unused input pixels with black.
                return frame.resize(width, height, fit=image.Fit.FIT_CONTAIN,
                                    method=image.ResizeMethod.BILINEAR)

            cam = camera.Camera(*camera_spec)
            cam.exp_mode(camera.AeMode.Auto)
            cam.awb_mode(camera.AwbMode.Auto)
            warm_started = time.monotonic()
            warm_frames = 0
            while warm_frames < 30 or time.monotonic() - warm_started < args.warmup_seconds:
                if STOP or app.need_exit():
                    raise InterruptedError('Stopped during camera warmup')
                if time.monotonic() - warm_started > args.warmup_seconds + 6:
                    raise RuntimeError('Camera warmup exceeded finite deadline')
                warm_read_start = time.monotonic()
                frame = cam.read(block_ms=1000)
                warm_read_done = time.monotonic()
                if frame is None:
                    raise RuntimeError('Camera returned no warmup image')
                warm_frames += 1
            # Separate the first kernel/runtime initialization from steady-state timing.
            for alias, detector in detectors:
                prep_start = time.monotonic()
                model_frame = prepare_input(frame, alias)
                t0 = time.monotonic()
                objects = detector.detect(model_frame, conf_th=.5, iou_th=.45)
                first_done = time.monotonic()
                emit('model_first_inference', model=alias, detect_ms=(first_done-t0)*1000,
                     prep_ms=(t0-prep_start)*1000,
                     startup_first_result_ms=(first_done-probe_started)*1000,
                     read_start_to_result_ms=(first_done-warm_read_start)*1000,
                     read_return_to_result_ms=(first_done-warm_read_done)*1000,
                     objects=len(objects))
            emit('ready', camera_warmup_frames=warm_frames,
                 camera_warmup_s=time.monotonic()-warm_started,
                 startup_to_steady_state_ms=(time.monotonic()-probe_started)*1000)
            measure_started = time.monotonic()
            next_tick = measure_started
            report_start = measure_started
            report_index = 0
            previous_results = {}
            previous_frame_start = None
            while not STOP and not app.need_exit():
                now = time.monotonic()
                if now - measure_started >= args.seconds:
                    break
                if args.fps_limit > 0 and now < next_tick:
                    time.sleep(min(next_tick-now, args.seconds-(now-measure_started)))
                    if time.monotonic()-measure_started >= args.seconds or STOP:
                        break
                t0 = time.monotonic()
                frame = cam.read(block_ms=1000)
                read_done = time.monotonic()
                if frame is None:
                    raise RuntimeError('Camera returned no measurement image')
                row = {'frame_id': len(rows), 'read_start_s': t0-measure_started,
                       'read_ms': (read_done-t0)*1000,
                       'frame_start_interval_ms': None if previous_frame_start is None else (t0-previous_frame_start)*1000,
                       'models': {}}
                previous_frame_start = t0
                for alias, detector in detectors:
                    prep_start = time.monotonic()
                    model_frame = prepare_input(frame, alias)
                    infer_start = time.monotonic()
                    objects = detector.detect(model_frame, conf_th=.5, iou_th=.45)
                    done = time.monotonic()
                    row['models'][alias] = {
                        'prep_ms': (infer_start-prep_start)*1000,
                        'detect_ms': (done-infer_start)*1000,
                        'read_start_to_result_ms': (done-t0)*1000,
                        'read_return_to_result_ms': (done-read_done)*1000,
                        'result_interval_ms': None if alias not in previous_results else (done-previous_results[alias])*1000,
                        'objects': len(objects),
                    }
                    previous_results[alias] = done
                row['all_done_ms'] = (time.monotonic()-t0)*1000
                rows.append(row)
                if args.fps_limit > 0:
                    next_tick += 1/args.fps_limit
                    # Do not burst through missed periods after an overloaded frame.
                    if next_tick < time.monotonic()-1/args.fps_limit:
                        next_tick = time.monotonic()
                now = time.monotonic()
                if now-report_start >= 1:
                    window = rows[report_index:]
                    emit('window', **summarize(window, args.models, now-report_start), samples=window)
                    report_index = len(rows)
                    report_start = now
            elapsed = time.monotonic()-measure_started
            if report_index < len(rows):
                emit('window_tail', **summarize(rows[report_index:], args.models,
                                              time.monotonic()-report_start), samples=rows[report_index:])
        except InterruptedError as error:
            errors.append(str(error))
            emit('interrupted', message=str(error))
            exit_code = 2
        except BaseException as error:
            errors.append(type(error).__name__ + ': ' + str(error))
            emit('error', error_type=type(error).__name__, message=str(error), traceback=traceback.format_exc())
            exit_code = 1
        finally:
            if measure_started is not None and not elapsed:
                elapsed = time.monotonic()-measure_started
            closed = False
            if cam is not None:
                try:
                    cam.close()
                    closed = True
                except Exception as error:
                    errors.append('camera close: ' + str(error))
                    exit_code = 1
            detectors.clear()
            emit('summary', **summarize(rows, args.models, elapsed),
                 stopped=STOP, errors=errors, camera_closed=closed,
                 completed_duration=elapsed >= args.seconds,
                 optical_end_to_end_verified=False)
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
