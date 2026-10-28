/*
 * maixcam2_overlay.c — 状态栏/全屏弹窗注入 + 触摸拦截（LD_PRELOAD）v2
 *
 * 渲染：hook VO::push，把若干 RGBA 图层 alpha 混合进 640x480 BGRA 帧。
 *   - 状态栏小图标（y<48）
 *   - 全屏弹窗图层（w>=320）：对话 UI 覆盖整屏（640x480）
 * 触摸：hook open()/read()，对 /dev/input/event* 设备：
 *   - 弹窗图层可见时：吞掉全部触摸事件（桌面收不到＝彻底防误触图标）
 *     手势抬起时判定 TAP，把 "TAP x y" 追加写入 /tmp/xz_cmd
 *     （小智服务解析：X=关闭弹窗 / 喇叭=静音 / 其他=点击唤醒）
 *   - 弹窗不可见时：事件原样透传，桌面触摸完全不受影响
 *
 * 环境变量：
 *   MAIXCAM2_GF=1                  总开关（0 = 不注入）
 *   MAIXCAM2_OVL=file:x:y[:fps],.. 图层列表（640x480 坐标系）
 *   MAIXCAM2_GF_LOG=/tmp/xx.log    日志文件
 *
 * 图标文件格式：
 *   偏移 0: u32 帧数 n   偏移 4: u32 宽 w   偏移 8: u32 高 h
 *   偏移 12: n*w*h 字节 RGBA
 *
 * 编译：gcc -O2 -fPIC -shared -o libmaixcam2_overlay.so maixcam2_overlay.c -ldl -lpthread
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <linux/input.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/sysmacros.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define MAX_OVLS 8
#define MAX_ICON_BYTES (8u * 1024u * 1024u) /* 单图层上限 8MB */
#define DISABLE_FLAG "/root/uishim/DISABLE"
#define RELOAD_CHECK_PERIOD_MS 120
#define EVDEV_GLOB "/dev/input/event*"
#define CMD_PATH "/tmp/xz_cmd"
#define TOUCH_LOG "/tmp/xz_touch.log"
#define TAP_MIN_MS 70    /* 更短的按下视为抖动，忽略 */
#define TAP_MAX_MS 700   /* 更长的按住视为长按，忽略 */
#define TAP_MAX_MOVE 24  /* 抬起时移动超过该像素视为滑动，忽略 */
#define MAX_EVDEV 16

typedef struct {
    char path[512];
    int x, y, fps;
    uint8_t *pix;
    int n, w, h;
    int has_visible;
    time_t mtime;
    long mtime_nsec;
    ino_t inode;
    long size;
} overlay_t;

typedef int (*vo_push_fn)(void *self, int display_id, int channel, void *frame);

int VO_push(void *self, int a1, int a2, void *frame)
    __asm__("_ZN4maix10middleware8maixcam22VO4pushEiiPNS1_5FrameE");

static vo_push_fn real_push = NULL;
static overlay_t ovls[MAX_OVLS];
static int novl = 0;
static int gf_enabled = 1;
static int inited = 0;
static pthread_mutex_t ovl_lock = PTHREAD_MUTEX_INITIALIZER;
static FILE *logfile = NULL;

/* ---- 触摸拦截状态 ---- */
static int evdev_fds[MAX_EVDEV];
static int evdev_cnt = 0;
static volatile int touch_intercept = 0; /* 1 = 弹窗可见，吞触摸 */
static int popup_overlay_index = -1;     /* 哪个图层是弹窗（覆盖判定） */
static int st_down = 0;
static long st_t0 = 0;
static int st_x0 = 0, st_y0 = 0, st_lx = 0, st_ly = 0;
static ssize_t (*real_read)(int, void *, size_t) = NULL;
static ssize_t (*real_read_chk)(int, void *, size_t, size_t) = NULL;

/* ---------------- utils ---------------- */

static long now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void gflog(const char *fmt, ...) {
    if (!logfile) return;
    va_list ap;
    va_start(ap, fmt);
    vfprintf(logfile, fmt, ap);
    va_end(ap);
    fflush(logfile);
}

static void tlog(const char *fmt, ...) {
    static FILE *tf = NULL;
    if (!tf) tf = fopen(TOUCH_LOG, "a");
    if (!tf) return;
    va_list ap;
    va_start(ap, fmt);
    vfprintf(tf, fmt, ap);
    va_end(ap);
    fflush(tf);
}

/* ---------------- 真实 VO::push 符号解析 ---------------- */

static vo_push_fn find_real_push(void) {
    static const char SYM[] =
        "_ZN4maix10middleware8maixcam22VO4pushEiiPNS1_5FrameE";
    vo_push_fn p;

    p = (vo_push_fn)dlsym(RTLD_NEXT, SYM);
    if (p) return p;
    p = (vo_push_fn)dlsym(RTLD_DEFAULT, SYM);
    if (p) return p;

    FILE *f = fopen("/proc/self/maps", "r");
    if (f) {
        char line[1024];
        while (fgets(line, sizeof(line), f)) {
            char *path = strchr(line, '/');
            if (!path) continue;
            char *nl = strchr(path, '\n');
            if (nl) *nl = 0;
            if (!strstr(path, "libmaixcam_lib.so")) continue;
            void *h = dlopen(path, RTLD_LAZY | RTLD_GLOBAL);
            if (h) {
                p = (vo_push_fn)dlsym(h, SYM);
                if (p) { fclose(f); return p; }
            }
        }
        fclose(f);
    }

    static const char *knames[] = {
        "libmaixcam_lib.so", "libmaixcam_lib.so.1",
        "libmaixcam_lib.so.1.2.5", NULL
    };
    for (int i = 0; knames[i]; i++) {
        void *h = dlopen(knames[i], RTLD_LAZY | RTLD_GLOBAL);
        if (!h) continue;
        p = (vo_push_fn)dlsym(h, SYM);
        if (p) return p;
    }
    return NULL;
}

/* 只对系统 UI 进程生效（launcher 及其备选 maixvision-server） */
static int process_allowed(void) {
    char exe[512];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (n <= 0) return 0;
    exe[n] = 0;
    if (strstr(exe, "launcher")) return 1;
    if (strstr(exe, "maixvision-server")) return 1;
    return 0;
}

/* ---------------- 图层加载 ---------------- */

static void free_overlay(overlay_t *o) {
    free(o->pix);
    o->pix = NULL;
    o->n = o->w = o->h = 0;
    o->has_visible = 0;
}

static int load_overlay(overlay_t *o) {
    struct stat st;
    if (stat(o->path, &st) != 0) return -1;
    if (o->pix && st.st_mtime == o->mtime && st.st_mtim.tv_nsec == o->mtime_nsec &&
        st.st_ino == o->inode && (long)st.st_size == o->size)
        return 0;
    if ((unsigned long)st.st_size > MAX_ICON_BYTES) return -1;

    FILE *f = fopen(o->path, "rb");
    if (!f) return -1;

    uint32_t hdr[3];
    if (fread(hdr, 4, 3, f) != 3) { fclose(f); return -1; }
    uint32_t n = hdr[0], w = hdr[1], h = hdr[2];
    if (n < 1 || n > 64 || w < 1 || h < 1 || w > 640 || h > 640) {
        fclose(f);
        return -1;
    }
    size_t need = (size_t)n * w * h * 4;
    if ((long)need + 12 > (long)st.st_size) { fclose(f); return -1; }

    uint8_t *buf = (uint8_t *)malloc(need);
    if (!buf) { fclose(f); return -1; }
    if (fread(buf, 1, need, f) != need) { free(buf); fclose(f); return -1; }
    fclose(f);

    int visible = 0;
    for (size_t i = 3; i < need; i += 4) {
        if (buf[i]) { visible = 1; break; }
    }

    pthread_mutex_lock(&ovl_lock);
    free(o->pix);
    o->pix = buf;
    o->n = (int)n;
    o->w = (int)w;
    o->h = (int)h;
    o->has_visible = visible;
    o->mtime = st.st_mtime;
    o->mtime_nsec = st.st_mtim.tv_nsec;
    o->inode = st.st_ino;
    o->size = (long)st.st_size;
    pthread_mutex_unlock(&ovl_lock);
    return 1;
}

static void parse_ovl_env(char *env) {
    char *save1 = NULL;
    for (char *tok = strtok_r(env, ",", &save1); tok && novl < MAX_OVLS;
         tok = strtok_r(NULL, ",", &save1)) {
        char *p1 = strchr(tok, ':');
        if (!p1) continue;
        *p1 = 0;
        int x = atoi(p1 + 1);
        char *p2 = strchr(p1 + 1, ':');
        int y = 0, fps = 0;
        if (p2) {
            *p2 = 0;
            y = atoi(p2 + 1);
            char *p3 = strchr(p2 + 1, ':');
            if (p3) { *p3 = 0; fps = atoi(p3 + 1); }
        }
        overlay_t *o = &ovls[novl];
        memset(o, 0, sizeof(*o));
        snprintf(o->path, sizeof(o->path), "%s", tok);
        o->x = x;
        o->y = y;
        o->fps = fps;
        if (load_overlay(o) > 0)
            gflog("[ovl] loaded %s %dx%dx%d at (%d,%d)\n", o->path, o->w,
                  o->h, o->n, o->x, o->y);
        else
            gflog("[ovl] initial load failed: %s (will retry)\n", o->path);
        novl++;
    }
}

static void init_once(void) {
    inited = 1;

    real_push = find_real_push();
    if (!real_push) {
        fprintf(stderr,
            "[maixcam2_overlay] FATAL: real VO::push unresolved, "
            "frames will be dropped\n");
        return;
    }

    if (!process_allowed()) {
        gf_enabled = 0;
        return;
    }

    const char *gf = getenv("MAIXCAM2_GF");
    if (gf && strcmp(gf, "0") == 0) gf_enabled = 0;
    if (!gf_enabled) return;

    const char *lg = getenv("MAIXCAM2_GF_LOG");
    if (lg) logfile = fopen(lg, "a");

    const char *env = getenv("MAIXCAM2_OVL");
    if (env && *env) {
        char *dup = strdup(env);
        char *dup2 = strdup(env);
        if (dup) { parse_ovl_env(dup); free(dup); }
        /* popup=N 指定弹窗图层序号；默认第 2 个图层（状态栏之后） */
        if (dup2) {
            char *ph = strstr(dup2, "popup=");
            popup_overlay_index = ph ? atoi(ph + 6) : (novl >= 2 ? 1 : 0);
            free(dup2);
        }
    }
    gflog("[ovl] init ok, overlays=%d enabled=%d popup_idx=%d\n", novl,
          gf_enabled, popup_overlay_index);
}

/* ---------------- 触摸拦截 ---------------- */

/* ---- EVIOCGRAB：弹窗可见时独占触摸设备 ----
 * 这是「桌面还能滑动」的确定性修复：只 hook read() 时，launcher 若用别的方式
 * （epoll 边沿触发、libinput、ioctl 批量取事件…）取事件就漏网。EVIOCGRAB 之后
 * 除本进程这些 fd 外，任何进程都收不到触摸事件，与读取方式无关。
 * 弹窗隐藏时立即释放，launcher 完全恢复正常。 */
static int grab_state = 0;

static void set_touch_grab(int on) {
    if (on == grab_state) return;
    for (int i = 0; i < evdev_cnt; i++) {
        if (evdev_fds[i] < 0) continue;
        if (ioctl(evdev_fds[i], EVIOCGRAB, on ? 1 : 0) != 0) {
            if (on) gflog("[ovl] EVIOCGRAB fd=%d 失败: %s\n", evdev_fds[i],
                          strerror(errno));
        }
    }
    grab_state = on;
    gflog("[ovl] 触摸设备 %s\n", on ? "已独占（桌面不可点/不可滑）" : "已释放");
}

static int input_has_bit(const unsigned long *bits, unsigned int bit) {
    unsigned int word_bits = sizeof(*bits) * 8;
    return !!(bits[bit / word_bits] & (1UL << (bit % word_bits)));
}

/* 按设备能力识别触摸，兼容 launcher 自己打开的 fd；GPIO 按键和键盘
 * 也属于 INPUT_MAJOR，不能仅凭 major 独占或吞掉它们的事件。探测失败放行。 */
static int is_evdev_fd(int fd) {
    struct stat st;
    if (fd < 0 || fstat(fd, &st) != 0) return 0;
    if (!S_ISCHR(st.st_mode) || major(st.st_rdev) != 13) return 0;
    unsigned long events[EV_MAX / (sizeof(unsigned long) * 8) + 1] = {0};
    unsigned long axes[ABS_MAX / (sizeof(unsigned long) * 8) + 1] = {0};
    if (ioctl(fd, EVIOCGBIT(0, sizeof(events)), events) < 0 ||
        !input_has_bit(events, EV_ABS)) return 0;
    if (ioctl(fd, EVIOCGBIT(EV_ABS, sizeof(axes)), axes) < 0) return 0;
    return (input_has_bit(axes, ABS_X) && input_has_bit(axes, ABS_Y)) ||
           (input_has_bit(axes, ABS_MT_POSITION_X) &&
            input_has_bit(axes, ABS_MT_POSITION_Y));
}

static void install_touch_hook(void) {
    glob_t g;
    memset(&g, 0, sizeof(g));
    if (glob(EVDEV_GLOB, 0, NULL, &g) == 0) {
        for (size_t i = 0; i < g.gl_pathc && evdev_cnt < MAX_EVDEV; i++) {
            int fd = open(g.gl_pathv[i], O_RDONLY | O_NONBLOCK | O_CLOEXEC);
            if (fd >= 0) {
                if (is_evdev_fd(fd)) evdev_fds[evdev_cnt++] = fd;
                else close(fd);
            }
        }
    }
    globfree(&g);
    tlog("hooked %d touch evdev fds (pid %d)\n", evdev_cnt, (int)getpid());
}

/* 弹窗图层是否可见 —— 不依赖 VO_push 是否还在被调用（桌面静止时可能不再推帧，
 * touch_intercept 会停在旧值上）。80ms 内复用上次结果，开销可忽略。 */
static int popup_visible_cached = 0;
static long last_vis_check = 0;

static void refresh_touch_intercept(void) {
    long now = now_ms();
    if (now - last_vis_check < 80) return;
    last_vis_check = now;
    if (popup_overlay_index < 0 || popup_overlay_index >= novl) return;
    overlay_t *o = &ovls[popup_overlay_index];
    load_overlay(o);                       /* mtime 未变时只有一次 stat */
    int vis = (o->pix && o->has_visible && o->w >= 320 && o->h >= 240) ? 1 : 0;
    if (vis != popup_visible_cached) {
        popup_visible_cached = vis;
        gflog("[ovl] 弹窗%s\n", vis ? "显示" : "隐藏");
    }
    touch_intercept = vis;
    set_touch_grab(vis);
}

static void xz_emit_tap(int x, int y) {
    FILE *f = fopen(CMD_PATH, "a");
    if (f) {
        fprintf(f, "TAP %d %d\n", x, y);
        fclose(f);
    }
    tlog("TAP %d %d\n", x, y);
    gflog("[ovl] TAP %d %d\n", x, y);
}

static void feed_events(const uint8_t *buf, int len) {
    int count = len / (int)sizeof(struct input_event);
    for (int i = 0; i < count; i++) {
        struct input_event e;
        memcpy(&e, buf + (size_t)i * sizeof(e), sizeof(e));
        /* 兼容单点 ABS_X/Y 与多点 ABS_MT_POSITION_X/Y 两种协议 */
        if (e.type == EV_ABS &&
            (e.code == ABS_MT_POSITION_X || e.code == ABS_X)) st_lx = e.value;
        else if (e.type == EV_ABS &&
                 (e.code == ABS_MT_POSITION_Y || e.code == ABS_Y)) st_ly = e.value;
        else if (e.type == EV_KEY && e.code == BTN_TOUCH) {
            if (e.value == 1) {
                st_down = 1;
                st_t0 = now_ms();
                st_x0 = st_lx; st_y0 = st_ly;
            } else if (e.value == 0 && st_down) {
                st_down = 0;
                long dt = now_ms() - st_t0;
                int dx = st_lx - st_x0, dy = st_ly - st_y0;
                if (dt >= TAP_MIN_MS && dt <= TAP_MAX_MS &&
                    dx * dx + dy * dy <= TAP_MAX_MOVE * TAP_MAX_MOVE)
                    xz_emit_tap(st_lx, st_ly);
            }
        } else if (e.type == EV_ABS && e.code == ABS_MT_TRACKING_ID) {
            /* 协议 A 设备无 BTN_TOUCH：用 tracking id 出现/消失判定 */
            if (e.value >= 0 && !st_down) {
                st_down = 1;
                st_t0 = now_ms();
                st_x0 = st_lx; st_y0 = st_ly;
            } else if (e.value < 0 && st_down) {
                st_down = 0;
                long dt = now_ms() - st_t0;
                int dx = st_lx - st_x0, dy = st_ly - st_y0;
                if (dt >= TAP_MIN_MS && dt <= TAP_MAX_MS &&
                    dx * dx + dy * dy <= TAP_MAX_MOVE * TAP_MAX_MOVE)
                    xz_emit_tap(st_lx, st_ly);
            }
        }
    }
}

/* 非阻塞排空所有 evdev；拦截开启时事件进 tap 状态机（不再透传） */
static void drain_evdev(void) {
    if (!evdev_cnt) return;
    uint8_t buf[512];
    for (int i = 0; i < evdev_cnt; i++) {
        for (;;) {
            ssize_t r = read(evdev_fds[i], buf, sizeof(buf));
            if (r > 0) {
                if (touch_intercept) feed_events(buf, (int)r);
                continue;
            }
            break; /* EAGAIN / EOF / 错误 */
        }
    }
}

static ssize_t hook_read_common(ssize_t ret, int fd, void *buf, size_t count) {
    if (ret > 0 && count >= sizeof(struct input_event) && is_evdev_fd(fd)) {
        if (!inited) init_once();
        if (gf_enabled) refresh_touch_intercept();
        if (touch_intercept) {
            feed_events(buf, (int)ret);
            memset(buf, 0, ret); /* 吞掉：调用方看到"成功但无事件" */
        }
    }
    return ret;
}

/* 库加载即安装（constructor）：确保 launcher 之后打开的触摸设备都被记录 */
__attribute__((constructor)) static void shim_ctor(void) {
    real_read = (ssize_t (*)(int, void *, size_t))dlsym(RTLD_NEXT, "read");
    if (!real_read)
        real_read = (ssize_t (*)(int, void *, size_t))syscall;
    real_read_chk =
        (ssize_t (*)(int, void *, size_t, size_t))dlsym(RTLD_NEXT, "__read_chk");
    install_touch_hook();
}

ssize_t read(int fd, void *buf, size_t count) {
    if (!real_read) return (ssize_t)syscall(SYS_read, fd, buf, count);
    return hook_read_common(real_read(fd, buf, count), fd, buf, count);
}

ssize_t __read_chk(int fd, void *buf, size_t count, size_t bufsize) {
    (void)bufsize;
    if (real_read_chk)
        return hook_read_common(real_read_chk(fd, buf, count, bufsize),
                                fd, buf, count);
    if (!real_read) return (ssize_t)syscall(SYS_read, fd, buf, count);
    return hook_read_common(real_read(fd, buf, count), fd, buf, count);
}

/* ---------------- blending ---------------- */

static void blend_icon(uint8_t *dst, int dw, int dh, const overlay_t *o,
                       int frame_idx) {
    pthread_mutex_lock(&ovl_lock);
    if (!o->pix) { pthread_mutex_unlock(&ovl_lock); return; }
    const uint8_t *src = o->pix + (size_t)frame_idx * o->w * o->h * 4;
    int ow = o->w, oh = o->h, ox = o->x, oy = o->y;
    for (int iy = 0; iy < oh; iy++) {
        int dy = oy + iy;
        if (dy < 0 || dy >= dh) continue;
        uint8_t *drow = dst + (size_t)dy * dw * 4;
        const uint8_t *srow = src + (size_t)iy * ow * 4;
        for (int ix = 0; ix < ow; ix++) {
            int dx = ox + ix;
            if (dx < 0 || dx >= dw) continue;
            const uint8_t *s = srow + (size_t)ix * 4;
            unsigned a = s[3];
            if (!a) continue;
            uint8_t *d = drow + (size_t)dx * 4;
            if (a >= 250) {
                d[0] = s[2]; d[1] = s[1]; d[2] = s[0]; d[3] = 0xff;
            } else {
                unsigned ia = 255 - a;
                d[0] = (uint8_t)((s[2] * a + d[0] * ia) / 255);
                d[1] = (uint8_t)((s[1] * a + d[1] * ia) / 255);
                d[2] = (uint8_t)((s[0] * a + d[2] * ia) / 255);
                d[3] = 0xff;
            }
        }
    }
    pthread_mutex_unlock(&ovl_lock);
}

/* ---------------- hooked entry ---------------- */

int VO_push(void *self, int display_id, int channel, void *frame) {
    if (!inited) init_once();

    if (!real_push) {
        static long last_err = 0;
        long n = now_ms();
        if (n - last_err > 5000) {
            last_err = n;
            gflog("[ovl] drop frame: real push unresolved\n");
        }
        return -1;
    }

    uint8_t *fb = *(uint8_t **)frame;
    int len = *(int *)((uint8_t *)frame + 16);
    int w = *(int *)((uint8_t *)frame + 20);
    int h = *(int *)((uint8_t *)frame + 24);

    static long last_check = 0;
    long now = now_ms();
    int do_check = (now - last_check) >= RELOAD_CHECK_PERIOD_MS;
    if (do_check) last_check = now;

    if (do_check && fb && len > 0 && access("/tmp/ovl_dump", F_OK) == 0) {
        FILE *f = fopen("/tmp/ovl_snap.raw", "wb");
        if (f) {
            fwrite(fb, 1, (size_t)len, f);
            fclose(f);
            gflog("[ovl] frame dumped %dx%d\n", w, h);
        }
        unlink("/tmp/ovl_dump");
    }

    if (!gf_enabled) return real_push(self, display_id, channel, frame);

    if (!fb || len <= 0 || w <= 0 || h <= 0 ||
        (size_t)len != (size_t)w * h * 4)
        return real_push(self, display_id, channel, frame);

    static int disable_idle = 0;
    if (do_check && access(DISABLE_FLAG, F_OK) == 0) {
        if (!disable_idle) {
            gflog("[ovl] DISABLE flag present — idle\n");
            disable_idle = 1;
        }
        touch_intercept = 0;
        return real_push(self, display_id, channel, frame);
    }
    disable_idle = 0;

    /* 非全屏分辨率（理论上不存在）不做触摸接管 */
    if (w != 640 || h != 480) {
        touch_intercept = 0;
        return real_push(self, display_id, channel, frame);
    }

    int popup_vis = 0;
    for (int i = 0; i < novl; i++) {
        overlay_t *o = &ovls[i];
        if (do_check) load_overlay(o);
        if (!o->pix || !o->has_visible) continue;
        int idx = 0;
        if (o->n > 1 && o->fps > 0) {
            int per = 1000 / o->fps;
            if (per < 1) per = 1;
            idx = (int)((now / per) % o->n);
        }
        blend_icon(fb, w, h, o, idx);
        if (i == popup_overlay_index && o->w >= 320 && o->h >= 240)
            popup_vis = 1;
    }
    if (popup_vis != popup_visible_cached) {
        popup_visible_cached = popup_vis;
        gflog("[ovl] 弹窗%s\n", popup_vis ? "显示" : "隐藏");
    }
    touch_intercept = popup_vis;
    set_touch_grab(popup_vis);
    if (popup_vis && do_check) drain_evdev();

    if (do_check && access("/tmp/ovl_dump_b", F_OK) == 0) {
        FILE *f = fopen("/tmp/ovl_snap_b.raw", "wb");
        if (f) {
            fwrite(fb, 1, (size_t)len, f);
            fclose(f);
            gflog("[ovl] blended frame dumped %dx%d\n", w, h);
        }
        unlink("/tmp/ovl_dump_b");
    }

    return real_push(self, display_id, channel, frame);
}
