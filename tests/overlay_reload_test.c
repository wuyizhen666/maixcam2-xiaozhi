/* Exercise the actual loader. run_overlay_tests.py removes the hook constructor
 * registration from this test object so no input devices are opened. */
#include "../xiaozhi/uishim/maixcam2_overlay.c"

static void write_pixel(const char *path, unsigned char red) {
    FILE *file = fopen(path, "wb");
    if (!file) { perror("fopen"); exit(2); }
    uint32_t header[3] = {1, 1, 1};
    unsigned char pixel[4] = {red, 0, 0, 255};
    if (fwrite(header, sizeof(header), 1, file) != 1 ||
        fwrite(pixel, sizeof(pixel), 1, file) != 1) exit(2);
    fclose(file);
}

int main(void) {
    char dir[] = "/tmp/xz-overlay-test-XXXXXX";
    if (!mkdtemp(dir)) return 2;
    char path[256], replacement[256];
    snprintf(path, sizeof(path), "%s/frame.rgba", dir);
    snprintf(replacement, sizeof(replacement), "%s/replacement.rgba", dir);
    overlay_t overlay = {0};
    snprintf(overlay.path, sizeof(overlay.path), "%s", path);
    write_pixel(path, 10);
    if (load_overlay(&overlay) != 1 || overlay.pix[0] != 10) return 1;
    if (load_overlay(&overlay) != 0) return 1;
    puts("PASS unchanged frame avoids reload");

    struct stat initial;
    if (stat(path, &initial)) return 2;
    struct timespec times[2] = {initial.st_atim, initial.st_mtim};
    times[1].tv_nsec += times[1].tv_nsec < 999999999 ? 1 : -1;
    write_pixel(path, 20);
    if (utimensat(AT_FDCWD, path, times, 0)) return 2;
    if (load_overlay(&overlay) != 1 || overlay.pix[0] != 20) {
        puts("FAIL changed frame within the same second was ignored");
        return 1;
    }
    puts("PASS nanosecond update reloads the frame");

    write_pixel(replacement, 30);
    if (utimensat(AT_FDCWD, replacement, times, 0) || rename(replacement, path)) return 2;
    if (load_overlay(&overlay) != 1 || overlay.pix[0] != 30) {
        puts("FAIL atomic replacement with preserved timestamp was ignored");
        return 1;
    }
    puts("PASS atomic replacement reloads the frame");
    free_overlay(&overlay);
    unlink(path); rmdir(dir);
    return 0;
}
