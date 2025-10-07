import obspython as obs
import mss
import time
from collections import deque

# ------------------------------------------------------------
# Initial configuration (default values)
interval          = 1        # interval in seconds (hidden from user)
block_size        = 32       # block size (pixels)
target_scene_name = "TEST"

prev_frame  = None
monitor_id  = 1
active_scene = None

#
# new global variables
current_corner = None
movement_threshold = 15   # minimum difference between the corner with the most and least movement
stay_threshold = 20       # low movement threshold to stay still

# history for moving average
history_length = 10  # number of frames to average
counts_history = { "top_left": deque(maxlen=history_length),
                   "top_right": deque(maxlen=history_length),
                   "bottom_left": deque(maxlen=history_length),
                   "bottom_right": deque(maxlen=history_length) }

# cooldown
last_move_time = 0
cooldown_seconds = 1   # seconds between movements (hidden from user)

source_name = "CAMTEST"  # Default, now configurable

# ------------------------------------------------------------
def get_source_bounds(scene_name, source_name):
    scene_source = obs.obs_get_source_by_name(scene_name)
    if scene_source is None:
        return None

    rect = None
    try:
        scene = obs.obs_scene_from_source(scene_source)
        if scene is not None:
            item = obs.obs_scene_find_source(scene, source_name)
            if item is not None:
                pos = obs.vec2()
                scale = obs.vec2()

                obs.obs_sceneitem_get_pos(item, pos)
                obs.obs_sceneitem_get_scale(item, scale)

                src = obs.obs_sceneitem_get_source(item)
                width = obs.obs_source_get_width(src)
                height = obs.obs_source_get_height(src)

                width *= scale.x
                height *= scale.y

                rect = (pos.x, pos.y, width, height)
    finally:
        pass
    return rect

def rects_intersect(r1, r2):
    x1, y1, w1, h1 = r1
    x2, y2, w2, h2 = r2
    return not (x1 + w1 < x2 or x2 + w2 < x1 or
                y1 + h1 < y2 or y2 + h2 < y1)

def analyze_screen():
    global prev_frame, block_size, monitor_id, active_scene, current_corner, last_move_time, interval, cooldown_seconds

    with mss.mss() as sct:
        monitor = sct.monitors[monitor_id]
        sct_img = sct.grab(monitor)
        data = sct_img.bgra
        screen_w, screen_h = sct_img.size

        # get OBS canvas size
        vi = obs.obs_video_info()
        if obs.obs_get_video_info(vi):
            canvas_w = vi.base_width
            canvas_h = vi.base_height
        else:
            canvas_w, canvas_h = screen_w, screen_h

        row_size = screen_w * 4
        changed_blocks = []

        if prev_frame is not None:
            for y in range(0, screen_h, block_size):
                for x in range(0, screen_w, block_size):
                    block = []
                    prev_block = []
                    for yy in range(block_size):
                        start = (y+yy) * row_size + x*4
                        end = start + block_size*4
                        block.append(data[start:end])
                        prev_block.append(prev_frame[start:end])
                    if b"".join(block) != b"".join(prev_block):
                        norm_x = (x / screen_w) * canvas_w
                        norm_y = (y / screen_h) * canvas_h
                        norm_w = (block_size / screen_w) * canvas_w
                        norm_h = (block_size / screen_h) * canvas_h
                        changed_blocks.append((norm_x, norm_y, norm_w, norm_h))

        prev_frame = data

        if not active_scene:
            return

        if scene_has_source(active_scene, source_name):
            camera_rect = get_source_bounds(active_scene, source_name)
            if camera_rect and changed_blocks:
                quadrants = {
                    "top_left":     (0, 0, canvas_w/2, canvas_h/2),
                    "top_right":    (canvas_w/2, 0, canvas_w/2, canvas_h/2),
                    "bottom_left":  (0, canvas_h/2, canvas_w/2, canvas_h/2),
                    "bottom_right": (canvas_w/2, canvas_h/2, canvas_w/2, canvas_h/2),
                }

                # count blocks in each corner
                counts = {name: sum(1 for blk in changed_blocks if rects_intersect(blk, rect))
                          for name, rect in quadrants.items()}

                # save to history
                for name in counts:
                    counts_history[name].append(counts[name])

                # moving average
                avg_counts = {name: (sum(counts_history[name]) / len(counts_history[name]))
                              if counts_history[name] else 0
                              for name in counts_history}

                # decide new corner using moving averages and smarter comparison
                target_corner = min(avg_counts, key=avg_counts.get)
                min_count = avg_counts[target_corner]
                max_count = max(avg_counts.values())
                current_count = avg_counts.get(current_corner, float('inf')) if current_corner else float('inf')

                # stay in corner if low movement AND there's no significantly better option
                if current_corner and current_count < stay_threshold:
                    # but move if there's a corner with significantly less movement
                    improvement_ratio = (current_count - min_count) / (current_count + 1)
                    if improvement_ratio > 0.3:  # 30% improvement threshold
                        obs.script_log(obs.LOG_INFO, f"DEBUG: better corner detected! {target_corner} ({min_count:.1f}) vs {current_corner} ({current_count:.1f})")
                    else:
                        obs.script_log(obs.LOG_INFO, f"DEBUG: staying in {current_corner} ({current_count:.1f} blocks)")
                        return

                now = time.time()
                if (max_count - min_count) > movement_threshold and (now - last_move_time > cooldown_seconds):
                    if target_corner != current_corner:
                        current_corner = target_corner
                        last_move_time = now
                        obs.script_log(obs.LOG_INFO, f"DEBUG: moving to {target_corner} (blocks: {min_count:.1f})")
                        move_corner(camera_rect, canvas_w, canvas_h, target_corner)
                else:
                    obs.script_log(obs.LOG_INFO, f"DEBUG: camera still (diff: {max_count-min_count:.1f}, threshold: {movement_threshold})")

def move_corner(camera_rect, canvas_w, canvas_h, target_corner):
    if target_corner == "top_left":
        new_x, new_y = 0, 0
    elif target_corner == "top_right":
        new_x, new_y = canvas_w - camera_rect[2], 0
    elif target_corner == "bottom_left":
        new_x, new_y = 0, canvas_h - camera_rect[3]
    else:  # bottom_right
        new_x, new_y = canvas_w - camera_rect[2], canvas_h - camera_rect[3]

    obs.script_log(obs.LOG_INFO, f"⚠️ Moving {source_name} to ({new_x},{new_y})")
    move_camera(active_scene, source_name, new_x, new_y)

def move_camera(scene_name, source_name, new_x, new_y):
    scene_source = obs.obs_get_source_by_name(scene_name)
    if scene_source is None:
        return
    try:
        scene = obs.obs_scene_from_source(scene_source)
        if scene is not None:
            item = obs.obs_scene_find_source(scene, source_name)
            if item is not None:
                pos = obs.vec2()
                pos.x = float(new_x)
                pos.y = float(new_y)
                obs.obs_sceneitem_set_pos(item, pos)
    finally:
        pass

def scene_has_source(scene_name, source_name):
    scene_source = obs.obs_get_source_by_name(scene_name)
    if scene_source is None:
        return False
    found = False
    try:
        if obs.obs_source_get_type(scene_source) == obs.OBS_SOURCE_TYPE_SCENE:
            scene = obs.obs_scene_from_source(scene_source)
            if scene is not None:
                item = obs.obs_scene_find_source(scene, source_name)
                if item is not None:
                    found = True
    finally:
        pass
    return found

# ------------------------------------------------------------
def on_event(event):
    global active_scene
    if event == obs.OBS_FRONTEND_EVENT_SCENE_CHANGED:
        scene = obs.obs_frontend_get_current_scene()
        if scene is not None:
            active_scene = obs.obs_source_get_name(scene)
            obs.script_log(obs.LOG_INFO, f"Active scene now: {active_scene}")

def script_description():
    return "Detects changed screen blocks and moves CAMTEST to the corner with the least movement, with moving average and cooldown."

def script_update(settings):
    global interval, monitor_id, target_scene_name, cooldown_seconds, source_name
    source_name = obs.obs_data_get_string(settings, "source_name")
    interval          = obs.obs_data_get_int(settings, "interval_hidden")
    cooldown_seconds  = obs.obs_data_get_int(settings, "cooldown_hidden")
    monitor_id        = obs.obs_data_get_int(settings, "monitor_id")
    target_scene_name = obs.obs_data_get_string(settings, "target_scene")

    obs.timer_remove(analyze_screen)
    obs.timer_add(analyze_screen, interval * 1000)

def script_defaults(settings):
    obs.obs_data_set_default_int(settings, "interval_hidden", 1)
    obs.obs_data_set_default_int(settings, "cooldown_hidden", 3)

def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_int(props, "monitor_id", "Monitor Number", 1, 10, 1)
    obs.obs_properties_add_text(props, "target_scene", "Target scene name", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "source_name", "Source name to move", obs.OBS_TEXT_DEFAULT)
    # We do not add interval or cooldown so they remain hidden
    return props

def script_load(settings):
    obs.obs_frontend_add_event_callback(on_event)
    obs.timer_remove(analyze_screen)
    obs.timer_add(analyze_screen, interval * 1000)