#define _POSIX_C_SOURCE 200809L
#include "asset_copy.h"
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int make_parent_directories(const char *path) {
    char *directory = strdup(path);
    if (!directory) return -1;
    int result = 0;
    for (char *p = directory + 1; *p; p++) {
        if (*p != '/') continue;
        *p = '\0';
        if (mkdir(directory, 0777) != 0 && errno != EEXIST) {
            result = -1;
            break;
        }
        *p = '/';
    }
    int saved_errno = errno;
    free(directory);
    errno = saved_errno;
    return result;
}

static int matches_existing_file(AAsset *asset, const char *destination) {
    FILE *existing = fopen(destination, "rb");
    if (!existing) return -1;
    char incoming[8192], current[8192];
    int result = 1;
    int count;
    while ((count = AAsset_read(asset, incoming, sizeof(incoming))) > 0) {
        size_t actual = fread(current, 1, (size_t)count, existing);
        if (ferror(existing)) {
            result = -1;
            break;
        }
        if (actual != (size_t)count || memcmp(incoming, current, actual) != 0) {
            result = 0;
            break;
        }
    }
    if (count < 0 || ferror(existing)) result = -1;
    if (result == 1 && fgetc(existing) != EOF) result = 0;
    if (ferror(existing)) result = -1;
    if (fclose(existing) != 0) result = -1;
    if (result < 0) errno = EIO;
    return result;
}

int be5_copy_asset_file(AAssetManager *manager, const char *source,
                        const char *destination, int preserve_existing) {
    struct stat current;
    int exists = stat(destination, &current) == 0;
    if (!exists && errno != ENOENT) return -1;
    if (exists && !S_ISREG(current.st_mode)) {
        errno = EINVAL;
        return -1;
    }
    if (exists && preserve_existing) return 0;

    AAsset *asset = AAssetManager_open(manager, source, AASSET_MODE_STREAMING);
    if (!asset) {
        errno = ENOENT;
        return -1;
    }
    int result = -1;
    char *temporary = NULL;
    int temporary_created = 0;
    FILE *output = NULL;
    off64_t length = AAsset_getLength64(asset);
    if (exists && current.st_size == length) {
        int match = matches_existing_file(asset, destination);
        if (match < 0) goto done;
        if (match) {
            result = 0;
            goto done;
        }
    }
    if (AAsset_seek64(asset, 0, SEEK_SET) != 0) {
        errno = EIO;
        goto done;
    }
    if (make_parent_directories(destination) != 0) goto done;
    temporary = malloc(strlen(destination) + sizeof(".tmp.XXXXXX"));
    if (!temporary) goto done;
    sprintf(temporary, "%s.tmp.XXXXXX", destination);
    int fd = mkstemp(temporary);
    if (fd < 0) goto done;
    temporary_created = 1;
    output = fdopen(fd, "wb");
    if (!output) {
        int saved_errno = errno;
        close(fd);
        errno = saved_errno;
        goto done;
    }
    char buffer[8192];
    off64_t total = 0;
    int count;
    while ((count = AAsset_read(asset, buffer, sizeof(buffer))) > 0) {
        if (fwrite(buffer, 1, (size_t)count, output) != (size_t)count) goto done;
        total += count;
    }
    if (count < 0 || total != length) {
        errno = EIO;
        goto done;
    }
    if (fflush(output) != 0 || fsync(fileno(output)) != 0) goto done;
    int closed = fclose(output);
    output = NULL;
    if (closed != 0) goto done;

    // Keep the old file until the complete replacement is ready in the same directory.
    if (rename(temporary, destination) != 0) goto done;
    result = 1;

done:;
    int saved_errno = errno;
    if (output) fclose(output);
    if (temporary) {
        if (temporary_created) unlink(temporary);
        free(temporary);
    }
    AAsset_close(asset);
    errno = saved_errno;
    return result;
}
