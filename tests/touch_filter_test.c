/* Test production classification/read filtering with synthetic input devices.
 * The runner removes .init_array, so the constructor never opens real evdevs. */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/input.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>

static unsigned char event_bits[(EV_MAX + 8) / 8];
static unsigned char abs_bits[(ABS_MAX + 8) / 8];
static int failed_probe;
static int character_device = 1;
static int failures;

static int fixture_fstat(int fd, struct stat *st) {
    if (fd != 101) { errno = EBADF; return -1; }
    memset(st, 0, sizeof(*st));
    st->st_mode = character_device ? S_IFCHR : S_IFREG;
    st->st_rdev = makedev(13, 65);
    return 0;
}

static int fixture_ioctl(int fd, unsigned long request, ...) {
    va_list ap;
    va_start(ap, request);
    void *out = va_arg(ap, void *);
    va_end(ap);
    unsigned int kind = _IOC_NR(request);
    if (fd != 101 || _IOC_TYPE(request) != 'E' ||
        (kind != _IOC_NR(EVIOCGBIT(0, 0)) &&
         kind != _IOC_NR(EVIOCGBIT(EV_ABS, 0)))) {
        errno = EINVAL;
        return -1;
    }
    int is_abs = kind == _IOC_NR(EVIOCGBIT(EV_ABS, 0));
    if (failed_probe == (is_abs ? 2 : 1)) { errno = EIO; return -1; }
    const unsigned char *bits = is_abs ? abs_bits : event_bits;
    size_t size = is_abs ? sizeof(abs_bits) : sizeof(event_bits);
    if (size > _IOC_SIZE(request)) size = _IOC_SIZE(request);
    memcpy(out, bits, size);
    return (int)size;
}

#define fstat fixture_fstat
#define ioctl fixture_ioctl
#include "../xiaozhi/uishim/maixcam2_overlay.c"
#undef ioctl
#undef fstat

static void bit_on(unsigned char *bits, unsigned int bit) {
    bits[bit / 8] |= (unsigned char)(1u << (bit % 8));
}

static void reset_fixture(void) {
    memset(event_bits, 0, sizeof(event_bits));
    memset(abs_bits, 0, sizeof(abs_bits));
    failed_probe = 0;
    character_device = 1;
    bit_on(event_bits, EV_KEY);
}

static void expect_classification(const char *name, int expected) {
    int actual = !!is_evdev_fd(101);
    printf("%s %s (touch=%d, expected=%d)\n",
           actual == expected ? "PASS" : "FAIL", name, actual, expected);
    if (actual != expected) failures++;
}

static void expect_read(const char *name, int swallowed) {
    struct input_event event = {0};
    event.type = EV_KEY;
    event.code = KEY_ENTER;
    event.value = 1;
    struct input_event original = event;
    struct input_event empty = {0};
    inited = 1;
    gf_enabled = 0; /* Keep the test popup state without loading real overlays. */
    touch_intercept = 1;
    ssize_t result = hook_read_common(sizeof(event), 101, &event, sizeof(event));
    int passed = result == sizeof(event) &&
        memcmp(&event, swallowed ? &empty : &original, sizeof(event)) == 0;
    printf("%s %s\n", passed ? "PASS" : "FAIL", name);
    if (!passed) failures++;
}

int main(void) {
    reset_fixture();
    expect_classification("gpio keys / keyboard are not touch", 0);
    expect_read("popup preserves gpio key bytes", 0);

    bit_on(event_bits, EV_ABS);
    bit_on(abs_bits, ABS_X);
    bit_on(abs_bits, ABS_Y);
    expect_classification("absolute X/Y touch", 1);
    expect_read("popup still suppresses touch-device input", 1);

    reset_fixture();
    bit_on(event_bits, EV_ABS);
    bit_on(abs_bits, ABS_MT_POSITION_X);
    bit_on(abs_bits, ABS_MT_POSITION_Y);
    expect_classification("multitouch position pair", 1);

    reset_fixture();
    bit_on(event_bits, EV_ABS);
    bit_on(abs_bits, ABS_X);
    expect_classification("missing Y axis", 0);
    bit_on(abs_bits, ABS_MT_POSITION_Y);
    expect_classification("mixed single/multitouch axes are incomplete", 0);

    bit_on(abs_bits, ABS_Y);
    failed_probe = 1;
    expect_classification("event capability query failure", 0);
    expect_read("query failure leaves input bytes untouched", 0);
    failed_probe = 2;
    expect_classification("absolute capability query failure", 0);

    failed_probe = 0;
    memset(event_bits, 0, sizeof(event_bits));
    expect_classification("coordinate bits without EV_ABS", 0);
    bit_on(event_bits, EV_ABS);
    character_device = 0;
    expect_classification("regular file is not touch", 0);
    if (is_evdev_fd(-1) || is_evdev_fd(102)) {
        puts("FAIL invalid descriptors must not be intercepted");
        failures++;
    }
    printf("touch filter: %d failure(s)\n", failures);
    return failures ? 1 : 0;
}
